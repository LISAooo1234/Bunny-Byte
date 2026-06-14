"""Turn-aware transcript rendering."""

import html
import json
import re
from collections import OrderedDict


CURRENT_TURN_KEEP_RECENT_ITEMS = 4
CURRENT_TURN_SOFT_BUDGET_RATIO = 0.6
CURRENT_TURN_SOFT_MIN_CHARS = 12_000
CURRENT_TURN_SOFT_MIN_ITEMS = 6

READ_FILE_META_RE = re.compile(
    r'<read_file_meta\s+path="(?P<path>[^"]+)"\s+start="(?P<start>\d+)"\s+'
    r'end="(?P<end>\d+)"\s+returned_lines="(?P<returned>\d+)"\s+'
    r'total_lines="(?P<total>\d+)"\s+eof="(?P<eof>true|false)"\s*/>'
)


def tail_clip(text, limit):
    text = str(text)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


class TurnHistoryBuilder:
    def __init__(self, agent):
        self.agent = agent

    def enrich(self, item):
        item = dict(item)
        if not item.get("turn_id"):
            current_turn = str(getattr(self.agent, "current_turn_id", "") or "")
            if not current_turn:
                if item.get("role") == "user" or not self.agent.session.get("_manual_turn_id"):
                    self.agent.session["_manual_turn_seq"] = int(self.agent.session.get("_manual_turn_seq", 0)) + 1
                    self.agent.session["_manual_turn_id"] = f"manual_{self.agent.session['_manual_turn_seq']:06d}"
                current_turn = str(self.agent.session.get("_manual_turn_id", "legacy"))
            item["turn_id"] = current_turn
        if not item.get("run_id"):
            item["run_id"] = str(getattr(self.agent, "current_run_id", "") or "")
        if not item.get("event_id"):
            self.agent.session["_event_seq"] = int(self.agent.session.get("_event_seq", 0)) + 1
            item["event_id"] = f"event_{self.agent.session['_event_seq']:06d}"
        item.setdefault("source", "runtime")
        return item

    def prompt_history(self):
        compact_manager = getattr(self.agent, "compact_manager", None)
        if compact_manager is not None and hasattr(compact_manager, "prompt_history"):
            return list(compact_manager.prompt_history())
        return list(getattr(self.agent, "session", {}).get("history", []))

    def raw_text(self, history):
        if not history:
            return "Transcript:\n- empty"
        return "\n".join(["Transcript:", *self._render_turn_lines(history, line_limit=None)])

    def render_section(self, budget):
        history = self.prompt_history()
        raw = self.raw_text(history)
        if not history:
            return raw, {
                "rendered_entries": [],
                "older_entries_count": 0,
                "collapsed_duplicate_reads": 0,
                "reused_file_summary_count": 0,
                "summarized_tool_count": 0,
                "same_turn_compacted_entries": 0,
                "same_turn_compacted_tools": 0,
                "same_turn_compacted_notifications": 0,
                "rendered_turns": 0,
            }

        turns = self._group_turns(history)
        same_turn_turn_id = ""
        if self._should_compact_current_turn(turns, raw, budget):
            same_turn_turn_id = next(reversed(turns.keys()), "")

        if budget <= 0 or (len(raw) <= budget and not same_turn_turn_id):
            rendered_entries = self._render_turn_lines(history, line_limit=None)
            return raw, {
                "rendered_entries": rendered_entries,
                "older_entries_count": 0,
                "collapsed_duplicate_reads": 0,
                "reused_file_summary_count": 0,
                "summarized_tool_count": 0,
                "same_turn_compacted_entries": 0,
                "same_turn_compacted_tools": 0,
                "same_turn_compacted_notifications": 0,
                "rendered_turns": sum(1 for line in rendered_entries if line.startswith("Turn ")),
                "budget_clipped": False,
            }

        recent_turn_ids = set(list(turns.keys())[-3:])
        compressed_entries, compression_details = self._compressed_turn_entries(
            turns,
            recent_turn_ids,
            same_turn_turn_id=same_turn_turn_id,
        )
        rendered_entries = [line for entry in compressed_entries for line in entry["lines"]]
        rendered_text = "\n".join(["Transcript:", *rendered_entries])
        budget_clipped = False
        if budget > 0 and len(rendered_text) > budget:
            marker = "Transcript:\n..."
            if budget <= len(marker):
                rendered_text = tail_clip(rendered_text, budget)
            else:
                rendered_text = marker + rendered_text[-(budget - len(marker)):]
            budget_clipped = True
        details = {
            "rendered_entries": rendered_entries,
            "older_entries_count": int(compression_details.get("older_entries_count", 0)),
            "collapsed_duplicate_reads": int(compression_details.get("collapsed_duplicate_reads", 0)),
            "reused_file_summary_count": int(compression_details.get("reused_file_summary_count", 0)),
            "summarized_tool_count": int(compression_details.get("summarized_tool_count", 0)),
            "same_turn_compacted_entries": int(compression_details.get("same_turn_compacted_entries", 0)),
            "same_turn_compacted_tools": int(compression_details.get("same_turn_compacted_tools", 0)),
            "same_turn_compacted_notifications": int(compression_details.get("same_turn_compacted_notifications", 0)),
            "rendered_turns": sum(1 for line in rendered_entries if line.startswith("Turn ")),
            "budget_clipped": budget_clipped,
        }
        return rendered_text, details

    def _group_turns(self, history):
        turns = OrderedDict()
        for item in history:
            turn_id = str(item.get("turn_id") or "legacy")
            turns.setdefault(turn_id, []).append(item)
        return turns

    def _compressed_turn_entries(self, turns, recent_turns, same_turn_turn_id=""):
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
        }
        for turn_id, items in turns.items():
            recent = turn_id in recent_turns and any(item.get("role") != "tool" for item in items)
            lines = [f"Turn {turn_id}:"]
            if turn_id == same_turn_turn_id:
                current_lines, current_details = self._microcompact_current_turn(items)
                lines.extend(current_lines)
                for key in (
                    "collapsed_duplicate_reads",
                    "same_turn_compacted_entries",
                    "same_turn_compacted_tools",
                    "same_turn_compacted_notifications",
                ):
                    details[key] += int(current_details.get(key, 0))
                entries.append({"turn_id": turn_id, "lines": lines})
                continue
            for item in items:
                if item.get("kind") == "compact_summary":
                    lines.extend(str(item.get("content", "")).splitlines())
                    continue
                if not recent and item.get("role") == "tool" and item.get("name") == "read_file":
                    path = str(item.get("args", {}).get("path", "")).strip()
                    if path in seen_older_reads:
                        details["collapsed_duplicate_reads"] += 1
                        continue
                    seen_older_reads.add(path)
                    summary = self._reusable_file_summary(path)
                    if summary:
                        lines.append(f"{path} -> {summary}")
                        details["reused_file_summary_count"] += 1
                        continue
                if not recent and item.get("role") == "tool":
                    lines.append(self._summarize_tool_item(item, prefer_file_summary=False))
                    details["summarized_tool_count"] += 1
                    continue
                lines.extend(self._render_item(item, 900 if recent else 80))
            if not recent:
                details["older_entries_count"] += 1
            entries.append({"turn_id": turn_id, "lines": lines})
        return entries, details

    def _should_compact_current_turn(self, turns, raw, budget):
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
            soft_limit = max(CURRENT_TURN_SOFT_MIN_CHARS, int(budget * CURRENT_TURN_SOFT_BUDGET_RATIO))
            return len(current_turn_text) > soft_limit
        return False

    def _microcompact_current_turn(self, items):
        lines = []
        details = {
            "collapsed_duplicate_reads": 0,
            "same_turn_compacted_entries": 0,
            "same_turn_compacted_tools": 0,
            "same_turn_compacted_notifications": 0,
        }
        if not items:
            return lines, details
        protected = {0} if items[0].get("role") == "user" else set()
        keep_from = max(0, len(items) - CURRENT_TURN_KEEP_RECENT_ITEMS)
        seen_read_paths = set()
        for index, item in enumerate(items):
            if item.get("kind") == "compact_summary":
                lines.extend(str(item.get("content", "")).splitlines())
                continue
            if index in protected or index >= keep_from:
                lines.extend(self._render_item(item, 900))
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

    def _render_turn_lines(self, history, line_limit):
        lines = []
        for turn_id, items in self._group_turns(history).items():
            lines.append(f"Turn {turn_id}:")
            for item in items:
                lines.extend(self._render_item(item, line_limit))
        return lines

    def _render_item(self, item, line_limit):
        if item.get("kind") == "compact_summary":
            return str(item.get("content", "")).splitlines()
        if item.get("role") == "tool":
            prefix = f"[tool:{item.get('name', '')}] {json.dumps(item.get('args', {}), sort_keys=True)}"
            if line_limit is None:
                content = str(item.get("content", ""))
            else:
                content = tail_clip(item.get("content", ""), max(20, line_limit))
            return [prefix, content]
        content = str(item.get("content", "")) if line_limit is None else tail_clip(item.get("content", ""), line_limit)
        return [f"[{item.get('role', '')}] {content}"]

    def _reusable_file_summary(self, path):
        memory = getattr(self.agent, "memory", None)
        if memory is None or not hasattr(memory, "to_dict"):
            return ""
        summary = memory.to_dict().get("file_summaries", {}).get(str(path), {})
        return str(summary.get("summary", "")).strip()

    def _summarize_current_turn_item(self, item, seen_read_paths):
        if self._is_worker_notification(item):
            return self._summarize_worker_notification(item)
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
                return (
                    f"{file_path} lines {groups.get('start')}-{groups.get('end')} "
                    f"of {groups.get('total')}{suffix}"
                ).strip()
            if path:
                return f"{path} lines {args.get('start', 1)}-{args.get('end', 2000)}"
        if name == "run_shell":
            command = str(args.get("command", "")).strip() or "shell"
            lines = [line.strip() for line in content.splitlines() if line.strip()]
            return f"{command} -> {' | '.join(lines[:3]) if lines else '(empty)'}"
        if name == "search":
            pattern = tail_clip(str(args.get("pattern", "")).strip(), 40)
            search_path = str(args.get("path", ".")).strip() or "."
            lines = [line for line in content.splitlines() if line.strip() and line.strip() != "(no matches)"]
            if content.strip() == "(no matches)":
                summary = "no matches"
            else:
                count = len(lines)
                summary = f"{count} matches" if count != 1 else "1 match"
            return f"search {pattern} in {search_path} -> {summary}".strip()
        if name == "list_files":
            list_path = str(args.get("path", ".")).strip() or "."
            lines = [line for line in content.splitlines() if line.strip() and line.strip() != "(empty)"]
            if not lines:
                return f"list_files {list_path} -> empty"
            return f"list_files {list_path} -> {len(lines)} items"
        return self._render_item(item, 80)[0]

    @staticmethod
    def _is_worker_notification(item):
        return (
            item.get("role") == "user"
            and "<task-notification>" in str(item.get("content", "") or "")
        )

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
