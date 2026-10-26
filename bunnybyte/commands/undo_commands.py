"""/undo and /undo list: revert the files a past agent turn changed."""

from __future__ import annotations

from ..core.shadow_snapshots import ShadowStore
from ..core.undo_log import UndoRefused, entry_paths, undo_entries, undo_latest

USAGE_LINES = (
    "`/undo` — 撤销最近一轮对文件的改动",
    "`/undo list` — 列出可撤销的轮次",
    "`/undo --force` — 即使之后又改过这些文件也强制撤销（会覆盖你的修改）",
)
MAX_LISTED_PATHS = 6


def handle_undo_command(agent, args):
    tokens = str(args or "").split()
    if tokens in ([], ["--force"], ["-f"], ["force"]):
        return _undo(agent, force=bool(tokens))
    if tokens == ["list"]:
        return _list(agent)
    return "## Usage\n\n" + "\n".join(f"- {line}" for line in USAGE_LINES)


def _undo(agent, force):
    try:
        outcome = undo_latest(agent, force=force)
    except UndoRefused as exc:
        return f"## 无法撤销\n\n{exc}"
    entry = outcome["entry"]
    request = entry.get("request") or entry.get("turn_id", "")
    if outcome["status"] == "conflict":
        files = "\n".join(f"- `{path}`" for path in outcome["conflicts"])
        return (
            f"## 没有撤销\n\n「{request}」那一轮结束之后，下面这些文件又被改过，直接撤销会覆盖这些修改，所以一个文件也没动：\n\n"
            f"{files}\n\n确认要覆盖时运行 `/undo --force`。"
        )
    lines = [f"## 已撤销\n\n还原了「{request}」那一轮改动的 {len(outcome['applied'])} 个文件：\n"]
    lines += [f"- `{path}`" for path in outcome["applied"]]
    if outcome["failed"]:
        lines += ["", "这些文件没能还原（权限或被目录占用）：", *[f"- `{path}`" for path in outcome["failed"]]]
    if outcome["head_changed"]:
        lines += ["", "注意：那一轮期间 git 的 HEAD 变过（提交或切换了分支）。撤销只还原工作区文件，不会撤销提交。"]
    lines += ["", "再运行 `/undo` 会继续撤销更早的一轮，`/undo list` 查看全部。"]
    return "\n".join(lines)


def _skipped_note(agent):
    skipped = ShadowStore.for_root(agent.root).skipped_large
    if not skipped:
        return ""
    shown = "、".join(f"`{path}`" for path in skipped[:5])
    return f"\n\n以下新增大文件（超过 8 MB）没有纳入快照，不会被撤销：{shown}" + (" 等" if len(skipped) > 5 else "")


def _list(agent):
    entries = undo_entries(agent)
    if not entries:
        return "## 可撤销的轮次\n\n还没有哪一轮改过文件。"
    lines = ["## 可撤销的轮次（最新在前）", "", "| # | 状态 | 请求 | 改动的文件 |", "| --- | --- | --- | --- |"]
    for number, entry in enumerate(reversed(entries), start=1):
        try:
            paths = entry_paths(agent, entry)
        except Exception:
            paths = []
        shown = "、".join(f"`{path}`" for path in paths[:MAX_LISTED_PATHS])
        if len(paths) > MAX_LISTED_PATHS:
            shown += f" 等 {len(paths)} 个"
        status = "已撤销" if entry.get("status") == "undone" else "生效中"
        request = str(entry.get("request", "")).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {number} | {status} | {request} | {shown or '-'} |")
    return "\n".join(lines) + _skipped_note(agent)
