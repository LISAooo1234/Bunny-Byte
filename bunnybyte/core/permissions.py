"""Runtime permission decisions for tool execution."""

from dataclasses import dataclass, replace
from pathlib import Path

from .control_plane import guard_write
from .dependency_guard import guard_permission


@dataclass(frozen=True)
class PermissionDecision:
    decision: str
    reason: str
    security_event_type: str = ""
    message: str = ""  # 非空时作为拒绝时给模型的错误文字
    notes: tuple = ()  # 放行时附在工具结果后面的提示（依赖守卫）

    @classmethod
    def allow(cls, reason):
        return cls("allow", reason)

    @classmethod
    def deny(cls, reason, security_event_type="", message=""):
        return cls("deny", reason, security_event_type, message)

    @property
    def allowed(self):
        return self.decision == "allow"


class PermissionChecker:
    def __init__(self, runtime):
        self.runtime = runtime

    def check(self, tool, args):
        args = args or {}
        profile = self.runtime.active_tool_profile
        if not profile.allows(tool.name):
            if profile.name == "plan":
                return PermissionDecision.deny("plan_mode_tool_not_allowed", "plan_mode_write_guard")
            return PermissionDecision.deny("tool_not_allowed")

        if self.runtime.runtime_mode == "plan":
            return self._check_plan(tool, args)
        # 控制面守卫和依赖守卫排在 write_scope 和只读判断之前：worker 改 AGENTS.md / requirements.txt 也要过它们
        control, control_notes = guard_write(self.runtime, tool, args, PermissionDecision.allow, PermissionDecision.deny)
        if control is not None:  # 用户确认过也不能越过只读和 write_scope
            if control.allowed and (self.runtime.read_only or getattr(self.runtime, "write_scope", ())):
                return self._check_tool(tool, args)
            return control
        verdict = guard_permission(self.runtime, tool, args, PermissionDecision.allow, PermissionDecision.deny)
        decision = verdict.decision or self._check_tool(tool, verdict.approval_args)
        notes = tuple(control_notes) + tuple(verdict.notes)
        return replace(decision, notes=notes) if decision.allowed and notes else decision

    def _check_tool(self, tool, args):
        if tool.name in {"write_file", "patch_file"} and getattr(self.runtime, "write_scope", ()):
            return self._check_write_scope(tool, args)
        if tool.read_only:
            return PermissionDecision.allow("read_only")
        if self.runtime.read_only:
            return PermissionDecision.deny("approval_denied", "read_only_block")
        if self.runtime.approval_policy == "auto":
            return PermissionDecision.allow("approval_auto")
        if self.runtime.approval_policy == "never":
            return PermissionDecision.deny("approval_denied", "approval_denied")
        if self.runtime.approve(tool.name, args):
            return PermissionDecision.allow("approval_prompt")
        return PermissionDecision.deny("approval_denied", "approval_denied")

    def _check_plan(self, tool, args):
        if tool.read_only:
            return PermissionDecision.allow("plan_read_only")
        if tool.name not in {"write_file", "patch_file"}:
            return PermissionDecision.deny("plan_mode_tool_not_allowed", "plan_mode_write_guard")
        requested = self.runtime.path(args.get("path", ""))
        active = self.runtime.path(self.runtime.plan_mode.plan_path)
        if Path(requested) != Path(active):
            return PermissionDecision.deny("plan_mode_path_mismatch", "plan_mode_write_guard")
        return PermissionDecision.allow("plan_artifact_write")

    def _check_write_scope(self, tool, args):
        requested = self.runtime.path(args.get("path", ""))
        for raw_scope in self.runtime.write_scope:
            scope = self.runtime.path(raw_scope)
            try:
                requested.relative_to(scope)
                return PermissionDecision.allow("write_scope")
            except ValueError:
                continue
        return PermissionDecision.deny("write_scope_mismatch", "write_scope_guard")
