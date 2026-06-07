"""Repeated tool-call guardrails."""

FILE_MUTATION_TOOLS = {"write_file", "patch_file"}
FILE_READ_TOOLS = {"read_file"}


def is_repeated_tool_call(history, name, args, read_ledger=None):
    current_turn = _current_turn_history(history)
    tool_events = [
        (index, item)
        for index, item in enumerate(current_turn)
        if item.get("role") == "tool"
    ]
    matches = [
        (index, item)
        for index, item in tool_events
        if item.get("name") == name and item.get("args") == args
    ]
    if name in FILE_MUTATION_TOOLS:
        if not matches:
            return False
        last_index, last_match = matches[-1]
        return not _failed_file_write_retry_is_now_informed(
            current_turn, last_index, last_match
        )
    if name in FILE_READ_TOOLS:
        if read_ledger is not None and read_ledger.covered(args):
            return True
        if not matches:
            return False
        last_index, _ = matches[-1]
        return not _file_was_mutated_after(current_turn, name, args, last_index)
    return len(matches) >= 2


def repeated_tool_call_metadata(tool):
    return {
        "tool_status": "rejected",
        "tool_error_code": "repeated_identical_call",
        "security_event_type": "",
        "risk_level": "high" if tool.risky else "low",
        "read_only": tool.read_only,
        "affected_paths": [],
        "workspace_changed": False,
        "diff_summary": [],
    }


def _failed_file_write_retry_is_now_informed(current_turn, last_index, last_match):
    content = str(last_match.get("content", ""))
    if not content.startswith("error:"):
        return False
    path = str((last_match.get("args") or {}).get("path", ""))
    if not path:
        return False
    for item in current_turn[last_index + 1 :]:
        if item.get("role") != "tool" or item.get("name") != "read_file":
            continue
        args = item.get("args") or {}
        if (
            str(args.get("path", "")) == path
            and not str(item.get("content", "")).startswith("error:")
        ):
            return True
    return False


def _file_was_mutated_after(current_turn, name, args, last_index):
    path = str((args or {}).get("path", "")).strip()
    if not path:
        return False
    for item in current_turn[last_index + 1 :]:
        if item.get("role") != "tool" or item.get("name") not in FILE_MUTATION_TOOLS:
            continue
        item_args = item.get("args") or {}
        if str(item_args.get("path", "")).strip() == path:
            return True
    return False


def _current_turn_history(history):
    history = list(history)
    for index in range(len(history) - 1, -1, -1):
        if history[index].get("role") == "user":
            return history[index + 1 :]
    return history
