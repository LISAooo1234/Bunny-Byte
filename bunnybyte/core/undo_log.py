"""Per-turn undo journal built from workspace snapshots.

Every snapshot taken while a turn is running is reported to ``observe_snapshot``.
The first one in a turn is the pre-turn state, the latest one is the post-turn
state. ``undo_latest`` reverts exactly the files that differ between the two,
and refuses (without writing anything) if any of them changed again afterwards.
"""

from __future__ import annotations

from .shadow_snapshots import ShadowError, ShadowStore
from .workspace import clip, now

MAX_ENTRIES = 30


class UndoRefused(Exception):
    """The undo cannot run; the message is shown to the user."""


def observe_snapshot(agent, tree, adopt_paths=()):
    turn_id = str(getattr(agent, "current_turn_id", "") or "")
    session = getattr(agent, "session", None)
    if not turn_id or not isinstance(session, dict):
        return
    log = session.setdefault("undo_log", [])
    last = log[-1] if log else None
    if last and last.get("turn_id") == turn_id:
        if adopt_paths:
            _adopt_preexisting(agent, last, tree, adopt_paths)
        last["after_tree"] = tree
        last["head_after"] = _git_head(agent.root)
        return
    if last and last.get("before_tree") == last.get("after_tree"):
        log.pop()
    state = getattr(agent, "current_task_state", None)
    head = _git_head(agent.root)
    log.append(
        {
            "turn_id": turn_id,
            "run_id": str(getattr(agent, "current_run_id", "") or ""),
            "request": clip(str(getattr(state, "user_request", "") or ""), 80),
            "created_at": now(),
            "before_tree": tree,
            "after_tree": tree,
            "head_before": head,
            "head_after": head,
            "status": "applied",
        }
    )
    del log[:-MAX_ENTRIES]


def _adopt_preexisting(agent, entry, tree, paths):
    """Files that already existed untracked (gitignored) when a write tool started belong to the pre-turn state.

    Without this the first force-tracked write of an ignored file such as .env would look like "created by
    the turn", and undoing the turn would delete a file the user had all along.
    """
    store = ShadowStore.for_root(agent.root)
    try:
        entries = store.new_entries(entry["after_tree"], tree, list(paths))
        if entries:
            entry["before_tree"] = store.tree_with_entries(entry["before_tree"], entries)
    except ShadowError:
        return


def _git_head(root):
    """Current HEAD commit of the user's repository, read straight from .git (no subprocess)."""
    try:
        git_dir = root / ".git"
        if git_dir.is_file():
            pointer = git_dir.read_text(encoding="utf-8").strip()
            if not pointer.startswith("gitdir:"):
                return ""
            git_dir = (root / pointer[len("gitdir:"):].strip()).resolve()
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head
        ref = head[4:].strip()
        ref_file = git_dir / ref
        if ref_file.is_file():
            return ref_file.read_text(encoding="utf-8").strip()
        common = git_dir / "commondir"
        base = (git_dir / common.read_text(encoding="utf-8").strip()).resolve() if common.is_file() else git_dir
        if (base / ref).is_file():
            return (base / ref).read_text(encoding="utf-8").strip()
        for line in (base / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return line.split(" ", 1)[0]
    except (OSError, ValueError):
        pass
    return ""


def undo_entries(agent):
    """Journal entries that actually changed files, oldest first."""
    log = agent.session.get("undo_log") or []
    return [item for item in log if item.get("before_tree") != item.get("after_tree")]


def entry_paths(agent, entry):
    store = ShadowStore.for_root(agent.root)
    return store.diff_paths(entry["before_tree"], entry["after_tree"])


def undo_latest(agent, force=False):
    """Revert the newest applied turn. Returns a dict describing what happened."""
    for item in agent.worker_manager.state.get("items", []):
        if item.get("status") in {"running", "stopping"}:
            raise UndoRefused("还有子任务在运行，先等它结束或用 /agents 停止它，再撤销。")
    store = ShadowStore.for_root(agent.root)
    candidates = [item for item in undo_entries(agent) if item.get("status") != "undone"]
    if not candidates:
        if not store.available:
            raise UndoRefused(f"撤销依赖 git 快照，当前不可用：{store.disabled_reason}")
        raise UndoRefused("没有可撤销的文件改动。")
    entry = candidates[-1]
    if not store.available:
        raise UndoRefused(f"撤销依赖 git 快照，当前不可用：{store.disabled_reason}")
    if not (store.has_tree(entry["before_tree"]) and store.has_tree(entry["after_tree"])):
        raise UndoRefused("这一轮的快照已经丢失（.bunnybyte/shadow.git 被清理过），无法撤销。")
    try:
        result = store.apply(entry["after_tree"], entry["before_tree"], force=force)
    except ShadowError as exc:
        raise UndoRefused(f"撤销失败，没有改动任何文件：{clip(str(exc), 200)}") from exc
    outcome = {
        "entry": entry,
        "applied": result.applied,
        "conflicts": result.conflicts,
        "failed": result.failed,
        "head_changed": bool(entry.get("head_before") != entry.get("head_after")),
        "status": "conflict" if result.conflicts and not force else "undone",
    }
    if outcome["status"] == "conflict":
        return outcome
    entry["status"] = "undone"
    entry["undone_at"] = now()
    _tell_runtime(agent, entry, result)
    return outcome


def _tell_runtime(agent, entry, result):
    ledger = getattr(agent, "read_ledger", None)
    for path in result.applied:
        if ledger is not None and hasattr(ledger, "invalidate"):
            try:
                ledger.invalidate(path)
            except Exception:
                pass
    paths = "、".join(result.applied[:12]) + ("…" if len(result.applied) > 12 else "")
    agent.record(
        {
            "role": "user",
            "content": (
                f"（系统提示：用户执行了 /undo，已把「{entry.get('request', '')}」那一轮对这些文件的改动还原："
                f"{paths}。文件现在是改动之前的内容，请重新读取后再继续，不要假设之前写入的内容还在。）"
            ),
            "created_at": now(),
            "source": "system_note",
        }
    )
    agent.session_event_bus.emit(
        "workspace_undone",
        {"turn_id": entry.get("turn_id", ""), "run_id": entry.get("run_id", ""), "paths": result.applied[:50]},
    )
    agent.refresh_prefix(force=True)
