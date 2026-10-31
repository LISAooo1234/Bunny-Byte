"""Loop guards for models that keep re-issuing tool calls that return nothing new."""

from .read_ledger import META_RE
from .turn_history_support import clip_read_result

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


CLIPPED_STUB = (
    "File range already read and unchanged: {path}. Your earlier read_file result is shown up to "
    "line {last} (it is clipped in the prompt); re-reading the same range would be clipped the same way. "
    "Call read_file with start={start} (and an end) to see the rest."
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


def prompt_view(agent):
    """What the last prompt showed of the running turn: read results by event id and their visible line range.

    None when no prompt has been rendered with this information yet (every delivery then counts as visible).
    """
    metadata = getattr(agent, "last_prompt_metadata", None) or {}
    history = metadata.get("history") or {}
    if "visible_reads" not in history:
        return None
    return {
        "visible_reads": dict(history.get("visible_reads") or {}),
        "rendered_through": str(history.get("rendered_through") or ""),
        "item_cap": history.get("item_cap"),
    }


def _same_path(item, path, canon):
    raw = str((item.get("args") or {}).get("path", "")).strip()
    return raw == path or (canon is not None and bool(raw) and canon(raw) == canon(path))


def _shown_ranges(history, path, view, canon=None):
    """(start, last_shown, clipped) for each read of `path` in this turn that the model can see."""
    items = current_turn_history(history)
    ids = [str(item.get("event_id", "")) for item in items]
    through = view.get("rendered_through", "") if view else ""
    new_from = ids.index(through) + 1 if through and through in ids else 0
    shown = []
    for index, item in enumerate(items):
        if item.get("role") != "tool" or item.get("name") != "read_file":
            continue
        if not _same_path(item, path, canon):
            continue
        if view is None or index >= new_from:
            # 还没进过提示词的结果下一次会出现：上次没压缩就是整份，压缩了就按上次的单条上限估算
            _, seen = clip_read_result(item.get("content", ""), None if view is None else view.get("item_cap"))
        else:
            seen = view["visible_reads"].get(ids[index])
        if seen:
            shown.append((int(seen[0]), int(seen[1]), bool(seen[2])))
    return shown


def _requested_range(args, history, path, canon=None):
    start = int((args or {}).get("start", 1))
    requested_end = int((args or {}).get("end", 2000))
    for item in reversed(current_turn_history(history)):
        if item.get("role") == "tool" and item.get("name") == "read_file" and _same_path(item, path, canon):
            match = META_RE.search(str(item.get("content", "")))
            if match and int(match.group("total")) > 0:
                return start, min(requested_end, int(match.group("total")))
    return start, requested_end


def read_content_visible(history, args, view=None, canon=None):
    """Whether the requested read_file range is still readable in the prompt the model just saw.

    Long turns collapse older results to one-line summaries and clip big ones; telling the
    model to "use the earlier result" then sends it into a re-read loop. A clipped result
    whose visible part already starts the request also counts as visible: re-sending it would
    be clipped the same way, so the reply points at the next start line instead
    (`clipped_continuation`). Returns True (legacy stub behaviour) when history holds no
    delivery to judge by. `canon` normalizes paths (`./a.py`, absolute paths) like the read ledger.
    """
    path = str((args or {}).get("path", "")).strip()
    if not path or not _delivered_before(history, path, canon):
        return True
    start, end = _requested_range(args, history, path, canon)
    for left, last, clipped in _shown_ranges(history, path, view, canon):
        if left <= start and last >= end:
            return True
        if clipped and left <= start <= last:
            return True
    return False


def clipped_continuation(history, args, view=None, canon=None):
    """Next start line when the visible result of this read was clipped before the requested end."""
    path = str((args or {}).get("path", "")).strip()
    if not path:
        return None
    start, end = _requested_range(args, history, path, canon)
    shown = _shown_ranges(history, path, view, canon)
    for left, last, clipped in shown:
        if left <= start and last >= end:
            return None
    for left, last, clipped in shown:
        if clipped and left <= start <= last:
            return last + 1
    return None


def read_stub(agent, ledger, args):
    """The "already read" reply; for a result clipped in the prompt it names the line to continue from."""
    following = clipped_continuation(agent.session["history"], args, prompt_view(agent), ledger.canonical_path)
    if following is None:
        return ledger.stub(args)
    path = ledger.canonical_path(str((args or {}).get("path", "")))
    return CLIPPED_STUB.format(path=path, last=following - 1, start=following)


def _delivered_before(history, path, canon=None):
    return any(
        item.get("role") == "tool"
        and item.get("name") == "read_file"
        and _same_path(item, path, canon)
        and META_RE.search(str(item.get("content", "")))
        for item in history
    )
