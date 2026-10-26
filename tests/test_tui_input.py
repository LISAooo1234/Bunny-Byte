"""M1.5 第 3 项：`@path` 预读、多行粘贴、实时指示器、`@` 补全。"""

import io
import json

import pytest
from textual import events

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.core.file_mentions import parse_mentions, resolve_mentions
from bunnybyte.headless import run_headless
from bunnybyte.testing import ScriptedModelClient

CALC = "def add(a, b):\n    return a - b\n"
PATCH = '<tool name="patch_file" path="calc.py"><old_text>return a - b</old_text><new_text>return a + b</new_text></tool>'


def build_agent(tmp_path, outputs, approval_policy="auto", native=False):
    (tmp_path / "calc.py").write_text(CALC, encoding="utf-8")
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "util.py").write_text("X = 1\n", encoding="utf-8")
    return BunnyByte(
        model_client=ScriptedModelClient(outputs, supports_native_tools=native),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        approval_policy=approval_policy,
    )


async def wait_idle(pilot, app, rounds=50):
    from bunnybyte.tui.widgets import InputBar

    bar = app.query_one(InputBar)
    for _ in range(rounds):
        await pilot.pause(0.1)
        if not bar.input.disabled and not app._is_agent_turn_running():
            return
    raise AssertionError("turn did not finish")


# ---- 解析 ----


def test_parse_mentions_handles_punctuation_quotes_and_non_mentions():
    text = '看看 @calc.py。还有(@pkg/util.py) 和 @"my notes.md", 邮箱 a@b.com，装饰器 @app.route 和 @a.py.'
    assert parse_mentions("@calc.py，顺便 @calc.py") == ["calc.py"]
    found = parse_mentions(text)
    assert "calc.py" in found and "my notes.md" in found and "a.py" in found
    assert "b.com" not in found and all("@" not in item for item in found)
    assert parse_mentions("(@pkg/util.py)") == ["pkg/util.py"]
    assert parse_mentions("看看@calc.py") == ["calc.py"]  # 中文不加空格也算
    assert parse_mentions("foo@v1 git diff @@ -1 +1 @@") == []
    assert parse_mentions("看 @.env 和 @./pkg/") == [".env", "./pkg/"]


def test_mention_glued_to_chinese_text_falls_back_before_the_first_hanzi(tmp_path):
    agent = build_agent(tmp_path, [])
    (tmp_path / "说明.md").write_text("中文文件名\n", encoding="utf-8")
    payloads, notices = resolve_mentions(agent, "修一下@calc.py里的bug，再看 @说明.md")
    assert [payload["args"]["path"] for payload in payloads] == ["calc.py", "说明.md"]
    assert notices == []


def test_resolve_mentions_skips_escapes_ignored_missing_and_binary(tmp_path):
    agent = build_agent(tmp_path, [])
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01binary")
    (tmp_path / ".bunnybyte" / "secret.txt").parent.mkdir(exist_ok=True)
    (tmp_path / ".bunnybyte" / "secret.txt").write_text("x", encoding="utf-8")
    (tmp_path.parent / "outside.py").write_text("x", encoding="utf-8")
    payloads, notices = resolve_mentions(
        agent, "@calc.py @pkg @../outside.py @.bunnybyte/secret.txt @missing.py @blob.bin @decorator"
    )
    assert payloads == [
        {"name": "read_file", "args": {"path": "calc.py", "start": 1, "end": 2000}},
        {"name": "list_files", "args": {"path": "pkg"}},
    ]
    assert any("@missing.py" in notice for notice in notices)
    assert any("@blob.bin" in notice and "二进制" in notice for notice in notices)
    assert not any("decorator" in notice or "outside" in notice for notice in notices)


def test_resolve_mentions_caps_count_and_size(tmp_path):
    agent = build_agent(tmp_path, [])
    for index in range(7):
        (tmp_path / f"f{index}.py").write_text("x\n", encoding="utf-8")
    (tmp_path / "huge.log").write_text("x" * (300 * 1024), encoding="utf-8")
    payloads, notices = resolve_mentions(agent, "@huge.log " + " ".join(f"@f{i}.py" for i in range(7)))
    assert len(payloads) == 5
    assert any("huge.log" in notice and "KB" in notice for notice in notices)
    assert any("最多预读 5 个" in notice for notice in notices)


