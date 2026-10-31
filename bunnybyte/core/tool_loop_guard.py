"""Loop guards for models that keep re-issuing tool calls that return nothing new."""

from .read_ledger import META_RE
from .turn_history_support import CURRENT_TURN_KEEP_RECENT_ITEMS

WASTED_REPLY_PREFIXES = (
    "File range already read and unchanged",
    "error: repeated identical tool call",
)
STALL_THRESHOLD = 3

STALL_NOTICE = (
    "Your last {count} tool calls were all rejected as repeats and returned nothing new. "
    "Stop re-issuing them. Work from the information you already have: make the edit, "
    "run a different command, or return your final answer now."
)


TURN_INTERNAL_SOURCES = frozenset({"steer", "worker_notification"})


def current_turn_history(history):
    # 运行中追加的话和 worker 通知也是 role=user，但不是新一轮的开始：跳过它们，否则重复检测会被清零。
    history = list(history)
    for index in range(len(history) - 1, -1, -1):
        item = history[index]
        if item.get("role") == "user" and str(item.get("source", "")) not in TURN_INTERNAL_SOURCES:
            return history[index + 1 :]
    return history


def is_wasted_reply(item):
    return item.get("role") == "tool" and str(item.get("content", "")).startswith(
        WASTED_REPLY_PREFIXES
    )


def wasted_reply_count(history, name, args):
    return sum(
        1
        for item in current_turn_history(history)
        if is_wasted_reply(item) and item.get("name") == name and item.get("args") == args
    )


def escalate_reply(text, history, name, args):
    """Make each further identical repeat in a turn more insistent than the last."""
    previous = wasted_reply_count(history, name, args)
    if previous == 0:
        return text
    if previous == 1:
        return (
            f"{text} This is the 2nd identical request in this turn: do not repeat it. "
            "Use what you already have to edit the file or give your final answer."
        )
    return (
        f"{text} STOP: you have made this exact call {previous + 1} times. Do not call "
        f"{name} with these arguments again; make the change or return your final answer now."
    )


def consecutive_wasted_replies(history):
    count = 0
    for item in reversed(current_turn_history(history)):
        if item.get("role") != "tool":
            continue
        if not is_wasted_reply(item):
            break
        count += 1
    return count


def stall_notice(history):
    count = consecutive_wasted_replies(history)
    return STALL_NOTICE.format(count=count) if count >= STALL_THRESHOLD else ""


def same_turn_compacted(agent):
    """Whether the last prompt collapsed older tool results of the running turn."""
    metadata = getattr(agent, "last_prompt_metadata", None) or {}
    history = (metadata.get("context_usage") or {}).get("history") or {}
    return int(history.get("same_turn_compacted_tools", 0) or 0) > 0


def read_content_visible(history, args, compacted=True):
    """Whether an earlier read_file result for this range is still verbatim in the prompt.

    Once a long turn is compacted, results older than the last few items shrink to a
    one-line summary, so telling the model to "use the earlier result" sends it into a
    re-read loop. Returns True (legacy stub behaviour) when history holds no delivery
    to judge by.
    """
    path = str((args or {}).get("path", "")).strip()
    if not path or not _delivered_before(history, path):
        return True
    items = current_turn_history(history)
    if compacted:
        items = items[-CURRENT_TURN_KEEP_RECENT_ITEMS:]
    start = int((args or {}).get("start", 1))
    requested_end = int((args or {}).get("end", 2000))
    for item in items:
        if item.get("role") != "tool" or item.get("name") != "read_file":
            continue
        if str((item.get("args") or {}).get("path", "")).strip() != path:
            continue
        match = META_RE.search(str(item.get("content", "")))
        if not match:
            continue
        total = int(match.group("total"))
        end = min(requested_end, total) if total > 0 else requested_end
        if int(match.group("start")) <= start and int(match.group("end")) >= end:
            return True
    return False


def _delivered_before(history, path):
    return any(
        item.get("role") == "tool"
        and item.get("name") == "read_file"
        and str((item.get("args") or {}).get("path", "")).strip() == path
        and META_RE.search(str(item.get("content", "")))
        for item in history
    )
