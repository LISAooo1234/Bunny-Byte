"""Rendering helpers for turn-aware transcript history."""

import html
import json
import re
from collections import OrderedDict

from .transcript_fence import fence

CURRENT_TURN_SOFT_BUDGET_RATIO = 0.6
CURRENT_TURN_SOFT_MIN_CHARS = 12_000
CURRENT_TURN_SOFT_MIN_ITEMS = 6
CURRENT_TURN_FORCED_MIN_CHARS = 4_000
# 本轮原样保留的结果超过软上限时，一次性把切点推到只剩一半：切点只随新结果单调前进，
# 两次切之间历史只在末尾追加，提示词前缀缓存只在切的那一次失效，而不是每次调用都失效。
CURRENT_TURN_KEEP_RATIO = 0.5
ITEM_CHAR_CAP = 16_000  # 单条结果在提示词里最多显示的字数；read_file 按整行截断并说明显示到第几行
LINE_CLIP_MIN_LIMIT = 400
READ_FILE_META_RE = re.compile(
    r'<read_file_meta\s+path="(?P<path>[^"]+)"\s+start="(?P<start>\d+)"\s+'
    r'end="(?P<end>\d+)"\s+returned_lines="(?P<returned>\d+)"\s+'
    r'total_lines="(?P<total>\d+)"\s+eof="(?P<eof>true|false)"\s*/>'
)
READ_LINE_RE = re.compile(r"^\s*(\d+): ")


def clip_read_result(content, limit):
    """Clip a read_file result at a line boundary.

    Returns (text, shown) where shown is [start, last_shown_line, clipped, total_lines]
    for the lines the model can actually see, or None when no numbered line survives.
    """
    content = str(content)
    meta = READ_FILE_META_RE.search(content)
    if not meta:
        return (content if limit is None else tail_clip(content, limit)), None
    start, end, total = int(meta.group("start")), int(meta.group("end")), int(meta.group("total"))
    if limit is None or len(content) <= limit:
        return content, [start, end, False, total]
    if limit < LINE_CLIP_MIN_LIMIT:
        return tail_clip(content, limit), None
    note_room = 120
    kept, used, last = [], 0, start - 1
    for line in content.splitlines():
        number = READ_LINE_RE.match(line)
        if used + len(line) + 1 > limit - note_room:
            break
        if number:
            last = int(number.group(1))
        elif kept:
            break  # 到了 meta 行或正文之后的内容
        kept.append(line)
        used += len(line) + 1
    if last < start:
        return tail_clip(content, limit), None
    kept.append(
        f"...[showing lines {start}-{last} of {total}; this result is clipped at {limit} chars, "
        f"call read_file with start={last + 1} for the rest]"
    )
    return "\n".join(kept), [start, last, True, total]


def verbatim_cut(sizes, first, limit):
    """Index of the first current-turn item shown verbatim.

    Depends only on the item sizes up to each point, so appending items never moves
    an earlier decision: the cut only advances, in batches, when the tail overflows.
    """
    cut, tail = first, 0
    for index in range(first, len(sizes)):
        tail += sizes[index]
        if tail <= limit:
            continue
        while cut < index and tail > limit * CURRENT_TURN_KEEP_RATIO:
            tail -= sizes[cut]
            cut += 1
    return cut


def tail_clip(text, limit):
    text = str(text)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def group_turns(history):
    turns = OrderedDict()
    for item in history:
        turn_id = str(item.get("turn_id") or "legacy")
        turns.setdefault(turn_id, []).append(item)
    return turns


def empty_history_details():
    return {
        "rendered_entries": [],
        "older_entries_count": 0,
        "collapsed_duplicate_reads": 0,
        "reused_file_summary_count": 0,
        "summarized_tool_count": 0,
        "same_turn_compacted_entries": 0,
        "same_turn_compacted_tools": 0,
        "same_turn_compacted_notifications": 0,
        "rendered_turns": 0,
        "visible_reads": {},
        "rendered_through": "",
    }


def current_turn_soft_limit(budget):
    return max(CURRENT_TURN_SOFT_MIN_CHARS, int(max(0, budget or 0) * CURRENT_TURN_SOFT_BUDGET_RATIO))


