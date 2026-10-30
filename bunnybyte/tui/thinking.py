"""运行中的一行实时反馈：`思考中... · 第 2 次请求 · 12s · 已输出 1.2k 字 ≈ 340 tok`。

每次模型请求或工具调用都重新计时（整轮耗时在状态栏里），流式输出时显示字数和估算 token。
"""

from __future__ import annotations

import time

from rich.text import Text
from textual.widgets import Static

from .theme import palette


def estimate_tokens(text: str) -> int:
    """粗估 token：CJK 字符约 1 字 1 token，其他字符约 4 个 1 token。"""
    text = str(text or "")
    wide = sum(1 for char in text if ord(char) >= 0x2E80)
    return wide + (len(text) - wide + 3) // 4


def _compact(number: int) -> str:
    return f"{number / 1000:.1f}k" if number >= 1000 else str(number)


class ThinkingIndicator(Static):
    DEFAULT_CSS = """
    ThinkingIndicator {
        height: 1;
        padding: 0 2;
        color: $bb-thinking;
        background: $bb-bg;
    }
    ThinkingIndicator.hidden {
        display: none;
    }
    """

    FRAMES = ("", ".", "..", "...")

    def __init__(self) -> None:
        super().__init__("")
        self.frame = 0
        self.detail = ""
        self.phase = "思考中"
        self.output_chars = 0
        self.output_tokens = 0
        self._phase_started = time.monotonic()
        self.add_class("hidden")

    def show(self, detail: str = "") -> None:
        self.detail = detail
        self.start_phase("思考中")
        self.remove_class("hidden")

    def hide(self) -> None:
        self.add_class("hidden")
        self.update("")

    def start_phase(self, phase: str, detail: str | None = None) -> None:
        """进入新阶段（一次模型请求 / 一个工具）：计时和输出量从零开始。"""
        self.phase = phase
        if detail is not None:
            self.detail = detail
        self.output_chars = 0
        self.output_tokens = 0
        self._phase_started = time.monotonic()
        self.advance()

    def add_output(self, delta: str) -> None:
        self.output_chars += len(delta)
        self.output_tokens += estimate_tokens(delta)

    def set_detail(self, detail: str) -> None:
        self.detail = detail
        self.advance()

    def elapsed_seconds(self) -> int:
        return int(time.monotonic() - self._phase_started)

    def label(self) -> Text:
        dots = self.FRAMES[self.frame % len(self.FRAMES)]
        colors = palette()
        parts = [Text(f"{self.phase}{dots:<3}", style=colors.thinking_phase)]
        parts.append(Text(f" · {self.elapsed_seconds()}s"))
        if self.output_chars:
            parts.append(
                Text(f" · 已输出 {_compact(self.output_chars)} 字 ≈ {_compact(self.output_tokens)} tok")
            )
        if self.detail:
            parts.append(Text(f"  {self.detail}", style=colors.thinking_detail))
        return Text.assemble(*parts)

    def advance(self) -> None:
        self.frame += 1
        if self.is_mounted:
            self.update(self.label())
