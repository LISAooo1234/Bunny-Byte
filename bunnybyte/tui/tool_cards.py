"""TUI 工具卡片：统一的一行摘要（图标 · 工具 · 参数 · 结果 · 耗时），可展开看输出或 diff。"""

from __future__ import annotations

import difflib
import re

from rich.text import Text
from textual.content import Content
from textual.widgets import Collapsible, Markdown, Static

from .theme import palette

TOOL_TITLES = {
    "read_file": "读取",
    "search": "搜索",
    "list_files": "列目录",
    "run_shell": "命令",
    "write_file": "写入",
    "patch_file": "修改",
    "agent": "子任务",
    "send_message": "发消息",
    "task_stop": "停止任务",
    "verify": "验证",
    "hook": "钩子",
    "critic": "审查",
    "fix_proof": "修复证明",
}
COMPACT_TOOLS = {"read_file", "search", "list_files"}
DIFF_TOOLS = {"write_file", "patch_file"}
DIFF_INPUT_LINES = 400
DIFF_OUTPUT_LINES = 60
DIFF_EXPAND_LINES = 12
WRITE_PREVIEW_LINES = 40
ARGS_CELLS = 72

# 状态 -> (图标, 色板里的颜色名)
ICONS = {"running": ("…", "info"), "success": ("✓", "success"), "error": ("✗", "error")}


def tool_title(name: str) -> str:
    if name.startswith("mcp__") and "__" in name[5:]:
        server, tool = name[5:].split("__", 1)  # 服务器名里不允许 "__"，第一个分隔符就是边界
        return f"MCP {server}/{tool}"
    return TOOL_TITLES.get(name, name.replace("_", " "))