def _line_offsets(entries):
    """Character offset in the rendered transcript where each visible read result starts."""
    offsets, position = {}, len("Transcript:") + 1
    for entry in entries:
        starts = {line: event_id for event_id, line in (entry.get("starts") or {}).items()}
        for index, line in enumerate(entry["lines"]):
            if index in starts:
                offsets[starts[index]] = position
            position += len(line) + 1
    return offsets


def full_visible_reads(items):
    """Every read_file result of the turn rendered in full: event_id -> shown range."""
    visible = {}
    for item in items:
        if item.get("role") == "tool" and item.get("name") == "read_file" and item.get("event_id"):
            _, shown = clip_read_result(item.get("content", ""), None)
            if shown:
                visible[str(item["event_id"])] = shown
    return visible


class TurnHistoryRenderer:
    def __init__(self, agent):
        self.agent = agent

    def raw_text(self, history):
        if not history:
            return "Transcript:\n- empty"
        return "\n".join(["Transcript:", *self._render_turn_lines(history, line_limit=None)])

    def render_section(self, history, budget, stable_budget=None):
        """Render the transcript within `budget`.

        `stable_budget` is the configured (unreduced) history budget: the current turn's
        cut point is computed from it so a one-off budget reduction doesn't move the cut.
        """
        raw = self.raw_text(history)
        if not history:
            return raw, empty_history_details()

        turns = group_turns(history)
        current_turn_id = next(reversed(turns.keys()), "")
        soft_limit = current_turn_soft_limit(stable_budget or budget)
        compact_current = self._should_compact_current_turn(turns, raw, budget, soft_limit)
        through = str(history[-1].get("event_id", ""))
        if budget <= 0 or (len(raw) <= budget and not compact_current):
            rendered_entries = self._render_turn_lines(history, line_limit=None)
            details = empty_history_details()
            details.update(
                {
                    "rendered_entries": rendered_entries,
                    "rendered_turns": sum(1 for line in rendered_entries if line.startswith("Turn ")),
                    "budget_clipped": False,
                    "visible_reads": full_visible_reads(turns.get(current_turn_id, [])),
                    "rendered_through": through,
                    "item_cap": None,
                }
            )
            return raw, details

        recent_turn_ids = set(list(turns.keys())[-3:])
        current_limit = soft_limit if len(raw) <= budget else None
        compressed_entries, compression_details = self._compressed_turn_entries(
            turns, recent_turn_ids, current_turn_id, stable_budget or budget, current_limit or soft_limit, forced=current_limit is None
        )
        rendered_entries = [line for entry in compressed_entries for line in entry["lines"]]
        rendered_text = "\n".join(["Transcript:", *rendered_entries])
        visible_reads = dict(compression_details.get("visible_reads", {}))
        budget_clipped = False
        if budget > 0 and len(rendered_text) > budget:
            marker = "Transcript:\n..."
            kept_from = len(rendered_text) - max(0, budget - len(marker))
            rendered_text = tail_clip(rendered_text, budget) if budget <= len(marker) else marker + rendered_text[-(budget - len(marker)) :]
            budget_clipped = True
            # 只截掉开头：起点还在保留部分里的结果仍然完整可见，其余的当作看不到
            offsets = _line_offsets(compressed_entries)
            visible_reads = {
                event_id: shown
                for event_id, shown in visible_reads.items()
                if budget > len(marker) and offsets.get(event_id, -1) >= kept_from
            }
        details = {
            key: int(compression_details.get(key, 0))
            for key in (
                "older_entries_count",
                "collapsed_duplicate_reads",
                "reused_file_summary_count",
                "summarized_tool_count",
                "same_turn_compacted_entries",
                "same_turn_compacted_tools",
                "same_turn_compacted_notifications",
            )
        }
        details.update(
            {
                "rendered_entries": rendered_entries,
                "rendered_turns": sum(1 for line in rendered_entries if line.startswith("Turn ")),
                "budget_clipped": budget_clipped,
                "visible_reads": visible_reads,
                "rendered_through": through,
                # 上次渲染时本轮单条结果的显示上限；下一次新到的结果按它估算能看到的行
                "item_cap": compression_details.get("item_cap"),
            }
        )
        return rendered_text, details

    def _compressed_turn_entries(self, turns, recent_turns, current_turn_id, budget, soft_limit, forced=False):
        entries = []
        seen_older_reads = set()
        details = {
            "recent_window": len(recent_turns),
            "older_entries_count": 0,
            "collapsed_duplicate_reads": 0,
            "reused_file_summary_count": 0,
            "summarized_tool_count": 0,
            "same_turn_compacted_entries": 0,
            "same_turn_compacted_tools": 0,
            "same_turn_compacted_notifications": 0,
            "visible_reads": {},
        }
        earlier_chars = 0
        for turn_id, items in turns.items():
            recent = turn_id in recent_turns and any(item.get("role") != "tool" for item in items)
            lines = [f"Turn {turn_id}:"]
            if turn_id == current_turn_id:
                limit = soft_limit
                if forced:
                    # 整段历史超预算：本轮原样部分只用前面几轮剩下的空间，免得整段从开头被截（那样每次调用开头都变）
                    limit = max(CURRENT_TURN_FORCED_MIN_CHARS, min(soft_limit, budget - earlier_chars - 64))
                current_lines, current_details = self._microcompact_current_turn(items, limit)
                for _ in range(3):
                    # 摘要行和受保护的用户消息也占地方：还超就按超出的量再收紧一次
                    overflow = earlier_chars + sum(len(line) + 1 for line in current_lines) + 32 - budget
                    if not forced or overflow <= 0 or limit <= CURRENT_TURN_FORCED_MIN_CHARS:
                        break
                    limit = max(CURRENT_TURN_FORCED_MIN_CHARS, limit - overflow)
                    current_lines, current_details = self._microcompact_current_turn(items, limit)
                lines.extend(current_lines)
                for key in (
                    "collapsed_duplicate_reads",
                    "same_turn_compacted_entries",
                    "same_turn_compacted_tools",
                    "same_turn_compacted_notifications",
                ):
                    details[key] += int(current_details.get(key, 0))
                details["visible_reads"] = current_details["visible_reads"]
                details["item_cap"] = current_details["item_cap"]
                entries.append(
                    {"turn_id": turn_id, "lines": lines, "starts": {key: line + 1 for key, line in current_details["starts"].items()}}
                )
                continue
            for item in items:
                self._append_item_lines(item, lines, recent, seen_older_reads, details)
            if not recent:
                details["older_entries_count"] += 1
            earlier_chars += sum(len(line) + 1 for line in lines)
            entries.append({"turn_id": turn_id, "lines": lines})
        return entries, details

    def _append_item_lines(self, item, lines, recent, seen_older_reads, details):
        if item.get("kind") == "compact_summary":
            lines.extend(str(item.get("content", "")).splitlines())
            return
        if not recent and item.get("role") == "tool" and item.get("name") == "read_file":
            path = str(item.get("args", {}).get("path", "")).strip()
            if path in seen_older_reads:
                details["collapsed_duplicate_reads"] += 1
                return
            seen_older_reads.add(path)
            summary = self._reusable_file_summary(path)
            if summary:
                lines.append(f"{path} -> {summary}")
                details["reused_file_summary_count"] += 1
                return
        if not recent and item.get("role") == "tool":
            lines.append(self._summarize_tool_item(item, prefer_file_summary=False))
            details["summarized_tool_count"] += 1
            return
        lines.extend(self._render_item(item, 900 if recent else 80))

    def _should_compact_current_turn(self, turns, raw, budget, soft_limit):
        if not turns:
            return False
        current_turn_id = next(reversed(turns.keys()), "")
        items = list(turns.get(current_turn_id, []) or [])
        if len(items) < CURRENT_TURN_SOFT_MIN_ITEMS:
            return False
        compactable = sum(1 for item in items if self._is_current_turn_compactable_item(item))
        if compactable < 3:
            return False
        current_turn_text = "\n".join(
            [f"Turn {current_turn_id}:"] + [line for item in items for line in self._render_item(item, line_limit=None)]
        )
        if budget and budget > 0 and len(raw) > budget:
            return True
        if budget and budget > 0:
            return len(current_turn_text) > soft_limit
        return False

    def _microcompact_current_turn(self, items, limit):
        lines = []
        details = {
            "collapsed_duplicate_reads": 0,
            "same_turn_compacted_entries": 0,
            "same_turn_compacted_tools": 0,
            "same_turn_compacted_notifications": 0,
            "visible_reads": {},
            "starts": {},
            "item_cap": None,
        }
        if not items:
            return lines, details
        cap = min(ITEM_CHAR_CAP, max(LINE_CLIP_MIN_LIMIT, limit // 2))
        details["item_cap"] = cap
        protected = {0} if items[0].get("role") == "user" else set()
        rendered = [self._render_verbatim(item, 900 if index in protected else cap) for index, item in enumerate(items)]
        sizes = [
            0 if item.get("kind") == "compact_summary" or index in protected else sum(len(line) + 1 for line in rendered[index][0])
            for index, item in enumerate(items)
        ]
        cut = verbatim_cut(sizes, 1 if protected else 0, limit)
        seen_read_paths = set()
        for index, item in enumerate(items):
            if item.get("kind") == "compact_summary":
                lines.extend(str(item.get("content", "")).splitlines())
                continue
            if index in protected or index >= cut:
                item_lines, shown = rendered[index]
                if shown and item.get("event_id"):
                    details["visible_reads"][str(item["event_id"])] = shown
                    details["starts"][str(item["event_id"])] = len(lines)
                lines.extend(item_lines)
                continue
            if item.get("role") == "tool" and item.get("name") == "read_file":
                path = str(item.get("args", {}).get("path", "")).strip()
                if path and path in seen_read_paths:
                    details["collapsed_duplicate_reads"] += 1
            summary = self._summarize_current_turn_item(item, seen_read_paths)
            if summary:
                lines.append(summary)
                details["same_turn_compacted_entries"] += 1
                if item.get("role") == "tool":
                    details["same_turn_compacted_tools"] += 1
                elif self._is_worker_notification(item):
                    details["same_turn_compacted_notifications"] += 1
                continue
            lines.extend(self._render_item(item, 160))
            details["same_turn_compacted_entries"] += 1
        return lines, details

    def _render_verbatim(self, item, limit):
        """Lines for an item shown as-is (clipped at `limit`) and, for read_file, the line range shown."""
        if item.get("role") == "tool" and item.get("name") == "read_file":
            content, shown = clip_read_result(item.get("content", ""), limit)
            prefix = f"[tool:read_file] {json.dumps(item.get('args', {}), sort_keys=True)}"
            return [prefix, fence(content)], shown
        return self._render_item(item, limit), None

    def _render_turn_lines(self, history, line_limit):
        lines = []
        for turn_id, items in group_turns(history).items():
            lines.append(f"Turn {turn_id}:")
            for item in items:
                lines.extend(self._render_item(item, line_limit))
        return lines

    def _render_item(self, item, line_limit):
        if item.get("kind") == "compact_summary":
            return str(item.get("content", "")).splitlines()
        if item.get("role") == "tool":
            prefix = f"[tool:{item.get('name', '')}] {json.dumps(item.get('args', {}), sort_keys=True)}"
            content = str(item.get("content", "")) if line_limit is None else tail_clip(item.get("content", ""), max(20, line_limit))
            return [prefix, fence(content)]
        content = str(item.get("content", "")) if line_limit is None else tail_clip(item.get("content", ""), line_limit)
        return [f"[{item.get('role', '')}] {fence(content)}"]

    def _reusable_file_summary(self, path):
        memory = getattr(self.agent, "memory", None)
        if memory is None or not hasattr(memory, "to_dict"):
            return ""
        summary = memory.to_dict().get("file_summaries", {}).get(str(path), {})
        return str(summary.get("summary", "")).strip()

    def _summarize_current_turn_item(self, item, seen_read_paths):
        if self._is_worker_notification(item):
            return fence(self._summarize_worker_notification(item))
        if item.get("role") != "tool":
            return ""
        if item.get("name") == "read_file":
            path = str(item.get("args", {}).get("path", "")).strip()
            if path in seen_read_paths:
                return f"{path} -> reused earlier in this turn"
            if path:
                seen_read_paths.add(path)
        return self._summarize_tool_item(item, prefer_file_summary=False)

    def _summarize_tool_item(self, item, prefer_file_summary=False):
        name = str(item.get("name", "")).strip()
        args = item.get("args", {}) or {}
        content = str(item.get("content", "") or "")
        if name == "read_file":
            return self._summarize_read_file(args, content, prefer_file_summary)
        if name == "run_shell":
            command = str(args.get("command", "")).strip() or "shell"
            lines = [line.strip() for line in content.splitlines() if line.strip()]
            return f"{command} -> {' | '.join(lines[:3]) if lines else '(empty)'}"
        if name == "search":
            return self._summarize_search(args, content)
        if name == "list_files":
            return self._summarize_list_files(args, content)
        return self._render_item(item, 80)[0]

    def _summarize_read_file(self, args, content, prefer_file_summary):
        path = str(args.get("path", "")).strip()
        if prefer_file_summary and path:
            summary = self._reusable_file_summary(path)
            if summary:
                return f"{path} -> {summary}"
        meta = READ_FILE_META_RE.search(content)
        if meta:
            groups = meta.groupdict()
            file_path = html.unescape(groups.get("path") or path or "")
            suffix = " complete" if groups.get("eof") == "true" else ""
            return f"{file_path} lines {groups.get('start')}-{groups.get('end')} of {groups.get('total')}{suffix}".strip()
        if path:
            return f"{path} lines {args.get('start', 1)}-{args.get('end', 2000)}"
        return ""

    @staticmethod
    def _summarize_search(args, content):
        pattern = tail_clip(str(args.get("pattern", "")).strip(), 40)
        search_path = str(args.get("path", ".")).strip() or "."
        lines = [line for line in content.splitlines() if line.strip() and line.strip() != "(no matches)"]
        if content.strip() == "(no matches)":
            summary = "no matches"
        else:
            count = len(lines)
            summary = f"{count} matches" if count != 1 else "1 match"
        return f"search {pattern} in {search_path} -> {summary}".strip()

    @staticmethod
    def _summarize_list_files(args, content):
        list_path = str(args.get("path", ".")).strip() or "."
        lines = [line for line in content.splitlines() if line.strip() and line.strip() != "(empty)"]
        if not lines:
            return f"list_files {list_path} -> empty"
        return f"list_files {list_path} -> {len(lines)} items"

    @staticmethod
    def _is_worker_notification(item):
        return item.get("role") == "user" and "<task-notification>" in str(item.get("content", "") or "")

    def _is_current_turn_compactable_item(self, item):
        return item.get("role") == "tool" or self._is_worker_notification(item)

    def _summarize_worker_notification(self, item):
        text = str(item.get("content", "") or "")
        task_id = self._xml_text(text, "task-id") or "worker"
        status = self._xml_text(text, "status") or "completed"
        summary = self._xml_text(text, "summary")
        preview = self._xml_text(text, "result_preview")
        tool_uses = self._xml_text(text, "tool_uses")
        attempts = self._xml_text(text, "attempts")
        duration_ms = self._xml_text(text, "duration_ms")
        parts = [f"worker {task_id} {status}"]
        if summary:
            parts.append(tail_clip(summary, 90))
        metrics = []
        if tool_uses:
            metrics.append(f"tools {tool_uses}")
        if attempts:
            metrics.append(f"attempts {attempts}")
        if duration_ms:
            metrics.append(f"{duration_ms}ms")
        if metrics:
            parts.append(" | ".join(metrics))
        if preview:
            parts.append(tail_clip(preview, 120))
        return " | ".join(parts)

    @staticmethod
    def _xml_text(text, tag):
        match = re.search(rf"<{tag}>(.*?)</{tag}>", str(text or ""), re.DOTALL)
        if not match:
            return ""
        return html.unescape(match.group(1).strip())
