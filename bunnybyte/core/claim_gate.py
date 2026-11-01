"""把 claim_check 接到回合收尾：准备本回合的记录、判断哪些路径看不清，结果写进 task_state / trace。"""

import os

from .claim_check import claims_notice, git_history_text, reconcile
from .shadow_snapshots import ShadowError, ShadowStore
from .tool_loop_guard import current_turn_history
from .turn_diff import current_turn_entry
from .verification import STATUS_PASSED
from .workspace import IGNORED_PATH_NAMES, clip


def _workers_running(agent):
    items = getattr(getattr(agent, "worker_manager", None), "state", {}) or {}
    return any(item.get("status") in {"running", "stopping"} for item in items.get("items", []))


class _Visibility:
    """惰性回答两个问题：快照里本回合还改了什么；某个路径是不是我们看不清的。"""

    def __init__(self, agent, items):
        self.agent = agent
        self.store = ShadowStore.for_root(agent.root)
        self.entry = current_turn_entry(agent)
        self.history_text = git_history_text(items)
        self._earlier = None

    def turn_paths(self):
        if self.entry is None:
            return []
        try:
            after = self.store.snapshot() or self.entry.get("after_tree")  # 子 agent 和 shell 的写入现在重新拍一次
            return self.store.diff_paths(self.entry.get("before_tree"), after)
        except (ShadowError, OSError):
            return []

    def earlier_paths(self):
        if self._earlier is None:
            self._earlier = set()
            log = [item for item in self.agent.session.get("undo_log") or [] if item is not self.entry]
            for item in log:
                try:
                    self._earlier.update(self.store.diff_paths(item["before_tree"], item["after_tree"]))
                except (ShadowError, OSError, KeyError):
                    continue
        return self._earlier

    def quick_unknown(self, path):
        """不起 git 进程就能判断的：`.bunnybyte/` 这类快照看不见的、工作区外的、git 历史里出现过的、文件不在了的。"""
        if any(part in IGNORED_PATH_NAMES for part in path.split("/")):
            return True
        if path in self.history_text:
            return True
        root = str(self.agent.root)
        target = os.path.normpath(os.path.join(root, path))
        if os.path.commonpath([root, target]) != root:
            return True
        try:
            return not os.path.exists(target)  # 本回合里建了又删的临时脚本，或者只给了文件名、不知道在哪个目录
        except (OSError, ValueError):
            return True

    def unknown(self, path):
        """要读快照的：更早回合改过的、被 .gitignore 忽略的。"""
        if any(item == path or item.endswith("/" + path) for item in self.earlier_paths()):
            return True
        try:
            return bool(self.store.ignored_paths([path]))
        except (ShadowError, OSError):
            return True


def claims_line(agent, task_state, final, *, skip_tests=False):
    """主 agent 的回合末尾对账一次；没有可对账的声明返回空串。验证闸门关着也照样运行。

    skip_tests：旧的“声称通过却没有任何回执”警告已经说过测试的事，这里不再重复。
    """
    if agent.depth != 0:
        return ""
    try:
        return _claims_line(agent, task_state, final, skip_tests)
    except Exception as exc:  # 对账只是提示，出错也不能让回合收尾半途而废
        agent.emit_trace(task_state, "claim_check_failed", {"error": clip(f"{type(exc).__name__}: {exc}", 300)})
        return ""


def _claims_line(agent, task_state, final, skip_tests):
    items = current_turn_history(agent.session.get("history", []))
    visibility = _Visibility(agent, items)
    # 快照不可用时分不清“本回合改的”和“以前改的”；子任务还在跑时它的改动还没落地：这两种只对账测试声明
    check_changes = visibility.store.available and not _workers_running(agent)
    result = reconcile(
        final,
        items,
        list(task_state.changed_paths),
        gate_passed=skip_tests or task_state.verification.get("status") == STATUS_PASSED,
        quick_unknown=visibility.quick_unknown,
        unknown=visibility.unknown,
        more_changed=visibility.turn_paths,
        check_changes=check_changes,
        require_run=bool(task_state.changed_paths) and not getattr(getattr(agent, "verify_config", None), "enabled", False),
        root=str(agent.root),
    )
    if result is None:
        return ""
    task_state.verification["claims"] = result
    agent.emit_trace(task_state, "claim_check", dict(result))
    return claims_notice(result)
