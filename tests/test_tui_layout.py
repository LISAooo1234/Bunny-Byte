"""M1.5 布局与工具卡片：折叠横幅、分段状态栏、统一工具卡片与内联 diff。"""

import pytest
from rich.text import Text

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.testing import ScriptedModelClient


def build_agent(tmp_path, outputs, approval_policy="auto"):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return BunnyByte(
        model_client=ScriptedModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        approval_policy=approval_policy,
    )


async def wait_idle(pilot, app, rounds=40):
    from bunnybyte.tui.widgets import InputBar

    bar = app.query_one(InputBar)
    for _ in range(rounds):
        await pilot.pause(0.1)
        if not bar.input.disabled and not app._is_agent_turn_running():
            return
    raise AssertionError("turn did not finish")


# ---- 状态栏 ----


def test_fit_segments_drops_low_priority_segments_first_and_measures_cjk():
    from bunnybyte.tui.status import fit_segments

    segments = [
        (0, Text("● 运行中 3s")),
        (1, Text("model deepseek-v4-flash")),
        (7, Text("provider deepseek")),
        (5, Text("Esc 停止")),
    ]
    full = fit_segments(segments, 200).plain
    assert full == "● 运行中 3s · model deepseek-v4-flash · provider deepseek · Esc 停止"

    narrow = fit_segments(segments, 48)
    assert narrow.cell_len <= 48
    assert "provider" not in narrow.plain
    assert "● 运行中 3s" in narrow.plain and "model deepseek-v4-flash" in narrow.plain

    tiny = fit_segments(segments, 8)
    assert tiny.cell_len <= 8
    assert tiny.plain.startswith("● 运行")


def test_status_bar_turn_tokens_use_last_input_and_summed_output():
    from bunnybyte.tui.status import StatusBar

    bar = StatusBar()
    bar.start_turn()
    assert "tokens -" in bar.render().plain
    bar.record_model_usage(input_tokens=12000, output_tokens=300)
    bar.record_model_usage(input_tokens=15500, output_tokens=450)
    bar.record_model_usage(input_tokens=None, output_tokens=None)
    text = bar.render().plain
    # 输入只取最后一次请求（每次请求都会重发上下文，求和会重复计数），输出累加。
    assert "tokens ↑15.5k ↓750" in text
    assert "● 运行中 0s" in text
    assert "Esc 停止" in text

    bar.set_stopping()
    assert "正在停止" in bar.render().plain
    bar.finish_turn()
    done = bar.render().plain
    assert "● 就绪 上轮 0s" in done
    assert "tokens ↑15.5k ↓750" in done


def test_status_bar_context_segment_colors_by_fill():
    from bunnybyte.tui.status import context_segment

    assert context_segment({}) is None
    low = context_segment({"total_estimated_tokens": 20000, "context_window": 200000})
    assert low.plain == "context 10% █░░░░░░░ 20k/200k"
    high = context_segment({"total_estimated_tokens": 190000, "context_window": 200000})
    assert high.plain.startswith("context 95% ████████")
    from bunnybyte.tui.theme import palette

    assert any(palette().error in str(span.style) for span in high.spans)


# ---- 工具卡片 ----


def test_shell_card_shows_exit_code_duration_and_turns_red_on_failure():
    from bunnybyte.tui.tool_cards import ToolCard

    ok = ToolCard("run_shell", args={"command": "pytest -q"})
    ok.set_success("exit_code: 0\nstdout:\n3 passed\nstderr:\n(empty)", duration_ms=1530)
    assert ok._label() == "✓ 命令 pytest -q · exit 0 · 1.5s"
    assert ok.status == "success" and ok.has_class("-success")

    failed = ToolCard("run_shell", args={"command": "pytest -q"})
    failed.set_success("exit_code: 1\nstdout:\n1 failed\nstderr:\n(empty)", duration_ms=40)
    assert failed._label() == "✗ 命令 pytest -q · exit 1 · 40ms"
    assert failed.has_class("-error")
    assert failed.expanded is True  # 非 0 退出码像其他失败一样自动展开


