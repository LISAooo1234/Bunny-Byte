"""运行结束后回答“这次改动有没有被真实验证过”，供无人值守模式的退出码使用。"""

from .artifacts import build_artifact_graph
from .verification import STATUS_FAILED, STATUS_PASSED, STATUS_TIMEOUT, changes_fingerprint

VERDICT_NOT_NEEDED = "not_needed"
VERDICT_VERIFIED = "verified"
VERDICT_FAILED = "failed"
VERDICT_UNVERIFIED = "unverified"


def _docs_only(root, paths):
    docs = build_artifact_graph(root, paths)["categories"]["docs"]
    return all(path in docs for path in paths)


def verification_verdict(agent, task_state):
    """没改文件或只改了文档 -> not_needed；最近一次通过的回执覆盖当前文件内容 -> verified；
    最近一次是失败/超时 -> failed；其余（没跑、被拒绝、不可用、没收集到测试、回执已过期）-> unverified。"""
    paths = list(task_state.changed_paths)
    if not paths or _docs_only(agent.root, paths):
        return VERDICT_NOT_NEEDED
    state = task_state.verification or {}
    status = state.get("status", "")
    if status in (STATUS_FAILED, STATUS_TIMEOUT):
        return VERDICT_FAILED
    fresh = state.get("fingerprint") == changes_fingerprint(agent.root, paths)
    if status == STATUS_PASSED and fresh:
        return VERDICT_VERIFIED
    return VERDICT_UNVERIFIED