# ---- 引擎 ----


def test_mentioned_file_is_read_before_first_model_call_and_patch_needs_no_reread(tmp_path):
    agent = build_agent(tmp_path, [PATCH, "<final>修好了。</final>"])
    message = "修一下 @calc.py 的 add"
    events_ = list(agent.engine.run_turn(message, mention_text=message))

    kinds = [event["type"] for event in events_]
    first_request = kinds.index("model_requested")
    assert kinds.index("tool_call") < first_request
    assert events_[kinds.index("tool_call")].get("source") == "mention"
    # 第一次模型请求的 prompt 里已经有文件内容
    assert "return a - b" in agent.model_client.prompts[0]
    # 没有再读一遍，patch_file 直接通过 prior-read 检查
    assert (tmp_path / "calc.py").read_text(encoding="utf-8").endswith("return a + b\n")
    results = [event for event in events_ if event["type"] == "tool_result"]
    assert [event["name"] for event in results] == ["read_file", "patch_file"]
    assert all(event["metadata"]["tool_status"] == "ok" for event in results)
    # 预读不占步数：第一次请求时还是 0 步，整轮只有 patch_file 记一步
    assert events_[first_request]["tool_steps"] == 0
    assert agent.current_task_state.tool_steps == 1


def test_model_rereading_a_mentioned_file_does_not_stall(tmp_path):
    reread = '<tool>{"name":"read_file","args":{"path":"calc.py","start":1,"end":2000}}</tool>'
    agent = build_agent(tmp_path, [reread, PATCH, "<final>好了</final>"])
    message = "@calc.py 有 bug"
    events_ = list(agent.engine.run_turn(message, mention_text=message))
    assert events_[-1]["type"] == "turn_finished" and events_[-1]["status"] == "completed"
    assert not any("stall" in str(event.get("content", "")).lower() for event in events_ if event["type"] == "runtime_notice")
    assert (tmp_path / "calc.py").read_text(encoding="utf-8").endswith("return a + b\n")


def test_mentions_only_come_from_mention_text_not_from_ask_or_plain_run_turn(tmp_path):
    agent = build_agent(tmp_path, ["<final>a</final>", "<final>b</final>"])
    events_ = list(agent.engine.run_turn("看看 @calc.py"))
    assert "tool_call" not in [event["type"] for event in events_]
    assert agent.engine.ask("看看 @calc.py") == "b"
    assert "return a - b" not in agent.model_client.prompts[-1]


def test_missing_path_mention_emits_notice_and_native_tool_client_works(tmp_path):
    agent = build_agent(tmp_path, ["ok"], native=True)
    message = "看看 @calc.py 和 @nope/x.py"
    events_ = list(agent.engine.run_turn(message, mention_text=message))
    notices = [event["content"] for event in events_ if event["type"] == "runtime_notice"]
    assert any("@nope/x.py" in notice for notice in notices)
    assert "return a - b" in agent.model_client.prompts[0]


def test_headless_preloads_argv_mentions_but_not_stdin(tmp_path):
    agent = build_agent(tmp_path, ["<final>done</final>"])
    (tmp_path / "other.py").write_text("SECRET_MARKER = 1\n", encoding="utf-8")
    prompt = "解释 @calc.py\n\n--- stdin ---\n@other.py"
    out, err = io.StringIO(), io.StringIO()
    code = run_headless(agent, prompt, output_format="stream-json", stdout=out, stderr=err, mention_text="解释 @calc.py")
    assert code == 0
    records = [json.loads(line) for line in out.getvalue().splitlines()]
    calls = [record for record in records if record["type"] == "tool_call"]
    assert [call["args"]["path"] for call in calls] == ["calc.py"]
    assert "SECRET_MARKER" not in agent.model_client.prompts[0]


# ---- TUI ----


