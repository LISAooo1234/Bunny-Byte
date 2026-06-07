"""Compact ledger of file ranges read during a session."""

import html
import re

from ..features import memory as memorylib
from .workspace import MAX_TOOL_OUTPUT, clip

META_RE = re.compile(
    r'<read_file_meta\s+path="(?P<path>[^"]+)"\s+start="(?P<start>\d+)"\s+'
    r'end="(?P<end>\d+)"\s+returned_lines="(?P<returned>\d+)"\s+'
    r'total_lines="(?P<total>\d+)"\s+eof="(?P<eof>true|false)"\s*/>'
)

FILE_UNCHANGED_STUB = (
    "File range already read and unchanged: {path} lines {start}-{end}. "
    "The earlier read_file result in this session is still current; use it instead "
    "of re-reading."
)


def merge_ranges(ranges):
    normalized = sorted(
        (int(start), int(end))
        for start, end in ranges
        if int(start) > 0 and int(end) >= int(start)
    )
    merged = []
    for start, end in normalized:
        if not merged or start > merged[-1][1] + 1:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return merged


def range_covered(ranges, start, end):
    start = int(start)
    end = int(end)
    return any(int(left) <= start and int(right) >= end for left, right in ranges)


def read_stub_metadata():
    return {
        "tool_status": "ok",
        "tool_error_code": "file_range_already_read",
        "security_event_type": "",
        "risk_level": "low",
        "read_only": True,
        "affected_paths": [],
        "workspace_changed": False,
        "diff_summary": [],
    }


def render_read_file_result(full_result):
    full_result = str(full_result)
    meta_start = full_result.rfind("<read_file_meta ")
    if len(full_result) <= MAX_TOOL_OUTPUT:
        return full_result
    meta = full_result[meta_start:].strip() if meta_start >= 0 else ""
    body = full_result[:meta_start].rstrip() if meta_start >= 0 else full_result
    reminder = (
        "\n<system-reminder>read_file output was truncated before entering history; "
        "use narrower start/end ranges or search if you need omitted lines.</system-reminder>"
    )
    budget = max(200, MAX_TOOL_OUTPUT - len(meta) - len(reminder) - 40)
    clipped = clip(body, budget)
    return f"{clipped}{reminder}\n{meta}" if meta else f"{clipped}{reminder}"


class ReadLedger:
    def __init__(self, runtime):
        self.runtime = runtime
        self.state = runtime.session.setdefault("read_ledger", {})
        if not isinstance(self.state, dict):
            self.state = {}
            runtime.session["read_ledger"] = self.state

    def canonical_path(self, path):
        return self.runtime.memory.canonical_path(path)

    def current_freshness(self, path):
        return memorylib.file_freshness(path, self.runtime.root)

    def entry(self, path):
        return self.state.get(self.canonical_path(path), {})

    def is_fresh(self, path):
        entry = self.entry(path)
        if not entry:
            return False
        return entry.get("freshness") == self.current_freshness(path)

    def covered(self, args):
        path = str((args or {}).get("path", "")).strip()
        if not path:
            return False
        if not self.is_fresh(path):
            return False
        start = int((args or {}).get("start", 1))
        requested_end = int((args or {}).get("end", 2000))
        entry = self.entry(path)
        total = int(entry.get("total_lines", 0) or 0)
        end = min(requested_end, total) if total > 0 else requested_end
        if end < start:
            return False
        return range_covered(entry.get("ranges", []), start, end)

    def stub(self, args):
        path = self.canonical_path(str((args or {}).get("path", "")))
        start = int((args or {}).get("start", 1))
        requested_end = int((args or {}).get("end", 2000))
        entry = self.entry(path)
        total = int(entry.get("total_lines", 0) or 0)
        end = min(requested_end, total) if total > 0 else requested_end
        return FILE_UNCHANGED_STUB.format(path=path, start=start, end=end)

    def record_read(self, args, result):
        match = META_RE.search(str(result))
        if not match:
            return
        path = self.canonical_path(html.unescape(match.group("path")))
        returned = int(match.group("returned"))
        if returned <= 0:
            return
        start = int(match.group("start"))
        end = int(match.group("end"))
        total = int(match.group("total"))
        freshness = self.current_freshness(path)
        entry = self.state.get(path, {})
        if entry.get("freshness") != freshness:
            entry = {"ranges": []}
        entry.update(
            {
                "freshness": freshness,
                "total_lines": total,
                "complete": False,
            }
        )
        entry["ranges"] = merge_ranges([*entry.get("ranges", []), [start, end]])
        entry["complete"] = total == 0 or range_covered(entry["ranges"], 1, total)
        self.state[path] = entry

    def invalidate(self, path):
        self.state.pop(self.canonical_path(path), None)

    def render_prompt(self, limit=12):
        fresh_lines = []
        for path, entry in sorted(self.state.items()):
            if entry.get("freshness") != self.current_freshness(path):
                continue
            ranges = entry.get("ranges", [])
            if not ranges:
                continue
            range_text = ", ".join(f"{start}-{end}" for start, end in ranges[:4])
            if len(ranges) > 4:
                range_text += ", ..."
            total = int(entry.get("total_lines", 0) or 0)
            complete = "complete" if entry.get("complete") else f"of {total}" if total else "partial"
            fresh_lines.append(f"- {path}: read lines {range_text} ({complete})")
            if len(fresh_lines) >= limit:
                break
        if not fresh_lines:
            return ""
        return "Read state:\n" + "\n".join(fresh_lines)
