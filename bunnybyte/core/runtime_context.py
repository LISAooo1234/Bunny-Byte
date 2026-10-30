"""Prompt, history, recall, and memory-scope runtime helpers."""

from __future__ import annotations

import json
import re
import textwrap

from .cost_meter import record_call
from .memory_scope import render_memory_scope, set_memory_scope
from .transcript_recall import (
    refine_turn_index_with_model,
    retrieve_transcript_snippets,
    update_turn_index,
)
from .workspace import clip

CHECKPOINT_NONE_STATUS = "no-checkpoint"
SHORT_APPROVAL_REPLIES = frozenset(
    {
        "yes",
        "ok",
        "okay",
        "go ahead",
        "continue",
        "好",
        "好的",
        "继续",
        "可以",
        "嗯",
        "行",
        "我愿意",
    }
)
NEXT_STEP_CUE_PATTERN = re.compile(
    r"(?:"
    r"(?:next|now|then)\s+i\s+(?:will|can|should|am going to)|"
    r"i(?:'ll|\s+will|\s+can)\s+(?:continue|proceed|run|check|inspect|fix|update|implement|test)|"
    r"接下来|下一步|然后|继续|我会|我将|我可以"
    r")",
    re.IGNORECASE,
)


class RuntimeContextMixin:
    def memory_text(self):
        return self.memory.render_memory_text()

    def history_text(self):
        history = self.session["history"]
        if not history:
            return "- empty"

        lines = []
        for item in history:
            if item["role"] == "tool":
                lines.append(
                    f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}"
                )
                lines.append(str(item["content"]))
            else:
                lines.append(f"[{item['role']}] {item['content']}")

        return "\n".join(lines)

    def feature_enabled(self, name):
        return bool(self.feature_flags.get(str(name), False))

    def effective_user_message(self, user_message):
        text = str(user_message or "")
        normalized = " ".join(text.strip().split()).lower()
        if normalized not in SHORT_APPROVAL_REPLIES:
            return text

        prior_message = self.latest_assistant_message()
        if not prior_message or not NEXT_STEP_CUE_PATTERN.search(prior_message):
            return text

        return textwrap.dedent(
            f"""\
            The user's latest reply was a short approval: "{text.strip()}".
            Interpret it as approval to continue the latest clear next step from the immediately preceding assistant message.
            Continue that work now instead of asking again or returning an acknowledgement-only final.
            Relevant prior assistant message:
            {clip(prior_message, 900)}
            """
        ).strip()

    def prompt(self, user_message):
        prompt, _ = self._build_prompt_and_metadata(user_message)
        return prompt

    def latest_assistant_message(self):
        for item in reversed(self.session.get("history", [])):
            if item.get("role") == "assistant":
                return str(item.get("content", "")).strip()
        return ""

    def record(self, item):
        self.ensure_session_started()
        if item.get("role") == "user":
            self._maybe_update_session_topic(item.get("content", ""))
        item = self.secret_vault.mask_structure(item, self.known_secret_items())  # 用户粘贴的密钥也换成占位符
        self.session["history"].append(self.turn_history.enrich(item))
        self.session_path = self.session_store.save(self.session)

    def index_turn(self, turn_id, run_id=""):
        if not self.feature_enabled("turn_index"):
            return {}
        index = update_turn_index(self.session, turn_id)
        if not index:
            return {}
        if self.feature_enabled("model_turn_index") and not hasattr(self.model_client, "outputs"):
            previous_model_metadata = dict(getattr(self.model_client, "last_completion_metadata", {}) or {})
            try:
                index = refine_turn_index_with_model(
                    self.model_client,
                    self.session.get("history", []),
                    turn_id,
                    index,
                    max_new_tokens=self.max_new_tokens,
                    on_usage=lambda metadata: record_call(self, metadata, source="turn_index"),
                )
                self.session.setdefault("turn_index", {})[str(turn_id)] = index
            except Exception as exc:
                self.session_event_bus.emit(
                    "turn_index_model_failed",
                    {
                        "run_id": str(run_id or ""),
                        "turn_id": str(turn_id),
                        "error": clip(str(exc), 300),
                    },
                )
            finally:
                if hasattr(self.model_client, "last_completion_metadata"):
                    self.model_client.last_completion_metadata = previous_model_metadata
        self.session_path = self.session_store.save(self.session)
        self.session_event_bus.emit(
            "turn_indexed",
            {
                "run_id": str(run_id or ""),
                "turn_id": index["turn_id"],
                "keywords": list(index.get("keywords", [])),
                "summary": clip(index.get("summary", ""), 300),
                "entities": list(index.get("entities", [])),
                "event_ids": list(index.get("event_ids", [])),
            },
        )
        return index

    def recall_transcript(self, query, limit=6, char_budget=6_000):
        return retrieve_transcript_snippets(
            query,
            self.session.get("history", []),
            visible_event_ids=set(),
            turn_index=self.session.get("turn_index", {}),
            limit=limit,
            char_budget=char_budget,
        )

    def set_memory_scope(self, mode, session_ids=None):
        scope = set_memory_scope(self.session, mode, session_ids=session_ids)
        if not self.is_pending_session:
            self.session_path = self.session_store.save(self.session)
        self.session_event_bus.emit("memory_scope_changed", scope)
        return scope

    def memory_scope_text(self):
        return render_memory_scope(self)

    def prompt_metadata(self, user_message, prompt):
        _, metadata = self._build_prompt_and_metadata(user_message)
        return metadata

    def _build_prompt_and_metadata(self, user_message):
        refresh = self.refresh_prefix()
        self.resume_state = self.evaluate_resume_state()
        prompt, metadata = self.context_manager.build(user_message)
        if (
            (metadata.get("prompt_over_budget") or metadata.get("budget_reductions"))
            and len(self.session.get("history", [])) > 4
            and not getattr(self.model_client, "deterministic_scripted", False)
        ):
            self.compact_history(trigger="auto_prompt_over_budget")
            prompt, metadata = self.context_manager.build(user_message)
            metadata["auto_compacted"] = True
        metadata.update(
            {
                "prefix_chars": len(self.prefix),
                "workspace_chars": len(self.workspace.text()),
                "memory_chars": len(self.memory_text()),
                "history_chars": len(self.history_text()),
                "request_chars": len(user_message),
                "tool_count": len(self.tools),
                "workspace_docs": len(self.workspace.project_docs),
                "recent_commits": len(self.workspace.recent_commits),
                "prefix_hash": self.prefix_state.hash,
                "prompt_cache_key": self.prefix_state.hash,
                "workspace_fingerprint": self.prefix_state.workspace_fingerprint,
                "tool_signature": self.prefix_state.tool_signature,
                "workspace_changed": refresh["workspace_changed"],
                "prefix_changed": refresh["prefix_changed"],
                "prompt_cache_supported": bool(
                    getattr(self.model_client, "supports_prompt_cache", False)
                ),
                "resume_status": self.resume_state.get(
                    "status", CHECKPOINT_NONE_STATUS
                ),
                "stale_summary_invalidations": int(
                    self.resume_state.get("stale_summary_invalidations", 0)
                ),
                "stale_paths": list(self.resume_state.get("stale_paths", [])),
                "runtime_identity_mismatch_fields": list(
                    self.resume_state.get("runtime_identity_mismatch_fields", [])
                ),
            }
        )
        metadata.update(self.detected_secret_env_summary())
        prompt = self.redact_text(prompt)  # 最后一道：规则、记忆、技能、当前请求里的密钥也不发给 provider
        usage_payload = {
            "run_id": getattr(getattr(self, "current_task_state", None), "run_id", ""),
            "context_usage": metadata.get("context_usage", {}),
        }
        if getattr(self, "current_task_state", None) is not None:
            self.session_event_bus.emit("context_usage_recorded", usage_payload)
        return prompt, metadata