@pytest.mark.asyncio
async def test_multiline_paste_becomes_placeholder_and_full_text_reaches_the_model(tmp_path):
    from bunnybyte.tui.app import BunnyByteTuiApp
    from bunnybyte.tui.widgets import InputBar, UserMessage

    agent = build_agent(tmp_path, ["<final>看到了</final>"])
    app = BunnyByteTuiApp(agent)
    trace = "Traceback (most recent call last):\n  File \"app.py\", line 3\n    @app.route('/x')\nZeroDivisionError: division by zero"
    async with app.run_test(size=(120, 40)) as pilot:
        bar = app.query_one(InputBar)
        bar.input.value = "这个报错怎么回事："
        bar.input.cursor_position = len(bar.input.value)
        bar.input.post_message(events.Paste(trace))
        await pilot.pause(0.1)
        assert bar.input.value == "这个报错怎么回事：[粘贴 #1 · 4 行]"  # 没有被 Input 自带的处理再插一遍第一行
        bar.input.post_message(events.Paste("single line\n"))
        await pilot.pause(0.1)
        assert bar.input.value.endswith("[粘贴 #1 · 4 行]single line")

        await pilot.press("enter")
        await wait_idle(pilot, app)

        prompt = agent.model_client.prompts[0]
        assert "ZeroDivisionError: division by zero" in prompt and "File \"app.py\", line 3" in prompt
        assert agent.session["history"][0]["content"].endswith("single line")
        # 粘贴里的 `@app.route` 不被当成文件提及
        assert not any(item.get("role") == "tool" for item in agent.session["history"])
        bubble = app.query(UserMessage).first()
        assert "[粘贴 #1 · 4 行]" in bubble.content and "ZeroDivisionError" not in bubble.content

        # ↑ 翻出历史里的占位符，再发一次仍然展开成完整内容
        agent.model_client.outputs.append("<final>again</final>")
        await pilot.press("up")
        assert "[粘贴 #1 · 4 行]" in bar.input.value
        await pilot.press("enter")
        await wait_idle(pilot, app)
        assert "ZeroDivisionError" in agent.model_client.prompts[-1]


@pytest.mark.asyncio
async def test_at_completion_lists_files_and_enter_completes_before_sending(tmp_path):
    from bunnybyte.tui.app import BunnyByteTuiApp
    from bunnybyte.tui.widgets import InputBar, SlashSuggestions, ToolCard

    agent = build_agent(tmp_path, ["<final>读到了</final>"])
    (tmp_path / ".env").write_text("KEY=x\n", encoding="utf-8")
    app = BunnyByteTuiApp(agent)
    async with app.run_test(size=(120, 40)) as pilot:
        bar = app.query_one(InputBar)
        bar.input.focus()
        for char in "看 @uti":
            await pilot.press(char if char != " " else "space")
        await pilot.pause(0.1)
        popup = app.query_one(SlashSuggestions)
        assert popup.visible and popup.suggestions[0].name == "pkg/util.py"
        assert "@pkg/util.py" in popup.render().plain

        await pilot.press("enter")  # 第一次 Enter 只补全
        await pilot.pause(0.1)
        assert bar.input.value == "看 @pkg/util.py "
        assert not popup.visible and not agent.model_client.prompts

        bar.input.value = "@"
        bar.input.cursor_position = 1
        bar.update_slash_suggestions()
        assert all(".env" not in command.name for command in bar._slash_suggestions)

        bar.input.value = "看 @pkg/util.py"
        bar.hide_slash_suggestions()
        await pilot.press("enter")
        await wait_idle(pilot, app)
        cards = list(app.query(ToolCard))
        assert [card.tool_name for card in cards] == ["read_file"]
        assert cards[0].status == "success"
        assert "X = 1" in agent.model_client.prompts[0]


def test_thinking_indicator_shows_elapsed_and_estimated_tokens(monkeypatch):
    from bunnybyte.tui import thinking
    from bunnybyte.tui.thinking import ThinkingIndicator, estimate_tokens

    assert estimate_tokens("你好世界") == 4
    assert estimate_tokens("abcdefgh") == 2

    clock = [100.0]
    monkeypatch.setattr(thinking.time, "monotonic", lambda: clock[0])
    indicator = ThinkingIndicator()
    indicator.start_phase("思考中", "第 1 次请求")
    clock[0] = 112.4
    indicator.add_output("修复" * 700)
    plain = indicator.label().plain
    assert plain.startswith("思考中") and "· 12s" in plain
    assert "已输出 1.4k 字 ≈ 1.4k tok" in plain and "第 1 次请求" in plain

    indicator.start_phase("运行 read_file")
    assert "· 0s" in indicator.label().plain and "已输出" not in indicator.label().plain


