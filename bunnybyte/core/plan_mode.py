"""Plan mode policy for sessions."""

import re
from pathlib import Path

from .session_topics import DEFAULT_SESSION_TOPIC, derive_session_topic


def _slug(value):
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", str(value).strip().lower()).strip("-")
    return slug or "plan"


class PlanModeManager:
    def __init__(self, runtime):
        self.runtime = runtime

    @property
    def state(self):
        return self.runtime.session.setdefault("runtime_mode", {"mode": "default"})

    @property
    def mode(self):
        return str(self.state.get("mode", "default") or "default")

    @property
    def plan_path(self):
        return str(self.state.get("plan_path", "") or "")

    def enter(self, topic, path=None):
        self.runtime.ensure_session_started()
        plan_path = _plan_path(
            topic,
            path,
            workspace_root=self.runtime.root,
            artifact_root=self.runtime.workspace.cwd,
        )
        if self.runtime.session_topic == DEFAULT_SESSION_TOPIC:
            self.runtime.session["topic"] = derive_session_topic(topic)
        self.runtime.session["runtime_mode"] = {
            "mode": "plan",
            "topic": str(topic or ""),
            "plan_path": plan_path,
        }
        self.runtime.set_tool_profile("plan")
        self.runtime.session_path = self.runtime.session_store.save(
            self.runtime.session
        )
        self.runtime.refresh_prefix(force=True)
        self.runtime.session_event_bus.emit(
            "runtime_mode_changed",
            {"mode": "plan", "plan_path": plan_path, "topic": str(topic or "")},
        )
        return plan_path

    def exit(self):
        if self.runtime.is_pending_session and self.mode == "default":
            return
        self.runtime.ensure_session_started()
        previous = dict(self.state)
        self.runtime.session["runtime_mode"] = {"mode": "default"}
        self.runtime.set_tool_profile("default")
        self.runtime.session_path = self.runtime.session_store.save(
            self.runtime.session
        )
        self.runtime.refresh_prefix(force=True)
        self.runtime.session_event_bus.emit(
            "runtime_mode_changed",
            {
                "mode": "default",
                "previous_mode": previous.get("mode", "default"),
                "plan_path": previous.get("plan_path", ""),
            },
        )

    def can_finish(self):
        if self.mode != "plan":
            return True
        path = self.runtime.path(self.plan_path)
        return path.is_file() and bool(path.read_text(encoding="utf-8").strip())

    def final_notice(self):
        return f"Plan mode requires writing the active plan artifact before final answer: {self.plan_path}"

    def prompt_text(self):
        if self.mode != "plan":
            return ""
        return (
            "Runtime mode: plan\n"
            f"- Active plan artifact: {self.plan_path}\n"
            "- Goal: collaboratively create an execution plan, then guide execution with visible progress.\n"
            "- First inspect enough context to understand the task. If requirements, risk tolerance, scope, or implementation preference materially affect the plan, use ask_user before committing to a plan.\n"
            "- Use todo_add/todo_update/todo_list as the progress ledger. Create concrete tasks before or while drafting the plan, keep exactly one task in_progress while executing, and mark tasks done as they complete.\n"
            "- Write the active plan artifact with: objective, assumptions, user choices, steps, validation, risks/rollback, and open questions.\n"
            "- The active plan artifact path is relative to the current working directory artifact root, not relative to the BunnyByte source directory.\n"
            "- You may inspect files, but writes must target only the active plan artifact until the user approves execution or exits plan mode.\n"
            "- You may launch Explore subagents, but not write-capable worker subagents.\n"
            "- If execution begins, keep progress updated and ask_user again when a non-obvious choice or blocker appears.\n"
            "- Return a final answer only after the active plan artifact has been written and the progress ledger reflects the current state."
        )


PlanModeController = PlanModeManager


_PLAN_DIR_MARKER = "/.bunnybyte/plans/"


def _plan_path(topic, path=None, *, workspace_root=None, artifact_root=None):
    artifact_prefix = _artifact_prefix(workspace_root, artifact_root)
    if path:
        value = str(path).strip()
        # 模型有时给绝对路径，如 /workspace/repo/.bunnybyte/plans/foo；自动把它相对化。
        if value.startswith("/") and _PLAN_DIR_MARKER in value:
            value = value[value.index(_PLAN_DIR_MARKER) + 1 :]
        if value.startswith("./"):
            value = value[2:]
        if artifact_prefix and value.startswith(f"{artifact_prefix}.bunnybyte/plans/"):
            value = value[len(artifact_prefix) :]
    else:
        value = f"{artifact_prefix}.bunnybyte/plans/{_slug(topic)}-plan.md"
    if (
        not value.startswith(f"{artifact_prefix}.bunnybyte/plans/")
        or value.endswith("/")
        or ".." in value.split("/")
    ):
        raise ValueError(
            f"plan path must stay under {artifact_prefix}.bunnybyte/plans/"
        )
    return value


def _artifact_prefix(workspace_root, artifact_root):
    if not workspace_root or not artifact_root:
        return ""
    try:
        root = Path(workspace_root).resolve()
        artifact = Path(artifact_root).resolve()
        relative = artifact.relative_to(root)
    except Exception:
        return ""
    value = relative.as_posix()
    if value in {"", "."}:
        return ""
    return value.rstrip("/") + "/"