def format_duration(duration_ms) -> str:
    try:
        value = int(duration_ms)
    except (TypeError, ValueError):
        return ""
    if value < 1000:
        return f"{value}ms"
    if value < 60_000:
        return f"{value / 1000:.1f}s"
    minutes, seconds = divmod(value // 1000, 60)
    return f"{minutes}m{seconds:02d}s"


def clip_cells(text: str, limit: int = ARGS_CELLS) -> str:
    """按终端显示宽度截断（中文占 2 格），超出时以 … 结尾。"""
    line = Text(" ".join(str(text or "").split()))
    if line.cell_len <= limit:
        return line.plain
    line.truncate(max(1, limit - 1))
    return line.plain + "…"


def card_args_summary(name: str, args: dict | None) -> str:
    args = args or {}
    if name == "critic":
        return clip_cells(str(args.get("model", "")))
    if name == "fix_proof":
        tests = [str(item) for item in args.get("tests") or []]
        return clip_cells("撤回源码重跑 " + "、".join(tests)) if tests else ""
    if name in {"run_shell", "verify", "hook"}:
        commands = args.get("commands")
        if isinstance(commands, list) and commands:
            return clip_cells(" ; ".join(str(item) for item in commands))
        return clip_cells(str(args.get("command", "")))
    if name == "read_file":
        path = str(args.get("path", "."))
        start, end = args.get("start"), args.get("end")
        if start is None and end is None:
            return clip_cells(path)
        return clip_cells(f"{path}:{start if start is not None else 1}-{end if end is not None else ''}")
    if name in {"write_file", "patch_file", "list_files"}:
        return clip_cells(str(args.get("path", ".")))
    if name == "search":
        return clip_cells(f"{args.get('pattern', '')} in {args.get('path', '.')}")
    if name == "agent":
        return clip_cells(str(args.get("task", args.get("description", ""))))
    if name == "send_message":
        return clip_cells(str(args.get("to", "")))
    if name == "task_stop":
        return clip_cells(str(args.get("task_id", "")))
    parts = [f"{key}={value}" for key, value in sorted(args.items())]
    return clip_cells(" ".join(parts))


def shell_exit_code(output: str):
    match = re.search(r"^exit_code:\s*(-?\d+)", str(output or ""), re.MULTILINE)
    return int(match.group(1)) if match else None


def summarize_tool_output(name: str, output: str, args: dict | None = None) -> str:
    text = str(output or "").strip()
    args = args or {}
    if text.startswith("error:"):
        return clip_cells(text.splitlines()[0], 80)
    if not text:
        return ""
    if name == "read_file":
        meta = re.search(r'<read_file_meta [^>]*returned_lines="(\d+)"[^>]*total_lines="(\d+)"[^>]*eof="(true|false)"', text)
        if meta:
            lines, total, eof = meta.groups()
            if eof == "true" and lines == total:
                return f"全部 {lines} 行"
            return f"{lines} 行 / 共 {total} 行"
        if text.startswith("File range already read"):
            return "已读取过 · 复用缓存"
        line_count = sum(1 for line in text.splitlines() if re.match(r"\s*\d+:", line))
        return f"{line_count} 行"
    if name == "search":
        if text == "(no matches)":
            return "无匹配"
        count = len([line for line in text.splitlines() if line.strip()])
        return f"{count} 处匹配"
    if name == "list_files":
        entries = [line.strip() for line in text.splitlines() if line.strip()]
        if text == "(empty)" or not entries:
            return "空目录"
        if len(entries) <= 3:
            return ", ".join(entries)
        return f"{len(entries)} 项"
    if name == "run_shell":
        code = shell_exit_code(text)
        return f"exit {code}" if code is not None else ""
    if name == "write_file":
        count = len(str(args.get("content", "")).splitlines())
        return f"写入 {count} 行（整文件）"
    if name == "patch_file":
        added, removed = diff_counts(str(args.get("old_text", "")), str(args.get("new_text", "")))
        return f"+{added} −{removed}"
    if name in {"verify", "hook", "critic", "fix_proof"}:
        return clip_cells(text.splitlines()[0], 60)
    return ""


def _diff_lines(old_text: str, new_text: str) -> list[str]:
    old_lines = str(old_text or "").splitlines()[:DIFF_INPUT_LINES]
    new_lines = str(new_text or "").splitlines()[:DIFF_INPUT_LINES]
    lines = list(difflib.unified_diff(old_lines, new_lines, lineterm="", n=2))
    return [line for line in lines if not line.startswith(("---", "+++"))]


def diff_counts(old_text: str, new_text: str) -> tuple[int, int]:
    lines = _diff_lines(old_text, new_text)
    added = sum(1 for line in lines if line.startswith("+"))
    removed = sum(1 for line in lines if line.startswith("-"))
    return added, removed


def render_diff(old_text: str, new_text: str, max_lines: int = DIFF_OUTPUT_LINES) -> Text:
    """彩色 unified diff（+ 绿 / − 红 / @@ 灰），超过 max_lines 行截断并注明剩余行数。"""
    lines = _diff_lines(old_text, new_text)
    return _style_diff(lines, max_lines)


def render_new_file(content: str, max_lines: int = WRITE_PREVIEW_LINES) -> Text:
    """write_file 的新内容预览（整文件覆盖，不与磁盘上的旧内容比较）。"""
    lines = ["+" + line for line in str(content or "").splitlines()[:DIFF_INPUT_LINES]]
    return _style_diff(lines, max_lines)


def _style_diff(lines: list[str], max_lines: int) -> Text:
    colors = palette()
    text = Text()
    shown = lines[:max_lines]
    for index, line in enumerate(shown):
        if index:
            text.append("\n")
        if line.startswith("@@"):
            text.append(line, style=colors.faint)
        elif line.startswith("+"):
            text.append(line, style=f"{colors.diff_add} on {colors.diff_add_bg}")
        elif line.startswith("-"):
            text.append(line, style=f"{colors.diff_del} on {colors.diff_del_bg}")
        else:
            text.append(line, style=colors.muted)
    hidden = len(lines) - len(shown)
    if hidden > 0:
        text.append(f"\n… 还有 {hidden} 行未显示", style=colors.faint)
    return text


def tool_diff(name: str, args: dict | None) -> Text | None:
    args = args or {}
    if name == "patch_file" and "old_text" in args:
        diff = render_diff(str(args.get("old_text", "")), str(args.get("new_text", "")))
        return diff if diff.plain else None
    if name == "write_file" and str(args.get("content", "")):
        return render_new_file(str(args.get("content", "")))
    return None


class ToolCard(Static):
    DEFAULT_CSS = """
    ToolCard {
        height: auto;
        margin: 0;
        padding: 0;
        background: transparent;
        border-left: outer $bb-card-border;
    }
    ToolCard.-success { border-left: outer $bb-card-ok-border; }
    ToolCard.-error { border-left: outer $bb-card-error-border; }
    ToolCard.-verify.-success { border-left: thick $bb-verify-ok-border; background: $bb-verify-ok-bg; }
    ToolCard.-verify.-error { border-left: thick $bb-verify-error-border; background: $bb-verify-error-bg; }
    ToolCard Collapsible {
        padding: 0;
        border-top: none;
        background: transparent;
    }
    ToolCard Collapsible:focus-within { background-tint: $foreground 3%; }
    ToolCard CollapsibleTitle { padding: 0 1; }
    ToolCard Collapsible > Contents { padding: 0 0 1 3; }
    ToolCard .tool-output {
        max-height: 16;
        color: $bb-text-soft;
        padding: 0 1;
        background: $bb-code-bg;
        overflow-x: hidden;
    }
    ToolCard .tool-output Markdown { height: auto; width: 100%; }
    ToolCard .tool-diff { height: auto; padding: 0 1; background: $bb-code-bg; }
    ToolCard .hidden { display: none; }
    """

    def __init__(self, tool_name: str, args_summary: str = "", args: dict | None = None, expanded: bool = False) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.args = dict(args or {})
        self.args_summary = card_args_summary(tool_name, args) if args is not None else clip_cells(args_summary)
        self.status = "running"
        self.output = ""
        self.result_summary = ""
        self.duration_ms = None
        self.compact = tool_name in COMPACT_TOOLS
        self.diff: Text | None = None
        self.add_class("-running")
        if self.compact:
            self.add_class("compact")
        if tool_name in {"verify", "critic", "fix_proof"}:
            self.add_class("-verify")
        self.expanded = bool(expanded)
        self.force_expanded = bool(expanded)
        self._collapsible: Collapsible | None = None
        self._output_widget: Markdown | None = None
        self._diff_widget: Static | None = None

    def compose(self):
        # 历史回放时 set_success 可能先于挂载调用；Markdown 挂载时会用初始内容覆盖
        # 挂载前 update() 写进去的内容，所以输出必须作为初始内容传入。
        self._output_widget = Markdown(self._output_markdown(), classes="tool-output")
        self._diff_widget = Static("", classes="tool-diff hidden")
        self._collapsible = Collapsible(
            self._diff_widget,
            self._output_widget,
            title=Content.from_rich_text(self.label_text()),
            collapsed=not self.expanded,
            collapsed_symbol="▸",
            expanded_symbol="▾",
        )
        if self.status != "running":
            self._update_body(self.output)
        yield self._collapsible

    def on_mount(self) -> None:
        # 卡片在挂载完成前就拿到结果（工具很快、或同一帧里连发事件）时再写一次输出。
        if self.status != "running":
            self.call_after_refresh(self._update_body, self.output)

    def _output_markdown(self) -> str:
        from .widgets import _format_tool_output

        if self.status == "running" or self.diff is not None:
            return ""
        return _format_tool_output(self.output, self.tool_name)

    def set_expanded(self, expanded: bool) -> None:
        self.expanded = bool(expanded)
        if self._collapsible is not None:
            self._collapsible.collapsed = not self.expanded

    def label_text(self) -> Text:
        colors = palette()
        icon, color_name = ICONS.get(self.status, ("•", "muted"))
        color = getattr(colors, color_name)
        text = Text()
        text.append(f"{icon} ", style=f"bold {color}")
        text.append(tool_title(self.tool_name), style=f"bold {colors.title}")
        if self.args_summary:
            text.append(f" {self.args_summary}", style=colors.text_soft)
        details = []
        if self.result_summary:
            details.append((self.result_summary, color if self.status == "error" else colors.muted))
        duration = format_duration(self.duration_ms) if self.duration_ms is not None else ""
        if duration:
            details.append((duration, colors.faint))
        for value, style in details:
            text.append(" · ", style=colors.separator)
            text.append(value, style=style)
        return text

    def _label(self) -> str:
        return self.label_text().plain

    def _refresh_label(self) -> None:
        for name in ("-running", "-success", "-error"):
            self.remove_class(name)
        self.add_class(f"-{self.status}")
        if self._collapsible is not None:
            # Content 相等只比较纯文本：主题切换后文字不变、颜色变了，reactive 会跳过，要强制写入。
            title = Content.from_rich_text(self.label_text())
            header = getattr(self._collapsible, "_title", None)
            if header is not None and hasattr(header, "_update_label"):
                self._collapsible.set_reactive(type(self._collapsible).title, title)
                header.set_reactive(type(header).label, title)
                header._update_label()
            else:  # Textual 内部结构变了：至少保证文字正确
                self._collapsible.title = title

    def refresh_theme(self) -> None:
        """主题切换后重新生成带颜色的标题和 diff（它们是按旧色板生成的 Rich 文本）。"""
        if self.diff is not None:
            self.diff = tool_diff(self.tool_name, self.args)
        self._refresh_label()
        if self.status != "running":
            self._update_body(self.output)

    def set_success(self, output: str = "", duration_ms=None) -> None:
        self.status = "success"
        self.output = output
        self.duration_ms = duration_ms
        self.result_summary = summarize_tool_output(self.tool_name, output, self.args)
        if self.tool_name == "run_shell":
            code = shell_exit_code(output)
            if code not in (None, 0):
                self.status = "error"
        self.diff = tool_diff(self.tool_name, self.args)
        self._refresh_label()
        self._update_body(output)
        short_diff = self.diff is not None and len(self.diff.plain.splitlines()) <= DIFF_EXPAND_LINES
        self.set_expanded(self.force_expanded or short_diff or self.status == "error")

    def set_error(self, output: str = "", duration_ms=None) -> None:
        self.status = "error"
        self.output = output
        self.duration_ms = duration_ms
        self.result_summary = summarize_tool_output(self.tool_name, output, self.args)
        self.diff = None
        if self.compact:
            self.compact = False
            self.remove_class("compact")
        self._refresh_label()
        self._update_body(output)
        self.set_expanded(True)

    def _update_body(self, output: str) -> None:
        if self._diff_widget is not None:
            self._diff_widget.update(self.diff if self.diff is not None else "")
            self._diff_widget.set_class(self.diff is None, "hidden")
        if self._output_widget is not None:
            self._output_widget.update(self._output_markdown())
            self._output_widget.set_class(self.diff is not None, "hidden")


def confirm_preview(name: str, args: dict | None) -> Text | None:
    """审批提示里给 patch_file / write_file 预览将要做的改动。"""
    return tool_diff(name, args)
