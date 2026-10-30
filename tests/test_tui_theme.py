"""M1.5 收尾：深色 / 浅色主题、颜色只在 theme.py、对比度、/theme 实时切换、布局快照。"""

import os
import re
from pathlib import Path

import pytest

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.testing import ScriptedModelClient

ROOT = Path(__file__).resolve().parents[1]
TUI_DIR = ROOT / "bunnybyte" / "tui"
SNAPSHOT = Path(__file__).parent / "snapshots" / "tui_turn.txt"


@pytest.fixture(autouse=True)
def reset_theme():
    from bunnybyte.tui import theme

    theme.set_active_theme("dark")
    yield
    theme.set_active_theme("dark")


def build_agent(tmp_path, outputs):
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    return BunnyByte(
        model_client=ScriptedModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        approval_policy="auto",
    )


TURN = [
    '<tool>{"name":"read_file","args":{"path":"calc.py","start":1,"end":50}}</tool>',
    '<tool name="patch_file" path="calc.py"><old_text>return a - b</old_text><new_text>return a + b</new_text></tool>',
    "<final>修好了：add 改成 a + b。</final>",
]


async def run_turn(pilot, app, text="修一下 add"):
    from bunnybyte.tui.widgets import InputBar

    bar = app.query_one(InputBar)
    bar.input.value = text
    await pilot.press("enter")
    for _ in range(60):
        await pilot.pause(0.1)
        if not bar.input.disabled and not app._is_agent_turn_running():
            return
    raise AssertionError("turn did not finish")


# ---- 颜色只在一个地方 ----


