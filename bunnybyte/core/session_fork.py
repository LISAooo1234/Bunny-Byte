"""Session forking helpers."""

from __future__ import annotations

import copy
import uuid
from datetime import datetime

from ..features import memory as memorylib
from .session_lifecycle import _rebind, _shutdown_workers
from .session_topics import DEFAULT_SESSION_TOPIC, topic_from_history
from .workspace import now


class SessionForkError(ValueError):
    """Raised when a session cannot be forked from the requested point."""


def fork_runtime_session(runtime, target="latest"):
    """Create and switch to a new session forked from a history event.

    The parent session remains append-only. The child gets a truncated history up
    to the selected message and restores the workspace to the checkpoint that was
    captured at, or before, that point in the conversation.
    """

    runtime.ensure_session_started()
    parent = copy.deepcopy(runtime.session)
    history = list(parent.get("history", []) or [])
    if not history:
        raise SessionForkError("current session has no history to fork")

    index = _resolve_history_index(history, target)
    target_item = history[index]
    checkpoint = _checkpoint_for_history_index(parent, history, index)
    restored = False
    restore_warning = ""
    if checkpoint:
        restored = runtime.restore_checkpoint(str(checkpoint.get("checkpoint_id", "")))
        if not restored:
            restore_warning = "workspace snapshot unavailable; forked conversation history only"
    else:
        restore_warning = "checkpoint unavailable; forked conversation history only"

    child_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-fork-" + uuid.uuid4().hex[:6]
    child_history = copy.deepcopy(history[: index + 1])
    child = copy.deepcopy(parent)
    child["id"] = child_id
    child["created_at"] = now()
    child["history"] = child_history
    child["workspace_root"] = runtime.workspace.repo_root
    child["topic"] = _fork_topic(parent, child_history)
    child["parent_session_id"] = parent.get("id", "")
    child["fork"] = {
        "parent_session_id": parent.get("id", ""),
        "forked_from_event_id": target_item.get("event_id", ""),
        "forked_from_turn_id": target_item.get("turn_id", ""),
        "forked_from_run_id": target_item.get("run_id", ""),
        "forked_from_role": target_item.get("role", ""),
        "forked_from_checkpoint_id": checkpoint.get("checkpoint_id", "") if checkpoint else "",
        "workspace_restored": restored,
        "restore_warning": restore_warning,
        "created_at": child["created_at"],
    }
    child["memory"] = copy.deepcopy(parent.get("memory") or memorylib.default_memory_state())
    child["read_ledger"] = copy.deepcopy(parent.get("read_ledger") or {})
    child["workers"] = {"items": []}
    child["todos"] = {"items": []}
    child["runtime_mode"] = {"mode": "default"}
    child.pop("_manual_turn_id", None)

    checkpoints = child.setdefault("checkpoints", {})
    if not isinstance(checkpoints, dict):
        checkpoints = {"current_id": "", "items": {}}
        child["checkpoints"] = checkpoints
    checkpoints.setdefault("items", {})
    checkpoints["current_id"] = checkpoint.get("checkpoint_id", "") if checkpoint and restored else ""

    _shutdown_workers(runtime)
    runtime.session = child
    runtime._lazy_session_requested = False
    _rebind(runtime, emit_started=True)
    runtime.session_event_bus.emit(
        "session_fork_created",
        {
            "parent_session_id": parent.get("id", ""),
            "forked_from_event_id": child["fork"].get("forked_from_event_id", ""),
            "forked_from_turn_id": child["fork"].get("forked_from_turn_id", ""),
            "forked_from_run_id": child["fork"].get("forked_from_run_id", ""),
            "forked_from_checkpoint_id": child["fork"].get("forked_from_checkpoint_id", ""),
            "workspace_restored": restored,
            "restore_warning": restore_warning,
        },
    )
    runtime.session_path = runtime.session_store.save(runtime.session)
    return dict(child["fork"], session_id=child_id)


def _resolve_history_index(history, target):
    value = str(target or "latest").strip()
    if not value or value == "latest":
        return len(history) - 1
    if value.isdigit():
        index = int(value) - 1
        if 0 <= index < len(history):
            return index
    for index, item in enumerate(history):
        if value == str(item.get("event_id", "")):
            return index
    turn_matches = [
        index for index, item in enumerate(history) if value == str(item.get("turn_id", ""))
    ]
    if turn_matches:
        return turn_matches[-1]
    raise SessionForkError(f"fork point not found: {value}")


def _checkpoint_for_history_index(session, history, index):
    checkpoints = (session.get("checkpoints", {}) or {}).get("items", {}) or {}
    if not checkpoints:
        return None
    event_order = {str(item.get("event_id", "")): position for position, item in enumerate(history)}
    candidates = []
    for checkpoint in checkpoints.values():
        after_event_id = str(checkpoint.get("after_event_id", ""))
        if after_event_id and after_event_id in event_order and event_order[after_event_id] <= index:
            candidates.append((event_order[after_event_id], str(checkpoint.get("created_at", "")), checkpoint))
    if candidates:
        return sorted(candidates, key=lambda item: (item[0], item[1]))[-1][2]
    current_id = str((session.get("checkpoints", {}) or {}).get("current_id", ""))
    if current_id and current_id in checkpoints and index == len(history) - 1:
        return checkpoints[current_id]
    return None


def _fork_topic(parent, child_history):
    base = str(parent.get("topic", "") or "").strip()
    if not base or base == DEFAULT_SESSION_TOPIC:
        base = topic_from_history(child_history)
    if not base or base == DEFAULT_SESSION_TOPIC:
        return "Forked session"
    return f"Fork: {base}"[:80]
