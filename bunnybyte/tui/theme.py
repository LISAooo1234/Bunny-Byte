"""TUI 配色：所有颜色都在这里，深色 / 浅色两套语义色板。

CSS 里用 `$bb-<名字>` 变量（由 App.get_css_variables 按当前主题注入），Rich 文本在
render() 里通过 `palette()` 取当前色板，所以 `/theme` 切换后整屏立即重绘，不用重启。
"""

from __future__ import annotations

import os

from textual.theme import Theme

THEME_NAMES = ("dark", "light")
THEME_CHOICES = (*THEME_NAMES, "auto")
DEFAULT_THEME = "dark"
TEXTUAL_THEME = {"dark": "bunnybyte-dark", "light": "bunnybyte-light"}

# 名字 -> (深色, 浅色)
_TOKENS: dict[str, tuple[str, str]] = {
    # 底色与层次
    "bg": ("#0f1117", "#fafbfc"),
    "panel": ("#15161c", "#f0f2f6"),
    "surface": ("#161a24", "#e6eaf0"),
    "code-bg": ("#0b0f16", "#eef1f5"),
    "banner-bg": ("#10131b", "#dde8f6"),
    # 文字
    "text": ("#eef4ff", "#1b2230"),
    "text-soft": ("#c5d1e8", "#2f3a4f"),
    "title": ("#dbe4ff", "#1d2b4f"),
    "muted": ("#9ca8bb", "#55606f"),
    "faint": ("#7d8aa5", "#58627a"),
    "separator": ("#616a7c", "#7c838e"),
    "accent": ("#7fc8ff", "#0b5fad"),
    # 状态
    "success": ("#8ce99a", "#17692f"),
    "warning": ("#ffd43b", "#8a5d00"),
    "error": ("#ff8787", "#c0262d"),
    "info": ("#74c0fc", "#1a68ad"),
    "meter-empty": ("#646a76", "#7d838a"),
    # 边框
    "border": ("#5f6774", "#808997"),
    "banner-border": ("#66b6ff", "#4383c5"),
    "header-rule": ("#646977", "#7a7f86"),
    "input-border": ("#6fd13e", "#3c9a1a"),
    "busy-border": ("#f59e0b", "#c27c00"),
    # 消息
    "user-text": ("#b7f5c1", "#14612a"),
    "user-border": ("#2f9e44", "#2f9e44"),
    "assistant-border": ("#5c6570", "#808997"),
    # diff
    "diff-add": ("#8ce99a", "#0f5c25"),
    "diff-add-bg": ("#12261a", "#d8f3e0"),
    "diff-del": ("#ffa8a8", "#9b1218"),
    "diff-del-bg": ("#2c1416", "#fbdfe0"),
    # 工具卡片
    "card-border": ("#4a6ea8", "#6888b8"),
    "card-ok-border": ("#36794f", "#3e8f5c"),
    "card-error-border": ("#c92a2a", "#d9534f"),
    "verify-ok-bg": ("#102016", "#e1f4e6"),
    "verify-ok-border": ("#40c057", "#2e9942"),
    "verify-error-bg": ("#241214", "#fbe3e3"),
    "verify-error-border": ("#fa5252", "#d63c3c"),
    # 审批提示
    "confirm-bg": ("#211d12", "#fff5d9"),
    "confirm-text": ("#ffe8a1", "#5a4000"),
    "confirm-border": ("#f59f00", "#b07400"),
    "confirm-hint": ("#c9a227", "#6e5000"),
    # ask_user 提示
    "ask-bg": ("#121f2b", "#e4f0fb"),
    "ask-text": ("#d0ebff", "#0f3a5f"),
    "ask-border": ("#4dabf7", "#388bd0"),
    "ask-choice": ("#74c0fc", "#1a5f9e"),
    "ask-selected": ("#a5d8ff", "#08406f"),
    # 记忆范围选择
    "scope-bg": ("#171d2a", "#eaedfe"),
    "scope-text": ("#e7f5ff", "#1f2a5c"),
    "scope-border": ("#748ffc", "#5c74e0"),
    "scope-choice": ("#91a7ff", "#3446a8"),
    "scope-selected": ("#bac8ff", "#1c2a85"),
    # 斜杠 / @ 补全弹层
    "popup-bg": ("#111827", "#f2f4fa"),
    "popup-border": ("#5066ab", "#7a88b7"),
    "popup-text": ("#d8dcff", "#27304d"),
    "popup-dim": ("#a7a9bb", "#555b70"),
    "popup-selected": ("#66d9ef", "#0b5fad"),
    # 运行指示
    "thinking": ("#8b93a7", "#55606f"),
    "thinking-phase": ("#c3c9d9", "#2f3a4f"),
    "thinking-detail": ("#808899", "#58627a"),
    # Stop 按钮
    "stop-border": ("#6b7280", "#7e848e"),
    "stop-bg": ("#161b24", "#eceef2"),
    "stop-text": ("#cbd5e1", "#2d3442"),
    "stop-hover-border": ("#fb7185", "#e0485f"),
    "stop-hover-bg": ("#2a1218", "#fde5e9"),
    "stop-hover-text": ("#ffe4e6", "#7a1424"),
    "stop-active-bg": ("#241117", "#fdecef"),
    "stop-active-text": ("#fecdd3", "#8a1c2c"),
    "stopping-bg": ("#261904", "#fff3d6"),
    "stopping-text": ("#fde68a", "#6b4a00"),
    # Fork / Rollback 按钮
    "fork-border": ("#4dabf7", "#388bd0"),
    "fork-bg": ("#10243a", "#e3effa"),
    "fork-text": ("#d0ebff", "#0f3a5f"),
    "fork-hover-border": ("#74c0fc", "#1a68ad"),
    "fork-hover-bg": ("#123456", "#cfe3f6"),
    "fork-focus-border": ("#91d5ff", "#0b5fad"),
    "fork-focus-bg": ("#1f4f7a", "#b9d7f2"),
    "rollback-border": ("#f59f00", "#bb7900"),
    "rollback-bg": ("#2b2112", "#fff3d6"),
    "rollback-text": ("#ffe8a1", "#5a4000"),
    "rollback-hover-border": ("#ffd43b", "#b07800"),
    "rollback-hover-bg": ("#3a2a10", "#ffe9b3"),
    "rollback-focus-border": ("#ffe066", "#8a5d00"),
    "rollback-focus-bg": ("#5c3d08", "#ffdf8f"),
    "button-strong": ("#ffffff", "#0b2a45"),
    # 吉祥物像素（branding.py 里的原色 -> 当前主题下的颜色；浅色背景上要更深的轮廓）
    "mascot-outline": ("#72adff", "#2f6fc9"),
    "mascot-face": ("#f7fbff", "#ffffff"),
    "mascot-ear": ("#d9ebff", "#a9cdf3"),
    "mascot-cheek-ring": ("#7cb8ff", "#3f86d8"),
    "mascot-cheek": ("#b7dcff", "#8fc0f0"),
    "mascot-feature": ("#17304e", "#17304e"),
    "mascot-nose": ("#21405f", "#21405f"),
}


