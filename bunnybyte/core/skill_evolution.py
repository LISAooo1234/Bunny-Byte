"""Evidence-gated skill self-evolution.

The runtime owns the conversation. This module owns the slower learning loop:
after a completed task, a forked review runtime can propose a reusable skill,
then a second forked review runtime judges whether the evidence is strong
enough to promote it. Periodic curation is deterministic and stages risky
maintenance instead of deleting skills directly.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..features import skills as skillslib
from .workspace import WorkspaceContext, clip, now

SCHEMA_VERSION = 1
PROPOSAL_REVIEW_THRESHOLD = 0.65
TURN_LEARNING_MIN_TOOL_STEPS = 3
TURN_LEARNING_MIN_USER_TURNS = 2
CURATOR_INTERVAL_DAYS = 7
LOW_USAGE_AFTER_DAYS = 30
MAX_REVIEW_HISTORY_CHARS = 12_000
SAFE_SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
SPECIFIC_NAME_RE = re.compile(
    r"(?:^|[-_])(?:fix|bug|pr|issue|today|tmp|temp)(?:[-_]|$)|"
    r"\d{6,}|event_\d+|run_\d+",
    re.IGNORECASE,
)


class SkillEvolutionManager:
    def __init__(self, runtime):
        self.runtime = runtime
        self.root = Path(runtime.root) / ".bunnybyte" / "skill-evolution"
        self.proposals_dir = self.root / "proposals"
        self.maintenance_dir = self.root / "maintenance"
        self.state_path = self.root / "curator_state.json"
        self.usage_path = self.root / "skill_usage.json"
        self.skill_tree_path = self.root / "SKILL_TREE.md"
        self._review_threads: list[threading.Thread] = []
        self._lock = threading.RLock()

    def after_turn(self, task_state, final_answer):
        if not self._feature_enabled("skill_evolution"):
            return {"started": False, "reason": "disabled"}
        if not self._background_allowed():
            return {"started": False, "reason": "background_gate"}
        self.maybe_run_curator()
        if not self._should_review_turn(task_state):
            return {"started": False, "reason": "gate"}
        payload = task_state.to_dict() if hasattr(task_state, "to_dict") else dict(task_state)
        thread = threading.Thread(
            target=self._review_completed_turn_thread,
            args=(payload, str(final_answer or "")),
            daemon=True,
            name=f"bunnybyte-skill-evolution-{payload.get('run_id', 'turn')}",
        )
        thread.start()
        self._review_threads.append(thread)
        self.runtime.session_event_bus.emit(
            "skill_evolution_scheduled",
            {"run_id": str(payload.get("run_id", "")), "task_id": str(payload.get("task_id", ""))},
        )
        return {"started": True, "run_id": str(payload.get("run_id", ""))}

    def wait_for_reviews(self, timeout=2.0):
        deadline = datetime.now(timezone.utc).timestamp() + float(timeout)
        for thread in list(self._review_threads):
            remaining = max(0.0, deadline - datetime.now(timezone.utc).timestamp())
            if remaining <= 0:
                break
            thread.join(remaining)
        self._review_threads = [thread for thread in self._review_threads if thread.is_alive()]

    def review_completed_turn(self, task_state_payload, final_answer):
        task_state_payload = dict(task_state_payload or {})
        history = self._history_for_review(str(task_state_payload.get("task_id", "")))
        proposal_answer, proposer_session_id = self._run_isolated_agent(
            "skill-proposal",
            self._proposal_prompt(task_state_payload, final_answer, history),
        )
        proposal = self._parse_json_answer(proposal_answer)
        normalized = self._normalize_proposal(
            proposal,
            task_state_payload,
            final_answer,
            proposer_session_id,
        )
        if not normalized.get("should_create_skill"):
            normalized["status"] = "skipped"
            normalized["skip_reason"] = normalized.get("skip_reason") or "no reusable workflow"
            self._save_proposal(normalized)
            self.runtime.session_event_bus.emit(
                "skill_evolution_skipped",
                {"run_id": normalized["run_id"], "proposal_id": normalized["proposal_id"], "reason": normalized["skip_reason"]},
            )
            return normalized

        errors = self._proposal_quality_errors(normalized)
        if errors:
            normalized["status"] = "rejected_quality_gate"
            normalized["quality_errors"] = errors
            self._save_proposal(normalized)
            self.runtime.session_event_bus.emit(
                "skill_evolution_rejected",
                {"run_id": normalized["run_id"], "proposal_id": normalized["proposal_id"], "reason": "quality_gate"},
            )
            return normalized

        self._save_proposal(normalized)
        review_answer, reviewer_session_id = self._run_isolated_agent(
            "skill-review",
            self._review_prompt(normalized),
        )
        review = self._normalize_review(self._parse_json_answer(review_answer), reviewer_session_id)
        normalized["review"] = review
        if review["decision"] == "accept" and review["necessity_score"] >= PROPOSAL_REVIEW_THRESHOLD:
            promoted = self._promote_proposal(normalized)
            normalized["status"] = "promoted" if promoted["promoted"] else "needs_manual_review"
            normalized["promotion"] = promoted
        else:
            normalized["status"] = "rejected_by_review" if review["decision"] == "reject" else "needs_revision"
        self._save_proposal(normalized)
        self.runtime.session_event_bus.emit(
            "skill_evolution_reviewed",
            {
                "run_id": normalized["run_id"],
                "proposal_id": normalized["proposal_id"],
                "decision": review["decision"],
                "status": normalized["status"],
            },
        )
        return normalized

    def record_skill_usage(self, skill):
        data = self._load_json(self.usage_path, {})
        name = str(getattr(skill, "name", "") or "").strip()
        if not name:
            return
        row = dict(data.get(name, {}) or {})
        row["name"] = name
        row["source"] = str(getattr(skill, "source", "") or "")
        row["skill_root"] = str(getattr(skill, "skill_root", "") or "")
        row["use_count"] = int(row.get("use_count", 0) or 0) + 1
        row["last_used_at"] = now()
        row.setdefault("first_used_at", row["last_used_at"])
        data[name] = row
        self._write_json(self.usage_path, data)

    def maybe_run_curator(self, force=False, dry_run=False, now_dt=None):
        if not self._feature_enabled("skill_curator"):
            return {"ran": False, "reason": "disabled"}
        now_dt = now_dt or datetime.now(timezone.utc)
        state = self._load_json(self.state_path, {})
        last_run = _parse_time(state.get("last_run_at"))
        if not force:
            if last_run is None:
                state["last_run_at"] = now_dt.isoformat()
                state["last_summary"] = "seeded; first curator run deferred for one interval"
                self._write_json(self.state_path, state)
                return {"ran": False, "reason": "seeded"}
            if now_dt - last_run < timedelta(days=CURATOR_INTERVAL_DAYS):
                return {"ran": False, "reason": "interval_gate"}

        report = self._build_curator_report(now_dt=now_dt, dry_run=dry_run)
        self._write_json(self.maintenance_dir / f"{report['maintenance_id']}.json", report)
        state.update(
            {
                "last_run_at": now_dt.isoformat(),
                "last_report_path": str(self.maintenance_dir / f"{report['maintenance_id']}.json"),
                "last_summary": report["summary"],
            }
        )
        self._write_json(self.state_path, state)
        self.refresh_skill_tree_index()
        self.runtime.session_event_bus.emit(
            "skill_curator_finished",
            {"maintenance_id": report["maintenance_id"], "action_count": len(report["actions"])},
        )
        return {"ran": True, "report": report}

    def approve_maintenance_action(self, maintenance_id, action_number):
        report_path = self._maintenance_report_path(maintenance_id)
        report = self._load_json(report_path, {})
        action = self._maintenance_action(report, action_number)
        if action.get("status") != "pending_user_approval":
            raise ValueError("maintenance action is not pending user approval")
        if action.get("action") != "delete":
            raise ValueError("only delete actions require approval")
        skill_path = Path(str(action.get("path", "")))
        skill_dir = skill_path.parent
        if not skill_dir.exists() or not skill_dir.is_dir():
            raise ValueError(f"skill directory not found: {skill_dir}")
        archive_root = Path(self.runtime.root) / ".bunnybyte" / "skills" / ".archive"
        archive_root.mkdir(parents=True, exist_ok=True)
        target = archive_root / f"{skill_dir.name}-{_compact_timestamp()}"
        shutil.move(str(skill_dir), str(target))
        action.update(
            {
                "status": "approved_archived",
                "approved_at": now(),
                "archive_path": str(target),
            }
        )
        self._write_json(report_path, report)
        if hasattr(self.runtime, "refresh_skills"):
            self.runtime.refresh_skills()
        self.refresh_skill_tree_index()
        self.runtime.session_event_bus.emit(
            "skill_curator_action_approved",
            {"maintenance_id": report.get("maintenance_id", ""), "action": action.get("action", ""), "skill": action.get("skill", "")},
        )
        return action

    def reject_maintenance_action(self, maintenance_id, action_number, reason=""):
        report_path = self._maintenance_report_path(maintenance_id)
        report = self._load_json(report_path, {})
        action = self._maintenance_action(report, action_number)
        if action.get("status") not in {"pending_user_approval", "needs_review"}:
            raise ValueError("maintenance action is not pending review")
        action.update(
            {
                "status": "rejected",
                "rejected_at": now(),
                "rejection_reason": str(reason or "").strip(),
            }
        )
        self._write_json(report_path, report)
        self.runtime.session_event_bus.emit(
            "skill_curator_action_rejected",
            {"maintenance_id": report.get("maintenance_id", ""), "action": action.get("action", ""), "skill": action.get("skill", "")},
        )
        return action

    def render_status(self):
        proposals = sorted(self.proposals_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        maintenance = sorted(self.maintenance_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        state = self._load_json(self.state_path, {})
        lines = [
            "## Skill Evolution",
            "",
            f"- proposals: {len(proposals)}",
            f"- maintenance_reports: {len(maintenance)}",
            f"- last_curator_run: {state.get('last_run_at') or '-'}",
            f"- skill_tree: `{self.skill_tree_path}`" if self.skill_tree_path.exists() else "- skill_tree: -",
        ]
        if proposals:
            lines.extend(["", "### Latest proposals", "", "| ID | Status | Skill | Review |", "| --- | --- | --- | --- |"])
            for path in proposals[:5]:
                item = self._load_json(path, {})
                lines.append(
                    "| "
                    f"`{item.get('proposal_id', path.stem)}` | "
                    f"{item.get('status', '-')} | "
                    f"`{item.get('skill', {}).get('name', '-')}` | "
                    f"{item.get('review', {}).get('decision', '-')} |"
                )
        return "\n".join(lines)

    def render_prompt_hint(self):
        if not self.skill_tree_path.exists():
            return ""
        rel = self.skill_tree_path.relative_to(self.runtime.root)
        return (
            "Skill routing index:\n"
            f"- When the flat skill list is ambiguous, read `{rel}` before choosing a skill.\n"
            "- Prefer narrow skills whose description and when-to-use match the task; avoid skills whose not-when-to-use applies."
        )

    def refresh_skill_tree_index(self):
        skills = getattr(self.runtime, "skills", {}) or {}
        if hasattr(self.runtime, "refresh_skills"):
            self.runtime.refresh_skills()
            skills = getattr(self.runtime, "skills", {}) or {}
        groups: dict[str, list] = {}
        for skill in skillslib.list_skills(skills):
            groups.setdefault(skill.source or "unknown", []).append(skill)
        lines = [
            "# Skill Tree",
            "",
            "Generated by BunnyByte skill evolution. Use this as a routing index before loading a specific skill.",
        ]
        for source, items in sorted(groups.items()):
            lines.extend(["", f"## {source}", ""])
            for skill in items:
                description = skill.description or skill.when_to_use or "No description"
                lines.append(f"- `/{skill.name}` — {description}")
                if skill.when_to_use:
                    lines.append(f"  - use when: {skill.when_to_use}")
                if skill.not_when_to_use:
                    lines.append(f"  - avoid when: {skill.not_when_to_use}")
        self.skill_tree_path.parent.mkdir(parents=True, exist_ok=True)
        self.skill_tree_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if hasattr(self.runtime, "refresh_prefix"):
            self.runtime.refresh_prefix(force=True)
        return self.skill_tree_path

    def _review_completed_turn_thread(self, task_state_payload, final_answer):
        try:
            self.review_completed_turn(task_state_payload, final_answer)
        except Exception as exc:
            self.runtime.session_event_bus.emit(
                "skill_evolution_failed",
                {
                    "run_id": str((task_state_payload or {}).get("run_id", "")),
                    "error": clip(str(exc), 300),
                },
            )

    def _should_review_turn(self, task_state):
        if not self._background_allowed():
            return False
        status = str(getattr(task_state, "status", "") or "")
        if status != "completed":
            return False
        user_turns = sum(1 for item in self.runtime.session.get("history", []) if item.get("role") == "user")
        tool_steps = int(getattr(task_state, "tool_steps", 0) or 0)
        return user_turns >= TURN_LEARNING_MIN_USER_TURNS or tool_steps >= TURN_LEARNING_MIN_TOOL_STEPS

    def _background_allowed(self):
        if getattr(self.runtime, "depth", 0) > 0:
            return False
        if str(self.runtime.session.get("kind", "")):
            return False
        if getattr(self.runtime, "read_only", False):
            return False
        if getattr(self.runtime.model_client, "outputs", None) is not None:
            return False
        if getattr(self.runtime.model_client, "deterministic_scripted", False):
            return False
        return getattr(self.runtime, "model_client_factory", None) is not None

    def _history_for_review(self, task_id):
        history = list(self.runtime.session.get("history", []))
        selected = [item for item in history if str(item.get("turn_id", "")) == str(task_id)]
        if not selected:
            selected = history[-16:]
        lines = []
        for item in selected:
            role = str(item.get("role", ""))
            event_id = str(item.get("event_id", ""))
            if role == "tool":
                name = str(item.get("name", "tool"))
                content = clip(str(item.get("content", "")), 700)
                lines.append(f"[{event_id}] tool:{name}: {content}")
            else:
                lines.append(f"[{event_id}] {role}: {clip(str(item.get('content', '')), 900)}")
        return clip("\n".join(lines), MAX_REVIEW_HISTORY_CHARS)

    def _run_isolated_agent(self, role, prompt):
        factory = getattr(self.runtime, "model_client_factory", None)
        if factory is None:
            raise RuntimeError("skill evolution requires a model_client_factory")
        from .runtime import BunnyByte

        flags = dict(getattr(self.runtime, "feature_flags", {}) or {})
        flags["skill_evolution"] = False
        flags["skill_curator"] = False
        child = BunnyByte(
            model_client=factory(),
            workspace=WorkspaceContext.build(self.runtime.root, repo_root_override=self.runtime.root),
            session_store=self.runtime.session_store,
            run_store=self.runtime.run_store,
            approval_policy="never",
            max_steps=2,
            max_new_tokens=getattr(self.runtime, "max_new_tokens", None),
            depth=getattr(self.runtime, "depth", 0) + 1,
            max_depth=getattr(self.runtime, "max_depth", 1),
            read_only=True,
            secret_env_names=getattr(self.runtime, "secret_env_names", set()),
            shell_env_allowlist=getattr(self.runtime, "shell_env_allowlist", ()),
            feature_flags=flags,
            memory_dir=getattr(self.runtime, "memory_dir", None),
            auto_dream=False,
            model_client_factory=factory,
            sandbox_config=getattr(self.runtime, "sandbox_config", None),
            session={
                "id": f"{self.runtime.session['id']}-internal-skill-evolution-{role}-{_compact_timestamp()}",
                "created_at": now(),
                "topic": f"# Internal skill evolution: {role}",
                "workspace_root": self.runtime.workspace.repo_root,
                "history": [],
                "kind": "internal_skill_evolution",
            },
        )
        if getattr(self.runtime, "cost_meter", None) is not None:
            child.cost_meter = self.runtime.cost_meter.background(child)  # 后台评审计入会话花费，不占用户这一轮的预算
        answer = child.ask(prompt)
        return answer, child.session["id"]

    def _proposal_prompt(self, task_state, final_answer, history):
        return (
            "You are BunnyByte's skill self-evolution proposal agent.\n"
            "Decide whether the completed conversation contains a reusable workflow worth turning into a project skill.\n\n"
            "Hard gates:\n"
            "- If the user's intent is not complete, output should_create_skill=false.\n"
            "- Skip one-off tasks, transient environment failures, and lessons that only make sense for this exact session.\n"
            "- A good proposal needs evidence: why this should be a skill, key history snippets, reusable steps, verification, and when NOT to use it.\n"
            "- Prefer class-level skills over narrow bug-specific skills.\n\n"
            "Return exactly one JSON object inside <final>...</final> with this shape:\n"
            "{\n"
            '  "completed": true,\n'
            '  "should_create_skill": true,\n'
            '  "skip_reason": "",\n'
            '  "skill": {"name": "class-level-name", "description": "...", "when_to_use": "...", "not_when_to_use": "...", "steps": ["..."], "verification": ["..."]},\n'
            '  "evidence": {"why": "...", "trigger_signals": ["..."], "key_history": ["quote or paraphrase with event ids"], "reusable_steps": ["..."], "source_event_ids": ["event_..."], "confidence": 0.0}\n'
            "}\n\n"
            f"Task state:\n{json.dumps(task_state, ensure_ascii=False, sort_keys=True)}\n\n"
            f"Final answer:\n{clip(final_answer, 2000)}\n\n"
            f"Relevant history:\n{history}"
        )

    def _review_prompt(self, proposal):
        return (
            "You are BunnyByte's independent skill-review agent. Review the proposed skill using the evidence field.\n"
            "Reject low-necessity, one-off, over-coupled, duplicate, or weak-evidence proposals. Accept only if it is broadly reusable and well-scoped.\n"
            "Return exactly one JSON object inside <final>...</final>:\n"
            '{"decision":"accept|reject|revise","necessity_score":0.0,"reasons":["..."],"risks":["..."],"required_changes":["..."]}\n\n'
            "Existing skills:\n"
            f"{self._skill_landscape()}\n\n"
            "Proposal:\n"
            f"{json.dumps(proposal, ensure_ascii=False, indent=2, sort_keys=True)}"
        )

    def _skill_landscape(self):
        skills = getattr(self.runtime, "skills", {}) or {}
        lines = []
        for skill in skillslib.list_skills(skills):
            lines.append(f"- /{skill.name} ({skill.source}): {skill.description or skill.when_to_use or '-'}")
        return "\n".join(lines) or "- none"

    def _normalize_proposal(self, raw, task_state, final_answer, proposer_session_id):
        raw = raw if isinstance(raw, dict) else {}
        skill = raw.get("skill") if isinstance(raw.get("skill"), dict) else {}
        evidence = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
        proposal_id = f"prop_{_compact_timestamp()}"
        name = _safe_skill_name(skill.get("name") or raw.get("skill_name") or "")
        normalized = {
            "schema_version": SCHEMA_VERSION,
            "proposal_id": proposal_id,
            "status": "pending_review",
            "created_at": now(),
            "session_id": str(self.runtime.session.get("id", "")),
            "run_id": str(task_state.get("run_id", "")),
            "task_id": str(task_state.get("task_id", "")),
            "proposer_session_id": proposer_session_id,
            "completed": bool(raw.get("completed", False)),
            "should_create_skill": bool(raw.get("should_create_skill", False)),
            "skip_reason": str(raw.get("skip_reason", "") or ""),
            "final_answer_preview": clip(final_answer, 500),
            "skill": {
                "name": name,
                "description": str(skill.get("description", "") or "").strip(),
                "when_to_use": str(skill.get("when_to_use", "") or "").strip(),
                "not_when_to_use": str(skill.get("not_when_to_use", "") or "").strip(),
                "steps": _string_list(skill.get("steps")),
                "verification": _string_list(skill.get("verification")),
                "content": str(skill.get("content", "") or "").strip(),
            },
            "evidence": {
                "why": str(evidence.get("why", "") or raw.get("reason", "") or "").strip(),
                "trigger_signals": _string_list(evidence.get("trigger_signals")),
                "key_history": _string_list(evidence.get("key_history")),
                "reusable_steps": _string_list(evidence.get("reusable_steps")),
                "source_event_ids": _string_list(evidence.get("source_event_ids")),
                "confidence": _float(evidence.get("confidence", raw.get("confidence", 0))),
            },
        }
        if not normalized["completed"] or not normalized["should_create_skill"]:
            normalized["should_create_skill"] = False
        return normalized

    def _proposal_quality_errors(self, proposal):
        errors = []
        skill = proposal.get("skill", {})
        evidence = proposal.get("evidence", {})
        name = str(skill.get("name", ""))
        if not SAFE_SKILL_NAME_RE.match(name):
            errors.append("skill name must be lowercase kebab/underscore and 2-64 chars")
        if SPECIFIC_NAME_RE.search(name):
            errors.append("skill name looks too session-specific")
        for field in ("description", "when_to_use", "not_when_to_use"):
            if not str(skill.get(field, "")).strip():
                errors.append(f"missing skill.{field}")
        if len(skill.get("steps", [])) < 2:
            errors.append("skill needs at least two reusable steps")
        if not skill.get("verification"):
            errors.append("skill needs verification guidance")
        if not evidence.get("why"):
            errors.append("missing evidence.why")
        if not evidence.get("key_history"):
            errors.append("missing evidence.key_history")
        if len(evidence.get("reusable_steps", [])) < 2:
            errors.append("missing evidence.reusable_steps")
        if _float(evidence.get("confidence")) < 0.55:
            errors.append("evidence confidence below threshold")
        return errors

    def _normalize_review(self, raw, reviewer_session_id):
        raw = raw if isinstance(raw, dict) else {}
        decision = str(raw.get("decision", "reject") or "reject").strip().lower()
        if decision not in {"accept", "reject", "revise"}:
            decision = "reject"
        return {
            "reviewer_session_id": reviewer_session_id,
            "decision": decision,
            "necessity_score": max(0.0, min(1.0, _float(raw.get("necessity_score", 0)))),
            "reasons": _string_list(raw.get("reasons")),
            "risks": _string_list(raw.get("risks")),
            "required_changes": _string_list(raw.get("required_changes")),
            "reviewed_at": now(),
        }

    def _promote_proposal(self, proposal):
        skill = proposal["skill"]
        name = skill["name"]
        skill_dir = Path(self.runtime.root) / ".bunnybyte" / "skills" / name
        if skill_dir.exists():
            return {
                "promoted": False,
                "reason": "target skill already exists; manual patch review required",
                "target_path": str(skill_dir / "SKILL.md"),
            }
        content = skill.get("content") or self._skill_content_from_proposal(proposal)
        content = self._ensure_frontmatter(content, proposal)
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        references = skill_dir / "references"
        references.mkdir(parents=True, exist_ok=True)
        (references / f"evidence-{proposal['proposal_id']}.md").write_text(
            self._evidence_markdown(proposal),
            encoding="utf-8",
        )
        self._seed_usage_for_promoted_skill(name, skill_dir, proposal)
        if hasattr(self.runtime, "refresh_skills"):
            self.runtime.refresh_skills()
        self.refresh_skill_tree_index()
        self.runtime.session_event_bus.emit(
            "skill_evolution_promoted",
            {"proposal_id": proposal["proposal_id"], "skill": name, "path": str(skill_dir / "SKILL.md")},
        )
        return {"promoted": True, "target_path": str(skill_dir / "SKILL.md")}

    def _skill_content_from_proposal(self, proposal):
        skill = proposal["skill"]
        evidence = proposal["evidence"]
        lines = [
            f"# {skill['name']}",
            "",
            "## When To Use",
            skill["when_to_use"],
            "",
            "## When Not To Use",
            skill["not_when_to_use"],
            "",
            "## Steps",
        ]
        lines.extend(f"{index}. {step}" for index, step in enumerate(skill["steps"], start=1))
        lines.extend(["", "## Verification"])
        lines.extend(f"- {item}" for item in skill["verification"])
        lines.extend(
            [
                "",
                "## Evidence",
                f"- why: {evidence['why']}",
            ]
        )
        for item in evidence.get("trigger_signals", []):
            lines.append(f"- trigger: {item}")
        for item in evidence.get("source_event_ids", []):
            lines.append(f"- source_event_id: {item}")
        for item in evidence.get("key_history", []):
            lines.append(f"- history: {item}")
        lines.append(f"- proposal: {proposal['proposal_id']}")
        return "\n".join(lines).strip() + "\n"

    def _ensure_frontmatter(self, content, proposal):
        metadata, body = skillslib.parse_frontmatter(content)
        skill = proposal["skill"]
        metadata.update(
            {
                "name": skill["name"],
                "description": skill["description"],
                "when_to_use": skill["when_to_use"],
                "not_when_to_use": skill["not_when_to_use"],
                "agent_created": True,
                "created_by": "bunnybyte-skill-evolution",
                "source_proposal_id": proposal["proposal_id"],
                "created_at": proposal["created_at"],
            }
        )
        frontmatter = ["---"]
        for key, value in metadata.items():
            frontmatter.append(f"{str(key).replace('_', '-')}: {_frontmatter_value(value)}")
        frontmatter.append("---")
        return "\n".join(frontmatter) + "\n" + body.strip() + "\n"

    def _evidence_markdown(self, proposal):
        evidence = proposal["evidence"]
        lines = [
            f"# Evidence for {proposal['skill']['name']}",
            "",
            f"- proposal_id: {proposal['proposal_id']}",
            f"- source_session_id: {proposal['session_id']}",
            f"- run_id: {proposal['run_id']}",
            f"- task_id: {proposal['task_id']}",
            f"- confidence: {evidence.get('confidence', 0)}",
            "",
            "## Why",
            evidence.get("why", ""),
            "",
            "## Key History",
        ]
        lines.extend(f"- {item}" for item in evidence.get("key_history", []))
        lines.extend(["", "## Trigger Signals"])
        lines.extend(f"- {item}" for item in evidence.get("trigger_signals", []))
        lines.extend(["", "## Source Event IDs"])
        lines.extend(f"- {item}" for item in evidence.get("source_event_ids", []))
        lines.extend(["", "## Reusable Steps"])
        lines.extend(f"- {item}" for item in evidence.get("reusable_steps", []))
        return "\n".join(lines).strip() + "\n"

    def _seed_usage_for_promoted_skill(self, name, skill_dir, proposal):
        data = self._load_json(self.usage_path, {})
        data[name] = {
            "name": name,
            "source": "project",
            "skill_root": str(skill_dir),
            "agent_created": True,
            "created_at": proposal["created_at"],
            "use_count": 0,
            "last_used_at": "",
            "source_proposal_id": proposal["proposal_id"],
        }
        self._write_json(self.usage_path, data)

    def _build_curator_report(self, now_dt, dry_run=False):
        usage = self._load_json(self.usage_path, {})
        skills = self._project_skill_candidates()
        actions = []
        for item in skills:
            name = item["name"]
            row = dict(usage.get(name, {}) or {})
            created_at = _parse_time(row.get("created_at") or item.get("created_at")) or item["mtime"]
            last_used = _parse_time(row.get("last_used_at"))
            use_count = int(row.get("use_count", 0) or 0)
            anchor = last_used or created_at
            if use_count == 0 and now_dt - anchor >= timedelta(days=LOW_USAGE_AFTER_DAYS):
                if item.get("agent_created"):
                    actions.append(
                        {
                            "action": "delete",
                            "skill": name,
                            "status": "pending_user_approval",
                            "approval_required": True,
                            "reason": f"agent-created skill has no recorded usage for {LOW_USAGE_AFTER_DAYS}+ days",
                            "path": item["path"],
                        }
                    )
                else:
                    actions.append(
                        {
                            "action": "low_usage_review",
                            "skill": name,
                            "status": "needs_review",
                            "approval_required": False,
                            "reason": f"project skill has no recorded usage for {LOW_USAGE_AFTER_DAYS}+ days; review before changing",
                            "path": item["path"],
                        }
                    )
        for left, right in _overlap_pairs(skills):
            actions.append(
                {
                    "action": "merge_review",
                    "skills": [left["name"], right["name"]],
                    "status": "needs_review",
                    "approval_required": False,
                    "reason": "descriptions or names overlap; consider a broader umbrella skill",
                }
            )
        maintenance_id = f"maint_{_compact_timestamp()}"
        return {
            "schema_version": SCHEMA_VERSION,
            "maintenance_id": maintenance_id,
            "created_at": now_dt.isoformat(),
            "dry_run": bool(dry_run),
            "summary": f"{len(actions)} maintenance action(s) staged",
            "actions": actions,
        }

    def _maintenance_report_path(self, maintenance_id):
        raw = str(maintenance_id or "").strip()
        if not raw:
            raise ValueError("maintenance id is required")
        name = raw if raw.endswith(".json") else f"{raw}.json"
        path = self.maintenance_dir / Path(name).name
        if not path.exists():
            raise ValueError(f"maintenance report not found: {raw}")
        return path

    def _maintenance_action(self, report, action_number):
        actions = report.get("actions") if isinstance(report.get("actions"), list) else []
        try:
            index = int(action_number) - 1
        except (TypeError, ValueError):
            raise ValueError("action number must be an integer") from None
        if index < 0 or index >= len(actions):
            raise ValueError("maintenance action not found")
        action = actions[index]
        if not isinstance(action, dict):
            raise ValueError("maintenance action is malformed")
        return action

    def _project_skill_candidates(self):
        skill_roots = [
            Path(self.runtime.root) / "skills",
            Path(self.runtime.root) / ".bunnybyte" / "skills",
        ]
        items = []
        for skills_dir in skill_roots:
            if not skills_dir.exists():
                continue
            for path in sorted(skills_dir.glob("*/SKILL.md")):
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                metadata, _body = skillslib.parse_frontmatter(text)
                agent_created = _truthy(metadata.get("agent_created")) or str(metadata.get("created_by", "")) == "bunnybyte-skill-evolution"
                stat = path.stat()
                items.append(
                    {
                        "name": _safe_skill_name(metadata.get("name") or path.parent.name),
                        "description": str(metadata.get("description", "") or ""),
                        "when_to_use": str(metadata.get("when_to_use", "") or ""),
                        "created_at": str(metadata.get("created_at", "") or ""),
                        "agent_created": bool(agent_created),
                        "path": str(path),
                        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                    }
                )
        return [item for item in items if item["name"]]

    def _feature_enabled(self, name):
        enabled = getattr(self.runtime, "feature_enabled", None)
        if callable(enabled):
            return bool(enabled(name))
        return bool(getattr(self.runtime, "feature_flags", {}).get(name, True))

    def _parse_json_answer(self, answer):
        text = str(answer or "").strip()
        if text.startswith("<final>") and text.endswith("</final>"):
            text = text[len("<final>") : -len("</final>")].strip()
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _save_proposal(self, proposal):
        self._write_json(self.proposals_dir / f"{proposal['proposal_id']}.json", proposal)

    def _load_json(self, path, default):
        path = Path(path)
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

    def _write_json(self, path, payload):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)


def _safe_skill_name(value):
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-_")
    return text[:64]


def _string_list(value):
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_time(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _compact_timestamp():
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")


def _frontmatter_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).replace("\n", " ").strip()
    if ":" in text or text.startswith(("{", "[", "#")):
        return json.dumps(text, ensure_ascii=False)
    return text


def _truthy(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _overlap_pairs(skills):
    pairs = []
    for index, left in enumerate(skills):
        left_tokens = _tokens(left["name"] + " " + left.get("description", "") + " " + left.get("when_to_use", ""))
        for right in skills[index + 1 :]:
            right_tokens = _tokens(right["name"] + " " + right.get("description", "") + " " + right.get("when_to_use", ""))
            if not left_tokens or not right_tokens:
                continue
            overlap = len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))
            same_prefix = left["name"].split("-")[0] == right["name"].split("-")[0]
            if overlap >= 0.5 or same_prefix:
                pairs.append((left, right))
    return pairs


def _tokens(text):
    stop = {"the", "and", "for", "with", "this", "that", "skill", "use", "when"}
    return {token for token in re.findall(r"[a-z0-9]+", str(text).lower()) if token not in stop and len(token) > 2}