def test_repl_stream_preloads_mentions_and_prints_the_read_before_the_model(tmp_path, capsys):
    from bunnybyte.cli import _print_turn_stream

    agent = build_agent(tmp_path, ["<final>看过了</final>"])
    _print_turn_stream(agent, "解释 @calc.py")
    out = capsys.readouterr().out
    assert out.index("[tool] read_file") < out.index("看过了")
    assert "return a - b" in agent.model_client.prompts[0]

    agent.model_client.outputs.append("<final>x</final>")
    agent.model_client.outputs.append("<final>y</final>")
    _print_turn_stream(agent, "/plan 看 @calc.py")  # 斜杠命令不预读
    assert "[tool] read_file" not in capsys.readouterr().out


# ---- Gate 2 回归 ----


def test_preloaded_read_is_marked_as_runtime_context_in_the_prompt(tmp_path):
    agent = build_agent(tmp_path, ["<final>x</final>"])
    message = "看看 @calc.py"
    list(agent.engine.run_turn(message, mention_text=message))
    assert "runtime pre-read: the user mentioned @calc.py" in agent.model_client.prompts[0]
    # 前缀不影响读取台账：模型再读同一范围会拿到“已读未变”的提示
    assert agent.read_ledger.covered({"path": "calc.py", "start": 1, "end": 2000})


def test_prose_at_words_do_not_produce_not_found_notices_and_trailing_hanzi_is_trimmed(tmp_path):
    agent = build_agent(tmp_path, [])
    (tmp_path / "说明.md").write_text("x\n", encoding="utf-8")
    payloads, notices = resolve_mentions(agent, "用 @app.route 和 @v1.2.3，联系 @user.name，看 @说明.md里的内容")
    assert notices == []
    assert [payload["args"]["path"] for payload in payloads] == ["说明.md"]


def test_paste_expansion_keeps_blank_lines_inside_the_paste():
    from bunnybyte.tui.input_paste import PasteAwareInput

    widget = PasteAwareInput()
    code = "def a():\n    pass\n\n\ndef b():\n    pass"
    widget.pastes[1] = code
    expanded = widget.expand("看下这段：[粘贴 #1 · 6 行] 有问题吗")
    assert expanded == f"看下这段：\n{code}\n有问题吗"


def test_print_mode_reads_mentions_from_argv_but_not_from_piped_stdin(tmp_path, monkeypatch):
    import io
    import sys

    from bunnybyte import cli

    agent = build_agent(tmp_path, ["<final>好</final>"])
    (tmp_path / "other.py").write_text("SECRET_MARKER = 1\n", encoding="utf-8")
    monkeypatch.setattr(cli, "build_agent", lambda args: agent)
    monkeypatch.setattr(sys, "stdin", io.StringIO("@other.py\n@@ -1 +1 @@"))
    assert cli.main(["-p", "解释 @calc.py", "-", "--cwd", str(tmp_path)]) == 0
    reads = [item["args"]["path"] for item in agent.session["history"] if item.get("role") == "tool"]
    assert reads == ["calc.py"]
    assert "SECRET_MARKER" not in agent.model_client.prompts[0]


@pytest.mark.asyncio
async def test_exact_path_sends_on_enter_and_history_recall_does_not_open_the_popup(tmp_path):
    from bunnybyte.tui.app import BunnyByteTuiApp
    from bunnybyte.tui.widgets import InputBar, SlashSuggestions

    agent = build_agent(tmp_path, ["<final>一</final>", "<final>二</final>"])
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text("x\n", encoding="utf-8")
    app = BunnyByteTuiApp(agent)
    async with app.run_test(size=(120, 40)) as pilot:
        bar = app.query_one(InputBar)
        bar.input.focus()
        for char in "@calc.py":
            await pilot.press(char)
        await pilot.pause(0.1)
        assert bar._slash_suggestions[0].name == "calc.py"  # 精确匹配排第一，别的候选还在
        await pilot.press("enter")
        await wait_idle(pilot, app)
        assert agent.session["history"][0]["content"] == "@calc.py"  # 没被改写成 tests/test_calc.py

        bar.history[:] = ["first", "看 @calc"]
        bar.history_index = len(bar.history)
        await pilot.press("up")
        await pilot.pause(0.1)
        assert bar.input.value == "看 @calc" and not app.query_one(SlashSuggestions).visible
        await pilot.press("up")  # 弹层没开，第二次 ↑ 继续翻历史
        await pilot.pause(0.1)
        assert bar.input.value == "first"