class Palette:
    """一套色板；属性名里的下划线对应 token 里的短横线：`palette().diff_add_bg`。"""

    def __init__(self, name: str, colors: dict[str, str]) -> None:
        self.name = name
        self.colors = dict(colors)

    def __getattr__(self, attr: str) -> str:
        try:
            return self.colors[attr.replace("_", "-")]
        except KeyError:
            raise AttributeError(attr) from None

    def css_variables(self) -> dict[str, str]:
        return {f"bb-{key}": value for key, value in self.colors.items()}


PALETTES = {
    name: Palette(name, {key: pair[index] for key, pair in _TOKENS.items()})
    for index, name in enumerate(THEME_NAMES)
}
_active = DEFAULT_THEME


def palette(name: str | None = None) -> Palette:
    return PALETTES.get(name or _active, PALETTES[DEFAULT_THEME])


def mascot_color(color: str | None) -> str | None:
    """吉祥物像素颜色按当前主题换算；branding.py 只描述形状和原色。"""
    from .. import branding

    tokens = {
        branding.OUTLINE: "mascot-outline",
        branding.FACE_FILL: "mascot-face",
        branding.EAR_HIGHLIGHT: "mascot-ear",
        branding.CHEEK_RING: "mascot-cheek-ring",
        branding.CHEEK_FILL: "mascot-cheek",
        branding.FEATURE: "mascot-feature",
        branding.NOSE: "mascot-nose",
    }
    if color is None:
        return None
    token = tokens.get(color)
    return palette().colors[token] if token else color


def active_theme() -> str:
    return _active


def set_active_theme(name: str) -> None:
    global _active
    _active = name if name in PALETTES else DEFAULT_THEME


def textual_theme(name: str) -> Theme:
    """给 Textual 内置部件（Input、Collapsible、滚动条、Markdown 代码块）用的真主题字段。"""
    colors = palette(name)
    return Theme(
        name=TEXTUAL_THEME[name],
        primary=colors.accent,
        secondary=colors.info,
        accent=colors.accent,
        warning=colors.warning,
        error=colors.error,
        success=colors.success,
        foreground=colors.text,
        background=colors.bg,
        surface=colors.surface,
        panel=colors.panel,
        dark=name == "dark",
        variables=colors.css_variables(),
    )


def theme_for_textual(theme: Theme | None) -> str:
    """任何 Textual 主题（包括 Ctrl+P 里选的内置主题）都映射到深色或浅色色板。"""
    if theme is None:
        return DEFAULT_THEME
    for name, textual_name in TEXTUAL_THEME.items():
        if theme.name == textual_name:
            return name
    return "dark" if theme.dark else "light"


def detect_terminal_theme(env=None) -> str:
    """`auto`：终端设置了 COLORFGBG（如 "0;15"）且背景是浅色（7 或 15）时用浅色，其余一律深色。"""
    env = os.environ if env is None else env
    value = str(env.get("COLORFGBG", "") or "").strip()
    background = value.split(";")[-1].strip() if value else ""
    return "light" if background in {"7", "15"} else "dark"


def resolve_theme(value: str | None, env=None) -> str:
    """把 dark / light / auto（或空）解析成实际色板名；无法识别的值按深色处理。"""
    choice = str(value or "").strip().lower()
    if choice == "auto":
        return detect_terminal_theme(env)
    return choice if choice in PALETTES else DEFAULT_THEME
