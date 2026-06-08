"""Non-destructive session compaction for model-facing context."""

import re

from ..providers.base import complete_model
from .context_usage import estimate_tokens
from .workspace import clip, now

COMPACT_MAX_OUTPUT_TOKENS = 4096
COMPACT_SUMMARY_FALLBACK_PREFIX = "Compacted session summary:"
COMPACT_SUMMARY_PROMPT = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

Your task is to create a detailed summary of the BunnyByte conversation so far. This summary will be placed at the start of a continuing coding-agent session; newer messages will follow after it. Preserve enough context for the agent to continue development work without losing important details.

Your summary should include these sections:

1. Primary Request and Intent: Capture the user's explicit requests and intents in detail.
2. Key Technical Concepts: List important technical concepts, technologies, files, commands, and runtime behavior discussed.
3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. Include why each file mattered and important snippets or function names when available.
4. Errors and fixes: List errors, failed approaches, test failures, and how they were fixed. Include user corrections and feedback.
5. Problem Solving: Document problems solved and ongoing troubleshooting.
6. All user messages: List all user messages from the summarized portion that are not tool results.
7. Pending Tasks: Outline tasks explicitly requested but not finished.
8. Current Work: Describe precisely what was being worked on immediately before this summary request.
9. Optional Next Step: If there is a next step directly tied to the most recent work, include it with direct quotes when possible.

Return only:
<summary>
...
</summary>