def test_card_args_are_clipped_by_display_width_and_markup_is_literal():
    from bunnybyte.tui.tool_cards import ToolCard, card_args_summary

    command = "echo " + "测试" * 60
    summary = card_args_summary("run_shell", {"command": command})
    assert Text(summary).cell_len <= 72
    assert summary.endswith("…")

    # 路径里的方括号不能被当成 Textual 标记吃掉。
    card = ToolCard("read_file", args={"path": "src/[bold]x[/bold].py", "start": 1, "end": 5})
    card.set_success('   1: a\n<read_file_meta path="x" start="1" end="5" returned_lines="1" total_lines="1" eof="true" />', duration_ms=3)
    assert card._label() == "✓ 读取 src/[bold]x[/bold].py:1-5 · 全部 1 行 · 3ms"


def test_patch_card_renders_colored_diff_and_long_diffs_are_clipped():
    from bunnybyte.tui.tool_cards import DIFF_OUTPUT_LINES, ToolCard, render_diff

    card = ToolCard("patch_file", args={"path": "calc.py", "old_text": "return a - b", "new_text": "return a + b  # 修复"})
    card.set_success("patched calc.py", duration_ms=8)
    assert card._label() == "✓ 修改 calc.py · +1 −1 · 8ms"
    lines = card.diff.plain.splitlines()
    assert "-return a - b" in lines and "+return a + b  # 修复" in lines
    styles = {str(span.style) for span in card.diff.spans}
    from bunnybyte.tui.theme import palette

    colors = palette()
    assert f"{colors.diff_add} on {colors.diff_add_bg}" in styles
    assert f"{colors.diff_del} on {colors.diff_del_bg}" in styles
    assert card.expanded is True  # 短 diff 默认展开

    old = "\n".join(f"line {i}" for i in range(1000))
    new = "\n".join(f"LINE {i}" for i in range(1000))
    long_diff = render_diff(old, new)
    long_lines = long_diff.plain.splitlines()
    assert len(long_lines) == DIFF_OUTPUT_LINES + 1
    assert long_lines[-1].startswith("… 还有 ")

    long_card = ToolCard("patch_file", args={"path": "big.txt", "old_text": old, "new_text": new})
    long_card.set_success("patched", duration_ms=1)
    assert long_card.expanded is False  # 长 diff 收起，点开再看


def test_failed_patch_shows_error_instead_of_diff():
    from bunnybyte.tui.tool_cards import ToolCard

    card = ToolCard("patch_file", args={"path": "calc.py", "old_text": "nope", "new_text": "x"})
    card.set_error("error: old_text not found in calc.py")
    assert card.diff is None
    assert card.expanded is True
    assert card._label() == "✗ 修改 calc.py · error: old_text not found in calc.py"


def test_write_card_previews_new_content_and_verify_card_is_distinct():
    from bunnybyte.tui.tool_cards import ToolCard

    write = ToolCard("write_file", args={"path": "notes.md", "content": "# 标题\n正文\n"})
    write.set_success("wrote notes.md")
    assert write._label() == "✓ 写入 notes.md · 写入 2 行（整文件）"
    assert write.diff.plain == "+# 标题\n+正文"

    verify = ToolCard("verify", args={"command": "python -m pytest -q"})
    verify.set_error("failed (exit 1, 2.3s)\nE assert 1 == 2", duration_ms=2300)
    assert verify.has_class("-verify") and verify.has_class("-error")
    assert verify._label() == "✗ 验证 python -m pytest -q · failed (exit 1, 2.3s) · 2.3s"


def test_confirm_prompt_previews_patch_diff():
    from bunnybyte.tui.widgets import ConfirmPrompt, confirm_preview

    args = {"path": "calc.py", "old_text": "a - b", "new_text": "a + b"}
    prompt = ConfirmPrompt("patch_file", "calc.py", preview=confirm_preview("patch_file", args))
    text = prompt.render().plain
    assert "-a - b" in text and "+a + b" in text
    assert confirm_preview("run_shell", {"command": "ls"}) is None


# ---- Pilot：整个 App ----


