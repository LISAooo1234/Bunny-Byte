"""多行粘贴：Textual 的 Input 只保留粘贴内容的第一行，堆栈、日志、代码块会被悄悄截断。

这里的 PasteAwareInput 把多行粘贴存起来，输入框里只放一个占位符
`[粘贴 #1 · 23 行]`；发送时 `expand()` 把占位符换回完整文本。
"""

from __future__ import annotations

import re

from textual import events
from textual.widgets import Input

_TOKEN_RE = re.compile(r"\[粘贴 #(\d+) · \d+ 行\]")
MAX_PASTE_CHARS = 200_000
# 粘贴缓冲跨消息保留（↑ 翻出的历史里还是占位符，发送时照样展开），只留最近这么多段。
MAX_KEPT_PASTES = 50


def paste_token(number: int, text: str) -> str:
    return f"[粘贴 #{number} · {len(text.splitlines())} 行]"


class PasteAwareInput(Input):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.pastes: dict[int, str] = {}
        self._paste_counter = 0

    def _on_paste(self, event: events.Paste) -> None:
        # Textual 沿 MRO 依次调用每个类的 _on_paste；prevent_default 让 Input 自带的那份不再执行。
        event.prevent_default()
        event.stop()
        text = str(event.text or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
        if not text:
            return
        if "\n" in text:
            if len(text) > MAX_PASTE_CHARS:
                text = text[:MAX_PASTE_CHARS] + f"\n…（粘贴内容超过 {MAX_PASTE_CHARS // 1000}K 字符，后面已截断）"
                self.notify("粘贴内容太长，只保留了前 200K 字符。", severity="warning")
            self._paste_counter += 1
            self.pastes[self._paste_counter] = text
            for stale in sorted(self.pastes)[:-MAX_KEPT_PASTES]:
                del self.pastes[stale]
            text = paste_token(self._paste_counter, text)
        selection = self.selection
        if selection.is_empty:
            self.insert_text_at_cursor(text)
        else:
            self.replace(text, *selection)

    def expand(self, text: str) -> str:
        """把占位符换成粘贴的原文（原样保留粘贴里的空行）；用户删掉的占位符对应的内容就不再发送。"""
        text = str(text or "")
        parts: list[str] = []
        position = 0
        missing = 0
        for match in _TOKEN_RE.finditer(text):
            body = self.pastes.get(int(match.group(1)))
            before = text[position : match.start()]
            if body is None:
                missing += 1
                parts.append(before + match.group(0))
            else:
                before = before.rstrip(" ")
                parts.append(before + ("\n" if before and not before.endswith("\n") else "") + body + "\n")
            position = match.end()
        parts.append(text[position:].lstrip(" ") if position else text)
        if missing:
            self.notify(f"有 {missing} 段粘贴内容已不在缓冲里，按占位符原文发送。", severity="warning")
        return "".join(parts).strip()

    @staticmethod
    def strip_tokens(text: str) -> str:
        """去掉占位符，只剩用户亲手敲的文字（`@path` 只从这里解析，粘贴的代码里的 `@decorator` 不算）。"""
        return _TOKEN_RE.sub(" ", str(text or ""))