Conversation to summarize:
{transcript}
"""


def _compact_output_tokens(max_new_tokens):
    if max_new_tokens is None:
        return None
    return max(COMPACT_MAX_OUTPUT_TOKENS, int(max_new_tokens or 0))


class CompactManager:
    def __init__(self, agent):
        self.agent = agent

    def compact(self, trigger="manual", keep_recent_turns=3):
        history = list(self.agent.session.get("history", []))
        groups = self._group(history)
        if len(groups) <= keep_recent_turns:
            summary = self._summary(trigger, history, history, "")
            self.agent.session_event_bus.emit("compaction_created", summary)
            return summary

        compacted_turns, kept_turns = self._split_groups_for_compaction(groups, keep_recent_turns)
        compacted_items = [item for _, items in compacted_turns for item in items]
        kept_items = [item for _, items in kept_turns for item in items]
        if not compacted_items:
            summary = self._summary(trigger, history, self.prompt_history(), "")
            self.agent.session_event_bus.emit("compaction_created", summary)
            return summary
        summary_text = self._model_summary_text(compacted_items)
        compaction_id = self._new_compaction_id()
        summary_item = self._summary_item(compaction_id, summary_text)
        preserved_event_ids = [str(item.get("event_id", "")) for item in kept_items if item.get("event_id")]
        summarized_event_ids = [str(item.get("event_id", "")) for item in compacted_items if item.get("event_id")]
        view_items = [summary_item, *kept_items]
        summary = self._summary(trigger, history, view_items, summary_text)
        summary.update(
            {
                "id": compaction_id,
                "strategy": "model_summary_v1",
                "summary": summary_text,
                "summary_event_id": summary_item["event_id"],
                "summarized_event_ids": summarized_event_ids,
                "preserved_event_ids": preserved_event_ids,
                "preserved_from_event_id": preserved_event_ids[0] if preserved_event_ids else "",
                "last_summarized_event_id": summarized_event_ids[-1] if summarized_event_ids else "",
            }
        )
        self.agent.session.setdefault("compactions", []).append(summary)
        self.agent.session["context_view"] = {
            "active_compaction_id": compaction_id,
            "summary_event_id": summary_item["event_id"],
            "preserved_from_event_id": summary["preserved_from_event_id"],
            "last_summarized_event_id": summary["last_summarized_event_id"],
        }
        self.agent.session_path = self.agent.session_store.save(self.agent.session)
        self.agent.session_event_bus.emit(
            "compaction_created",
            {key: value for key, value in summary.items() if key != "summary"},
        )
        if self.agent.current_task_state:
            self.agent.emit_trace(
                self.agent.current_task_state,
                "compaction_started",
                {"trigger": trigger, "pre_tokens": summary["pre_tokens"]},
            )
            self.agent.emit_trace(
                self.agent.current_task_state,
                "compaction_finished",
                {key: value for key, value in summary.items() if key != "summary"},
            )
        return summary

    def prompt_history(self):
        history = list(self.agent.session.get("history", []))
        compaction = self.active_compaction()
        if not compaction:
            return history
        summary_item = self._summary_item(
            str(compaction.get("id", "compact")),
            str(compaction.get("summary", "")).strip(),
            event_id=str(compaction.get("summary_event_id", "")) or None,
        )
        preserved_ids = {str(event_id) for event_id in compaction.get("preserved_event_ids", []) if str(event_id)}
        last_summarized = str(compaction.get("last_summarized_event_id", ""))
        preserved = []
        include_after_boundary = not preserved_ids
        for item in history:
            event_id = str(item.get("event_id", ""))
            if event_id in preserved_ids:
                preserved.append(item)
                continue
            if include_after_boundary:
                preserved.append(item)
                continue
            if last_summarized and event_id == last_summarized:
                include_after_boundary = True
        return [summary_item, *preserved]

    def active_compaction(self):
        context_view = self.agent.session.get("context_view") or {}
        active_id = str(context_view.get("active_compaction_id", "") or "")
        if not active_id:
            return None
        for item in reversed(self.agent.session.get("compactions", [])):
            if str(item.get("id", "")) == active_id:
                return item
        return None

    def _split_groups_for_compaction(self, groups, keep_recent_turns):
        compaction = self.active_compaction()
        if compaction:
            last_summarized = str(compaction.get("last_summarized_event_id", ""))
            if last_summarized:
                start_index = 0
                for index, (_, items) in enumerate(groups):
                    if any(str(item.get("event_id", "")) == last_summarized for item in items):
                        start_index = index + 1
                        break
                groups = groups[start_index:]
        if len(groups) <= keep_recent_turns:
            return [], groups
        return groups[:-keep_recent_turns], groups[-keep_recent_turns:]

    @staticmethod
    def _group(history):
        groups = []
        by_id = {}
        for item in history:
            turn_id = str(item.get("turn_id") or "legacy")
            if turn_id not in by_id:
                by_id[turn_id] = []
                groups.append((turn_id, by_id[turn_id]))
            by_id[turn_id].append(item)
        return groups

    def _summary(self, trigger, before, after, summary_text):
        pre_chars = sum(len(str(item.get("content", ""))) for item in before)
        post_chars = sum(len(str(item.get("content", ""))) for item in after)
        return {
            "trigger": str(trigger),
            "created_at": now(),
            "pre_tokens": estimate_tokens(pre_chars),
            "post_tokens": estimate_tokens(post_chars),
            "pre_items": len(before),
            "post_items": len(after),
            "summary_chars": len(summary_text),
        }

    def _model_summary_text(self, items):
        transcript = self._render_items_for_summary(items)
        prompt = COMPACT_SUMMARY_PROMPT.format(transcript=transcript)
        try:
            result = complete_model(
                self.agent.model_client,
                prompt,
                _compact_output_tokens(getattr(self.agent, "max_new_tokens", None)),
            )
            summary = self._extract_summary(result.text)
            if summary:
                return summary
        except Exception:
            return self._fallback_summary_text(items)
        return self._fallback_summary_text(items)

    def _extract_summary(self, text):
        text = str(text or "").strip()
        match = re.search(r"<summary>(.*?)</summary>", text, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip()
        text = re.sub(r"<analysis>.*?</analysis>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
        return text

    def _render_items_for_summary(self, items):
        lines = []
        for turn_id, turn_items in self._group(items):
            lines.append(f"Turn {turn_id}:")
            for item in turn_items:
                role = str(item.get("role", ""))
                if role == "tool":
                    lines.append(
                        f"[tool:{item.get('name', '')}] args={item.get('args', {})} result={clip(item.get('content', ''), 2000)}"
                    )
                else:
                    lines.append(f"[{role}] {clip(item.get('content', ''), 3000)}")
        return "\n".join(lines)

    def _fallback_summary_text(self, items):
        files_read = []
        files_modified = []
        user_requests = []
        assistant_notes = []
        for item in items:
            if item.get("role") == "user":
                user_requests.append(str(item.get("content", "")).strip())
            elif item.get("role") == "assistant":
                assistant_notes.append(str(item.get("content", "")).strip())
            elif item.get("role") == "tool":
                path = str(item.get("args", {}).get("path", "")).strip()
                if item.get("name") == "read_file" and path:
                    files_read.append(path)
                if item.get("name") in {"write_file", "patch_file"} and path:
                    files_modified.append(path)
        return "\n".join(
            [
                COMPACT_SUMMARY_FALLBACK_PREFIX,
                f"- Goal: {user_requests[-1] if user_requests else '-'}",
                "- Constraints and preferences: -",
                f"- Files read: {', '.join(sorted(set(files_read))) or '-'}",
                f"- Files modified: {', '.join(sorted(set(files_modified))) or '-'}",
                f"- Key decisions: {assistant_notes[-1] if assistant_notes else '-'}",
                f"- Current progress: compacted {len(items)} history items",
                "- Open blockers: -",
                "- Next step: continue from the latest preserved turn",
                "- Critical context: earlier turns were compacted; use preserved latest turns for exact wording",
            ]
        )

    def _summary_item(self, compaction_id, summary_text, event_id=None):
        item = {
            "role": "system",
            "kind": "compact_summary",
            "content": "Compacted context summary:\n" + str(summary_text or "").strip(),
            "created_at": now(),
            "source": "compact",
            "turn_id": compaction_id,
            "run_id": "",
            "event_id": event_id or f"{compaction_id}_summary",
        }
        return item

    def _new_compaction_id(self):
        sequence = len(self.agent.session.get("compactions", [])) + 1
        return f"compact_{now().replace(':', '').replace('-', '').replace('+', '_')}_{sequence:03d}"