@pytest.mark.asyncio
async def test_banner_collapses_after_first_turn_and_banner_command_toggles(tmp_path):
    from bunnybyte.tui.app import BunnyByteTuiApp
    from bunnybyte.tui.widgets import AssistantMessage, InputBar, UserMessage, WelcomeBanner

    app = BunnyByteTuiApp(build_agent(tmp_path, ["<final>好的</final>"]))
    async with app.run_test(size=(120, 40)) as pilot:
        banner = app.query_one(WelcomeBanner)
        assert banner.collapsed is False
        tall = banner.size.height
        assert tall >= 8

        bar = app.query_one(InputBar)
        bar.input.value = "你好"
        await pilot.press("enter")
        await wait_idle(pilot, app)
        assert banner.collapsed is True
        assert banner.size.height <= 2
        header = banner.render().plain
        assert "BunnyByte" in header and "/banner 展开" in header and "\n" not in header

        bar.input.value = "/banner"
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert banner.collapsed is False
        # 展开后回到完整横幅（这一宽度下有了 context 行就放不下吉祥物，只剩信息行）。
        assert banner.size.height >= 6
        assert "turns 1" in banner.render().plain
        # /banner 是界面命令，不进聊天记录的用户消息，也不走 REPL 命令处理。
        assert [m.content for m in app.query(UserMessage)] == ["你好"]
        assert [m.content for m in app.query(AssistantMessage)] == ["好的"]

        bar.input.value = "/banner"
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert banner.collapsed is True


@pytest.mark.asyncio
async def test_resumed_session_with_history_starts_collapsed(tmp_path):
    from bunnybyte.tui.app import BunnyByteTuiApp
    from bunnybyte.tui.widgets import WelcomeBanner

    agent = build_agent(tmp_path, [])
    agent.session["history"] = [{"role": "user", "content": "旧问题"}, {"role": "assistant", "content": "旧回答"}]
    app = BunnyByteTuiApp(agent)
    async with app.run_test(size=(120, 40)):
        assert app.query_one(WelcomeBanner).collapsed is True


@pytest.mark.asyncio
async def test_turn_renders_unified_cards_with_duration_diff_and_status_tokens(tmp_path):
    from bunnybyte.tui.app import BunnyByteTuiApp
    from bunnybyte.tui.status import StatusBar
    from bunnybyte.tui.widgets import InputBar, ToolCard

    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"calc.py","start":1,"end":50}}</tool>',
            '<tool name="patch_file" path="calc.py"><old_text>return a - b</old_text><new_text>return a + b</new_text></tool>',
            "<final>修好了。</final>",
        ],
    )
    app = BunnyByteTuiApp(agent)
    async with app.run_test(size=(120, 40)) as pilot:
        bar = app.query_one(InputBar)
        bar.input.value = "修 add"
        await pilot.press("enter")
        await wait_idle(pilot, app)

        read_card, patch_card = list(app.query(ToolCard))
        assert read_card.status == "success" and read_card.duration_ms is not None
        assert read_card._label().startswith("✓ 读取 calc.py:1-50 · 全部 2 行 · ")
        assert patch_card._label().startswith("✓ 修改 calc.py · +1 −1 · ")
        assert patch_card.expanded is True
        assert "+    return a + b" not in patch_card.diff.plain  # diff 基于 old_text/new_text
        assert "+return a + b" in patch_card.diff.plain
        assert (tmp_path / "calc.py").read_text(encoding="utf-8").endswith("return a + b\n")

        status = app.query_one(StatusBar)
        assert status.busy is False
        assert "● 就绪 上轮" in status.render().plain
        assert status.turns == 1
        assert status.render().cell_len <= status.size.width  # 窄于全部分段时丢掉了 turns/provider

        await pilot.press("ctrl+o")
        await pilot.pause(0.05)
        assert all(card.expanded for card in app.query(ToolCard))
        await pilot.press("ctrl+o")
        await pilot.pause(0.05)
        assert not any(card.expanded for card in app.query(ToolCard))


@pytest.mark.asyncio
async def test_parallel_results_land_on_cards_in_call_order(tmp_path):
    from bunnybyte.tui.app import BunnyByteTuiApp
    from bunnybyte.tui.widgets import ToolCard

    app = BunnyByteTuiApp(build_agent(tmp_path, []))
    async with app.run_test(size=(120, 40)) as pilot:
        for path in ("a.py", "b.py"):
            app._handle_runtime_event({"type": "tool_call", "name": "read_file", "args": {"path": path, "start": 1, "end": 9}})
        await pilot.pause(0.05)
        # 并行批次：结果按调用顺序提交，第一条结果属于 a.py。
        app._handle_runtime_event({"type": "tool_result", "name": "read_file", "content": "   1: a", "metadata": {"tool_status": "ok"}, "duration_ms": 11})
        app._handle_runtime_event({"type": "tool_result", "name": "read_file", "content": "   1: b", "metadata": {"tool_status": "ok"}, "duration_ms": 22})
        await pilot.pause(0.05)
        first, second = list(app.query(ToolCard))
        assert first.args["path"] == "a.py" and first.output == "   1: a" and first.duration_ms == 11
        assert second.args["path"] == "b.py" and second.output == "   1: b" and second.duration_ms == 22


