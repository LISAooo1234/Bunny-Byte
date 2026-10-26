"""TUI 状态栏：彩色分段，窄屏时按优先级丢弃次要分段。"""

from __future__ import annotations

import time

from rich.text import Text
from textual.widgets import Static

from ..branding import TUI_ACCENT, TUI_MUTED

SEPARATOR = " · "
BUSY_COLOR = "#ffd43b"
READY_COLOR = "#8ce99a"
WARN_COLOR = "#ff8787"


def compact_number(value) -> str:
    """1234 -> 1.2k，1500000 -> 1.5M；非数字原样返回。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value)
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M".replace(".0M", "M")
    if number >= 1000:
        return f"{number / 1000:.1f}k".replace(".0k", "k")
    return str(number)


def format_elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    return f"{minutes}m{rest:02d}s"


def context_segment(usage: dict | None, bar_width: int = 8) -> Text | None:
    usage = usage or {}
    used = (
        usage.get("total_estimated_tokens")
        or usage.get("used_tokens")
        or usage.get("estimated_tokens")
        or usage.get("total_tokens")
    )
    budget = usage.get("budget") or usage.get("max_tokens") or usage.get("context_window")
    if not used:
        return None
    try:
        used_value = int(used)
        budget_value = int(budget) if budget else 0
    except (TypeError, ValueError):
        return None
    text = Text("context ", style=TUI_MUTED)
    if budget_value <= 0:
        text.append(compact_number(used_value), style=TUI_ACCENT)
        return text
    ratio = min(1.0, used_value / budget_value)
    color = WARN_COLOR if ratio >= 0.8 else (BUSY_COLOR if ratio >= 0.5 else TUI_ACCENT)
    text.append(f"{int(ratio * 100)}% ", style=color)
    filled = int(round(ratio * bar_width))
    text.append("█" * filled, style=color)
    text.append("░" * (bar_width - filled), style="#3b4252")
    text.append(f" {compact_number(used_value)}/{compact_number(budget_value)}", style=TUI_MUTED)
    return text


def token_segment(tokens: dict | None) -> Text | None:
    """本轮 token：↑ 是最后一次请求的输入（即当前上下文），↓ 是本轮所有输出之和。"""
    if tokens is None:
        return None
    last_input = tokens.get("last_input")
    output = tokens.get("output")
    text = Text("tokens ", style=TUI_MUTED)
    if last_input is None and output is None:
        text.append("-", style=TUI_MUTED)
        return text
    text.append(f"↑{compact_number(last_input) if last_input is not None else '-'}", style=TUI_ACCENT)
    text.append(" ")
    text.append(f"↓{compact_number(output) if output is not None else '-'}", style=TUI_ACCENT)
    return text


def labeled(label: str, value: str, style: str = TUI_ACCENT) -> Text:
    text = Text(f"{label} ", style=TUI_MUTED)
    text.append(str(value), style=style)
    return text


def fit_segments(segments: list[tuple[int, Text]], width: int) -> Text:
    """按顺序拼接分段；放不下时先丢优先级数字最大的分段，最后仍过宽就截断。"""
    kept = [item for item in segments if item[1] is not None and item[1].cell_len]

    def join(items):
        line = Text()
        for index, (_priority, segment) in enumerate(items):
            if index:
                line.append(SEPARATOR, style="#4c566a")
            line.append_text(segment)
        return line

    line = join(kept)
    while width > 0 and line.cell_len > width and len(kept) > 1:
        drop = max(range(len(kept)), key=lambda index: (kept[index][0], index))
        kept.pop(drop)
        line = join(kept)
    if width > 0 and line.cell_len > width:
        line.truncate(width, overflow="ellipsis")
    return line


class StatusBar(Static):
    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        margin: 0 1;
        padding: 0 1;
        background: #161a24;
        color: #c5d1e8;
    }
    """

    def __init__(self) -> None:
        super().__init__("")
        self.turns = 0
        self.model = ""
        self.provider = ""
        self.mode = "default"
        self.approval = ""
        self.workers_running = 0
        self.workers_total = 0
        self.usage: dict = {}
        self.tokens: dict | None = None
        self.busy = False
        self.stopping = False
        self.turn_started_at: float | None = None
        self.last_turn_seconds: float | None = None

    @property
    def agent_text(self) -> str:
        return self.render().plain

    def update_agent(self, agent) -> None:
        self.provider = str(getattr(agent.model_client, "provider", "") or "")
        self.model = str(getattr(agent.model_client, "model", "") or "")
        self.mode = str(getattr(agent, "runtime_mode", "default") or "default")
        self.approval = str(getattr(agent, "approval_policy", "") or "")
        workers = list((agent.session.get("workers", {}) or {}).get("items", []) or [])
        self.workers_total = len(workers)
        self.workers_running = sum(1 for item in workers if item.get("status") in {"running", "stopping"})
        self.refresh()

    def update_turns(self, count: int) -> None:
        self.turns = int(count)
        self.refresh()

    def update_context_usage(self, usage: dict | None) -> None:
        self.usage = dict(usage or {})
        self.refresh()

    def start_turn(self) -> None:
        self.busy = True
        self.stopping = False
        self.turn_started_at = time.monotonic()
        self.tokens = {"last_input": None, "output": None}
        self.refresh()

    def set_stopping(self) -> None:
        self.stopping = True
        self.refresh()

    def record_model_usage(self, input_tokens=None, output_tokens=None) -> None:
        if self.tokens is None:
            self.tokens = {"last_input": None, "output": None}
        if isinstance(input_tokens, int) and not isinstance(input_tokens, bool):
            self.tokens["last_input"] = input_tokens
        if isinstance(output_tokens, int) and not isinstance(output_tokens, bool):
            self.tokens["output"] = (self.tokens["output"] or 0) + output_tokens
        self.refresh()

    def finish_turn(self) -> None:
        if self.turn_started_at is not None:
            self.last_turn_seconds = time.monotonic() - self.turn_started_at
        self.busy = False
        self.stopping = False
        self.turn_started_at = None
        self.refresh()

    def tick(self) -> None:
        if self.busy:
            self.refresh()

    def on_resize(self, _event) -> None:
        self.refresh()

    def segments(self) -> list[tuple[int, Text]]:
        """(优先级, 分段)；优先级数字越大越先在窄屏时被丢弃。"""
        if self.busy:
            elapsed = format_elapsed(time.monotonic() - (self.turn_started_at or time.monotonic()))
            word = "正在停止" if self.stopping else "运行中"
            state = Text("● ", style=BUSY_COLOR)
            state.append(f"{word} {elapsed}", style=f"bold {BUSY_COLOR}")
        else:
            state = Text("● ", style=READY_COLOR)
            state.append("就绪", style=f"bold {READY_COLOR}")
            if self.last_turn_seconds is not None:
                state.append(f" 上轮 {format_elapsed(self.last_turn_seconds)}", style=TUI_MUTED)
        segments: list[tuple[int, Text]] = [
            (0, state),
            (1, labeled("model", self.model or "-")),
            (3, labeled("mode", self.mode, style=READY_COLOR if self.mode == "default" else BUSY_COLOR)),
            (2, context_segment(self.usage)),
            (4, token_segment(self.tokens)),
        ]
        if self.workers_total:
            segments.append((3, labeled("agents", f"{self.workers_running}/{self.workers_total}")))
        segments.append((6, labeled("turns", str(self.turns))))
        segments.append((7, labeled("provider", self.provider or "-")))
        hint = "Esc 停止" if self.busy else "/help 命令 · Ctrl+L 清屏"
        segments.append((5, Text(hint, style=TUI_MUTED)))
        return [item for item in segments if item[1] is not None]

    def render(self) -> Text:
        width = int(getattr(self.size, "width", 0) or 0)
        return fit_segments(self.segments(), width)