def test_tui_modules_have_no_color_literals_outside_theme():
    hex_color = re.compile(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?\b|\brgba?\(")
    # 只适合深色背景的具名颜色（bold green / yellow / cyan ...）在浅色终端上看不清，也要走色板：
    # style="red"、Text(x, "red")、[red]...[/] 标记都算。
    names = r"(?:bold |dim |italic )*(?:bright_)?(?:green|yellow|red|cyan|blue|magenta|white|black)\b"
    named = re.compile(rf"(style=f?\"[^\"]*\b{names}|Text\([^)]*,\s*\"{names}|\[{names}[^\]]*\])")
    offenders = []
    for path in sorted(TUI_DIR.glob("*.py")):
        if path.name == "theme.py":
            continue
        source = path.read_text(encoding="utf-8")
        for number, line in enumerate(source.splitlines(), 1):
            if hex_color.search(line) or named.search(line) or re.search(r"\bTUI_[A-Z_]+\b(?!_CSS)", line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert offenders == []


def test_every_css_variable_used_by_the_tui_exists_in_both_palettes():
    from bunnybyte.tui.theme import PALETTES

    used = set()
    for path in TUI_DIR.glob("*.py"):
        used |= set(re.findall(r"\$bb-([a-z-]+)", path.read_text(encoding="utf-8")))
    assert used, "TUI CSS should use $bb-* variables"
    for palette in PALETTES.values():
        assert sorted(used - set(palette.colors)) == []
    assert set(PALETTES["dark"].colors) == set(PALETTES["light"].colors)


def _luminance(color: str) -> float:
    channels = [int(color.lstrip("#")[index : index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(foreground: str, background: str) -> float:
    high, low = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


TEXT_TOKENS = "text text-soft title muted faint accent success warning error info user-text thinking thinking-phase thinking-detail"
BACKGROUNDS = "bg panel surface banner-bg code-bg"
PAIRS = [
    ("diff-add", "diff-add-bg"),
    ("diff-del", "diff-del-bg"),
    ("confirm-text", "confirm-bg"),
    ("confirm-hint", "confirm-bg"),
    ("warning", "confirm-bg"),
    ("ask-text", "ask-bg"),
    ("ask-choice", "ask-bg"),
    ("scope-text", "scope-bg"),
    ("scope-choice", "scope-bg"),
    ("popup-text", "popup-bg"),
    ("popup-dim", "popup-bg"),
    ("popup-selected", "popup-bg"),
    ("fork-text", "fork-bg"),
    ("rollback-text", "rollback-bg"),
    ("stop-text", "stop-bg"),
]
# 边框 / 分隔符 / 进度条底色 -> 它实际所在的背景
BORDERS = {
    "border": "panel",
    "banner-border": "banner-bg",
    "header-rule": "banner-bg",
    "input-border": "bg",
    "busy-border": "bg",
    "user-border": "bg",
    "assistant-border": "bg",
    "card-border": "bg",
    "card-ok-border": "bg",
    "card-error-border": "bg",
    "verify-ok-border": "verify-ok-bg",
    "verify-error-border": "verify-error-bg",
    "confirm-border": "confirm-bg",
    "ask-border": "ask-bg",
    "scope-border": "scope-bg",
    "popup-border": "popup-bg",
    "stop-border": "stop-bg",
    "stop-hover-border": "stop-active-bg",
    "fork-border": "fork-bg",
    "rollback-border": "rollback-bg",
    "separator": "surface",
    "meter-empty": "surface",
    "mascot-outline": "banner-bg",
    "mascot-feature": "mascot-face",
}


@pytest.mark.parametrize("name", ["dark", "light"])
def test_palette_text_meets_wcag_aa_and_borders_are_visible(name):
    from bunnybyte.tui.theme import PALETTES

    colors = PALETTES[name].colors
    pairs = [(fg, bg) for fg in TEXT_TOKENS.split() for bg in BACKGROUNDS.split()] + PAIRS
    low_text = [(fg, bg, round(contrast(colors[fg], colors[bg]), 2)) for fg, bg in pairs if contrast(colors[fg], colors[bg]) < 4.5]
    low_border = [
        (token, background, round(contrast(colors[token], colors[background]), 2))
        for token, background in BORDERS.items()
        if contrast(colors[token], colors[background]) < 3
    ]
    border_tokens = {key for key in colors if key.endswith("-border")}
    assert sorted(border_tokens - set(BORDERS) - {"stop-hover-border", "fork-hover-border", "fork-focus-border", "rollback-hover-border", "rollback-focus-border"}) == []
    assert low_text == [] and low_border == []


# ---- 选择主题 ----


def test_resolve_theme_and_auto_detection():
    from bunnybyte.tui.theme import detect_terminal_theme, resolve_theme

    assert resolve_theme(None) == "dark"
    assert resolve_theme("LIGHT") == "light"
    assert resolve_theme("solarized") == "dark"
    assert resolve_theme("auto", env={"COLORFGBG": "0;15"}) == "light"
    assert resolve_theme("auto", env={"COLORFGBG": "15;0"}) == "dark"
    assert resolve_theme("auto", env={"COLORFGBG": "0;default;7"}) == "light"
    assert detect_terminal_theme({}) == "dark"  # Terminal.app 等不设 COLORFGBG：保持深色


def test_theme_precedence_cli_env_project_global(tmp_path, monkeypatch):
    from bunnybyte import config

    global_path = tmp_path / "global.toml"
    global_path.write_text('[tui]\ntheme = "light"\n', encoding="utf-8")
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", global_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    assert config.resolve_tui_theme(start=repo, env={}) == "light"  # 全局配置
    (repo / ".bunnybyte.toml").write_text('[tui]\ntheme = "auto"\n', encoding="utf-8")
    assert config.resolve_tui_theme(start=repo, env={}) == "auto"  # 项目覆盖全局
    assert config.resolve_tui_theme(start=repo, env={"BUNNYBYTE_THEME": "dark"}) == "dark"
    assert config.resolve_tui_theme(start=repo, env={"BUNNYBYTE_THEME": "dark"}, cli_value="light") == "light"


def test_setup_rewrite_keeps_tui_section():
    from bunnybyte.config import _render_config_values

    rendered = _render_config_values({"top": {"provider": "deepseek"}, "providers": {}, "sandbox": {}, "tui": {"theme": "light"}})
    assert '[tui]\ntheme = "light"' in rendered


def test_cli_parses_theme_flag_and_non_tui_paths_explain_theme_command(tmp_path):
    from bunnybyte.cli import build_arg_parser, handle_repl_command, tui_theme_choice

    args = build_arg_parser().parse_args(["--theme", "light", "--cwd", str(tmp_path)])
    assert tui_theme_choice(args) == "light"
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args(["--theme", "pink"])
    agent = build_agent(tmp_path, [])
    handled, should_exit, output = handle_repl_command(agent, "/theme light")
    assert handled and not should_exit and "只在 TUI" in output


# ---- Pilot：整个 App ----


@pytest.mark.asyncio
async def test_theme_command_repaints_css_and_rich_text_without_restart(tmp_path):
    from textual.color import Color

    from bunnybyte.tui.app import BunnyByteTuiApp
    from bunnybyte.tui.status import StatusBar
    from bunnybyte.tui.theme import PALETTES, active_theme
    from bunnybyte.tui.widgets import AssistantMessage, ChatLog, ToolCard

    dark, light = PALETTES["dark"], PALETTES["light"]
    app = BunnyByteTuiApp(build_agent(tmp_path, TURN))
    async with app.run_test(size=(120, 40)) as pilot:
        await run_turn(pilot, app)
        read_card, patch_card = list(app.query(ToolCard))
        assert app.screen.styles.background == Color.parse(dark.bg)
        assert dark.diff_add_bg in {str(span.style).split(" on ")[-1] for span in patch_card.diff.spans}

        app.query_one("Input").value = "/theme light"
        await pilot.press("enter")
        await pilot.pause(0.3)

        assert active_theme() == "light" and app.theme == "bunnybyte-light"
        assert app.screen.styles.background == Color.parse(light.bg)
        assert app.query_one(ChatLog).styles.background == Color.parse(light.bg)
        assert app.query_one(StatusBar).styles.background == Color.parse(light.surface)
        # 已经生成的 Rich 文本（卡片标题、diff）也换成浅色色板。
        title_colors = {span.style.foreground for span in read_card._collapsible._title.label.spans}
        assert Color.parse(light.success) in title_colors and Color.parse(dark.success) not in title_colors
        diff_styles = {str(span.style) for span in patch_card.diff.spans}
        assert f"{light.diff_add} on {light.diff_add_bg}" in diff_styles
        assert light.success in str(app.query_one(StatusBar).render().spans)
        assert "已切换到浅色主题" in list(app.query(AssistantMessage))[-1].content

        app.query_one("Input").value = "/theme"  # 不带参数：来回切换
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert active_theme() == "dark"
        assert app.screen.styles.background == Color.parse(dark.bg)

        app.query_one("Input").value = "/theme pink"
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert "用法：/theme" in list(app.query(AssistantMessage))[-1].content
        assert active_theme() == "dark"


@pytest.mark.asyncio
async def test_app_starts_in_requested_theme_and_survives_builtin_textual_themes(tmp_path):
    from textual.color import Color

    from bunnybyte.tui.app import BunnyByteTuiApp
    from bunnybyte.tui.theme import PALETTES, active_theme
    from bunnybyte.tui.widgets import ChatLog

    app = BunnyByteTuiApp(build_agent(tmp_path, []), theme="light")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        assert app.screen.styles.background == Color.parse(PALETTES["light"].bg)
        # Ctrl+P 的“切换主题”能选 Textual 自带主题：没有 $bb-* 变量也不能让 CSS 崩掉，按明暗换色板。
        app.theme = "textual-dark"
        await pilot.pause(0.3)
        assert active_theme() == "dark"
        assert app.query_one(ChatLog).styles.background == Color.parse(PALETTES["dark"].bg)
        app.theme = "solarized-light"
        await pilot.pause(0.3)
        assert active_theme() == "light"


def test_mascot_pixels_follow_the_theme():
    from bunnybyte import branding
    from bunnybyte.tui.theme import PALETTES, set_active_theme
    from bunnybyte.tui.widgets import WelcomeBanner

    def colors_in(rows):
        found = set()
        for row in rows:
            for span in row.spans:
                found |= {part for part in str(span.style).split() if part.startswith("#")}
        return found

    set_active_theme("light")
    light = colors_in(WelcomeBanner()._mascot_rows())
    assert PALETTES["light"].mascot_outline in light
    assert branding.OUTLINE not in light and branding.FACE_FILL not in light
    set_active_theme("dark")
    assert branding.OUTLINE in colors_in(WelcomeBanner()._mascot_rows())


def test_invalid_theme_value_warns_and_falls_back(tmp_path, monkeypatch, capsys):
    from bunnybyte.cli import build_arg_parser, tui_theme_choice

    monkeypatch.setenv("BUNNYBYTE_THEME", "pink")
    assert tui_theme_choice(build_arg_parser().parse_args(["--cwd", str(tmp_path)])) == "dark"
    assert "未知的 TUI 主题 'pink'" in capsys.readouterr().err


def test_progress_panel_clips_cjk_todos_by_display_width(tmp_path):
    from rich.text import Text

    from bunnybyte.tui.widgets import ProgressPanel

    agent = build_agent(tmp_path, [])
    agent.session["todos"] = {"items": [{"content": "修复" * 80, "status": "in_progress"}]}
    panel = ProgressPanel()
    panel.update_agent(agent)
    line = str(panel.render()).splitlines()[1]
    assert line.endswith("…")
    assert Text(line).cell_len <= 102  # "→ " + 100 格


# ---- 布局快照（纯文本，不含颜色；颜色由上面的测试负责） ----


def normalize_screen(lines: list[str], tmp_path, session_id: str) -> str:
    text = "\n".join(line.rstrip() for line in lines).rstrip() + "\n"
    text = text.replace(tmp_path.name, "<tmp>").replace(session_id[-10:], "<session>")
    # 标题行的 emoji 宽度取决于 wcwidth 表、行尾提示的截断位置随之变化：只保留到 approval。
    header, _, rest = text.partition("\n")
    header = re.sub(r"approval (\S+).*", r"approval \1", header.replace("🐰", "")).strip()
    text = header + "\n" + rest
    text = re.sub(r"\b\d+(\.\d+)?(ms|s)\b", "<t>", text)
    text = re.sub(r"\d+(\.\d+)?[kM]?/\d+(\.\d+)?[kM]?", "<n>/<n>", text)
    return re.sub(r"context \d+%", "context <p>%", text)


@pytest.mark.asyncio
async def test_turn_layout_matches_snapshot(tmp_path):
    from bunnybyte.tui.app import BunnyByteTuiApp

    agent = build_agent(tmp_path, TURN)
    app = BunnyByteTuiApp(agent)
    async with app.run_test(size=(100, 30)) as pilot:
        await run_turn(pilot, app)
        await pilot.pause(0.2)
        lines = [strip.text for strip in app.screen._compositor.render_strips()]
    screen = normalize_screen(lines, tmp_path, str(agent.session.get("id", "")))
    if os.environ.get("BUNNYBYTE_UPDATE_SNAPSHOTS") == "1":
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(screen, encoding="utf-8")
    assert SNAPSHOT.exists(), "缺少布局快照：用 BUNNYBYTE_UPDATE_SNAPSHOTS=1 生成"
    assert screen == SNAPSHOT.read_text(encoding="utf-8"), "布局变了：确认无误后用 BUNNYBYTE_UPDATE_SNAPSHOTS=1 重新生成"