def test_engine_events_carry_tool_duration_and_model_usage(tmp_path):
    agent = build_agent(
        tmp_path,
        ['<tool>{"name":"list_files","args":{"path":"."}}</tool>', "<final>ok</final>"],
    )
    events = list(agent.engine.run_turn("看看目录"))
    result = next(event for event in events if event["type"] == "tool_result")
    assert isinstance(result["duration_ms"], int) and result["duration_ms"] >= 0
    parsed = [event for event in events if event["type"] == "model_parsed"]
    assert parsed and all("input_tokens" in event and "output_tokens" in event for event in parsed)


@pytest.mark.asyncio
async def test_history_replay_cards_render_their_output_body(tmp_path):
    """卡片在挂载前就 set_success（/resume 回放、极快的工具）时，输出不能被挂载清空。"""
    from textual.widgets import Markdown

    from bunnybyte.tui.app import BunnyByteTuiApp
    from bunnybyte.tui.widgets import ChatLog, ToolCard

    app = BunnyByteTuiApp(build_agent(tmp_path, []))
    async with app.run_test(size=(120, 40)) as pilot:
        chat = app.query_one(ChatLog)
        chat.add_tool_history("run_shell", {"command": "echo hi"}, "exit_code: 0\nstdout:\nHELLO_FROM_SHELL\nstderr:\n(empty)")
        chat.add_tool_history(
            "read_file",
            {"path": "a.py", "start": 1, "end": 2},
            '   1: READ_LINE_ONE\n<read_file_meta path="a.py" start="1" end="1" returned_lines="1" total_lines="1" eof="true" />',
        )
        # 实时路径：tool_call 与 tool_result 在同一帧里到达。
        app._handle_runtime_event({"type": "tool_call", "name": "search", "args": {"pattern": "x", "path": "."}})
        app._handle_runtime_event({"type": "tool_result", "name": "search", "content": "a.py:1:SEARCH_HIT", "metadata": {"tool_status": "ok"}, "duration_ms": 5})
        await pilot.pause(0.3)
        shell, read, search = list(app.query(ToolCard))
        assert "HELLO_FROM_SHELL" in shell.query_one(Markdown)._markdown
        assert "READ_LINE_ONE" in read.query_one(Markdown)._markdown
        assert "SEARCH_HIT" in search.query_one(Markdown)._markdown
        assert search._label() == "✓ 搜索 x in . · 1 处匹配 · 5ms"


@pytest.mark.asyncio
async def test_expand_all_applies_to_cards_created_afterwards(tmp_path):
    from bunnybyte.tui.app import BunnyByteTuiApp
    from bunnybyte.tui.widgets import ToolCard

    app = BunnyByteTuiApp(build_agent(tmp_path, []))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("ctrl+o")
        app._handle_runtime_event({"type": "tool_call", "name": "list_files", "args": {"path": "."}})
        app._handle_runtime_event({"type": "tool_result", "name": "list_files", "content": "a.py\nb.py", "metadata": {"tool_status": "ok"}})
        await pilot.pause(0.1)
        (card,) = list(app.query(ToolCard))
        assert card.expanded is True


def test_verify_gate_result_event_carries_receipt_duration(tmp_path):
    (tmp_path / "test_ok.py").write_text("assert True\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool name="write_file" path="mod.py"><content>X = 1\n</content></tool>',
            "<final>done</final>",
        ],
    )
    agent.verify_config = agent.verify_config.__class__(**{**agent.verify_config.__dict__, "mode": "auto", "commands": ["python3 -c 'print(1)'"]})
    events = list(agent.engine.run_turn("写 mod.py"))
    verify = [event for event in events if event["type"] == "tool_result" and event["name"] == "verify"]
    assert verify and isinstance(verify[0]["duration_ms"], int)
