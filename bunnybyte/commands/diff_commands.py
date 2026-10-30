"""/diff [n] [--stat] [路径...]：查看第 n 轮（1 = 最近一轮，与 /undo list 同序）agent 改了什么。"""

from __future__ import annotations

from ..core.shadow_snapshots import ShadowError, ShadowStore
from ..core.turn_diff import file_stats, filter_paths, render_changes, tree_changes
from ..core.undo_log import undo_entries
from .undo_commands import _skipped_note

USAGE_LINES = (
    "`/diff` — 最近一轮 agent 改动的 diff",
    "`/diff 2` — 倒数第 2 轮（序号与 `/undo list` 相同）",
    "`/diff --stat` — 只列出文件和增删行数",
    "`/diff 1 src/app.py tests/` — 只看这些文件或目录",
)
MAX_FILE_LINES = 200
MAX_TOTAL_LINES = 400
STATUS_TEXT = {"A": "新增", "M": "修改", "D": "删除", "T": "类型变化"}


def parse_diff_args(args):
    """返回 (序号, 是否 --stat, 路径列表)；参数不合法时返回 None。"""
    number, stat, paths = 1, False, []
    for position, token in enumerate(str(args or "").split()):
        if token in {"--stat", "-s"}:
            stat = True
        elif token.isdigit() and position == 0:
            number = int(token)
        elif token.startswith("-"):
            return None
        else:
            paths.append(token)
    return (number, stat, paths) if number >= 1 else None


def handle_diff_command(agent, args):
    parsed = parse_diff_args(args)
    if parsed is None:
        return "## Usage\n\n" + "\n".join(f"- {line}" for line in USAGE_LINES)
    number, stat, prefixes = parsed
    store = ShadowStore.for_root(agent.root)
    if not store.available:
        return f"## 无法显示 diff\n\n查看每轮改动依赖 git 快照，当前不可用：{store.disabled_reason}"
    entries = list(reversed(undo_entries(agent)))
    if not entries:
        return "## 没有改动\n\n这个会话里还没有哪一轮改过文件。"
    if number > len(entries):
        return f"## 没有第 {number} 轮\n\n只有 {len(entries)} 轮改过文件，`/undo list` 查看全部。"
    entry = entries[number - 1]
    try:
        changes = filter_paths(tree_changes(store, entry["before_tree"], entry["after_tree"]), prefixes)
    except (ShadowError, OSError, KeyError, ValueError) as exc:
        return f"## 无法显示 diff\n\n这一轮的快照读不出来（{exc}），可能 `.bunnybyte/shadow.git` 被清理过。"
    return render_turn_diff(agent, entry, number, changes, stat=stat, filtered=bool(prefixes))


def render_turn_diff(agent, entry, number, changes, *, stat=False, filtered=False):
    request = str(entry.get("request") or entry.get("turn_id", "")).replace("\n", " ")
    notes = []
    if entry.get("status") == "undone":
        notes.append("这一轮已经用 `/undo` 撤销，下面是它当时的改动，现在磁盘上已不是这样。")
    if entry.get("turn_id") and entry.get("turn_id") == getattr(agent, "current_turn_id", ""):
        notes.append("这一轮还在进行中，diff 只到最近一次工具调用为止。")
    header = f"## 第 {number} 轮的改动：「{request}」"
    if not changes:
        reason = "这些路径在这一轮没有改动。" if filtered else "这一轮没有留下文件改动。"
        return "\n\n".join([header, *notes, reason])
    stats = [file_stats(*item) for item in changes]
    lines = [header, *notes, _stat_table(stats)]
    if not stat:
        body = render_changes(
            changes, mask=agent.redact_text, max_file_lines=MAX_FILE_LINES, max_total_lines=MAX_TOTAL_LINES
        )
        fence = "````" if "```" in body else "```"
        lines.append(f"{fence}diff\n{body}\n{fence}")
    return "\n\n".join(lines) + _skipped_note(agent)


def _stat_table(stats):
    rows = ["| 文件 | 改动 | 行 |", "| --- | --- | --- |"]
    added = removed = 0
    for item in stats:
        path = item["path"].replace("|", "\\|")
        if item["added"] is None:
            counts = "二进制" if item.get("binary") else "太大，未比较"
        else:
            counts = f"+{item['added']} −{item['removed']}"
            added += item["added"]
            removed += item["removed"]
        rows.append(f"| `{path}` | {STATUS_TEXT.get(item['status'], item['status'])} | {counts} |")
    rows.append(f"\n共 {len(stats)} 个文件，+{added} −{removed}")
    return "\n".join(rows)
