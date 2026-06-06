import pytest

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.testing import ScriptedModelClient


def build_agent(tmp_path, outputs, approval_policy="auto"):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    return BunnyByte(
        model_client=ScriptedModelClient(outputs),
        workspace=workspace,
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        approval_policy=approval_policy,
    )


def assistant_contents(app):
    from bunnybyte.tui.widgets import AssistantMessage

    return [message.content for message in app.query(AssistantMessage)]


def rendered_text(widget) -> str:
    rendered = widget.render()
    return getattr(rendered, "plain", str(rendered))


def test_cli_defaults_interactive_tty_mode_to_tui(monkeypatch):
    from bunnybyte.cli import build_arg_parser, interaction_mode

    monkeypatch.setattr(
        "bunnybyte.cli.sys.stdin", type("Stdin", (), {"isatty": lambda self: True})()
    )
    args = build_arg_parser().parse_args(["--cwd", "/tmp/workspace"])

    assert interaction_mode(args) == "tui"


def test_cli_keeps_prompt_as_one_shot_mode():
    from bunnybyte.cli import build_arg_parser, interaction_mode

    args = build_arg_parser().parse_args(["inspect", "tests"])

    assert interaction_mode(args) == "one_shot"


def test_cli_repl_flag_restores_plain_repl():
    from bunnybyte.cli import build_arg_parser, interaction_mode

    args = build_arg_parser().parse_args(["--repl", "--cwd", "/tmp/workspace"])

    assert interaction_mode(args) == "repl"


def test_cli_uses_plain_repl_for_piped_stdin(monkeypatch):
    from bunnybyte.cli import build_arg_parser, interaction_mode

    monkeypatch.setattr(
        "bunnybyte.cli.sys.stdin", type("Stdin", (), {"isatty": lambda self: False})()
    )
    args = build_arg_parser().parse_args(["--cwd", "/tmp/workspace"])

    assert interaction_mode(args) == "repl"


def test_cli_accepts_explicit_tui_flag():
    from bunnybyte.cli import build_arg_parser, interaction_mode

    args = build_arg_parser().parse_args(["--tui", "--cwd", "/tmp/workspace"])

    assert args.tui is True
    assert interaction_mode(args) == "tui"
    assert args.cwd == "/tmp/workspace"


def test_cli_build_agent_defers_new_session_until_first_request(tmp_path, monkeypatch):
    import json

    from bunnybyte.cli import build_agent, build_arg_parser

    class DummyModelClient:
        provider = "openai"
        protocol = "openai"
        supports_prompt_cache = False

        def __init__(self, model="", base_url="", **_kwargs):
            self.model = model
            self.base_url = base_url

        def complete(self, _prompt, _max_new_tokens, **_kwargs):
            return "<final>Done.</final>"

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("bunnybyte.cli.OpenAICompatibleModelClient", DummyModelClient)
    args = build_arg_parser().parse_args(["--cwd", str(tmp_path), "--approval", "auto"])

    agent = build_agent(args)

    sessions_dir = tmp_path / ".bunnybyte" / "sessions"
    assert agent.is_pending_session is True
    assert not list(sessions_dir.glob("*.json"))
    assert not list(sessions_dir.glob("*.events.jsonl"))

    assert agent.ask("hello") == "Done."

    events = [
        json.loads(line)
        for line in agent.session_event_bus.path.read_text(encoding="utf-8").splitlines()
    ]
    assert agent.is_pending_session is False
    assert agent.session_path.exists()
    assert [event["event"] for event in events[:2]] == ["session_started", "turn_started"]


def test_pending_cli_resume_latest_does_not_create_empty_session(tmp_path, monkeypatch):
    from bunnybyte.cli import build_agent as build_cli_agent
    from bunnybyte.cli import build_arg_parser, handle_repl_command

    class DummyModelClient:
        provider = "openai"
        protocol = "openai"
        supports_prompt_cache = False

        def __init__(self, model="", base_url="", **_kwargs):
            self.model = model
            self.base_url = base_url

        def complete(self, _prompt, _max_new_tokens, **_kwargs):
            return "<final>Resumed.</final>"

    first = build_agent(tmp_path, ["<final>First.</final>"])
    assert first.ask("first request") == "First."
    first_id = first.session["id"]
    first.session_store.save(
        {
            "id": "empty-latest",
            "created_at": "2026-06-06T00:00:00+00:00",
            "workspace_root": str(tmp_path),
            "history": [],
        }
    )

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("bunnybyte.cli.OpenAICompatibleModelClient", DummyModelClient)
    args = build_arg_parser().parse_args(["--cwd", str(tmp_path), "--approval", "auto"])
    pending_agent = build_cli_agent(args)
    pending_path = pending_agent.session_path
    session_files_before = {path.name for path in (tmp_path / ".bunnybyte" / "sessions").glob("*.json")}

    handled, _, output = handle_repl_command(pending_agent, "/history")
    assert handled is True
    assert first_id in output
    assert "## Session History" in output
    assert "| # | Topic | ID | Mode | Turns | Updated | Last answer |" in output
    assert not pending_path.exists()

    handled, _, output = handle_repl_command(pending_agent, "/resume latest")

    session_files_after = {path.name for path in (tmp_path / ".bunnybyte" / "sessions").glob("*.json")}
    assert handled is True
    assert first_id in output
    assert pending_agent.session["id"] == first_id
    assert session_files_after == session_files_before


def test_resume_latest_and_history_ignore_internal_dream_sessions(tmp_path):
    from bunnybyte.cli import handle_repl_command

    first = build_agent(tmp_path, ["<final>User answer.</final>"])
    assert first.ask("real user request") == "User answer."
    first_id = first.session["id"]
    first.session_store.save(
        {
            "id": "dream-internal",
            "created_at": "2026-06-07T00:00:00+00:00",
            "topic": "# Dream: Memory Consolidation",
            "workspace_root": str(tmp_path),
            "history": [
                {"role": "user", "content": "# Dream: Memory Consolidation\ninternal"},
                {"role": "assistant", "content": "Dream consolidation complete."},
            ],
            "kind": "internal_dream",
        }
    )

    second = build_agent(tmp_path, [])

    handled, _, output = handle_repl_command(second, "/history")
    assert handled is True
    assert first_id in output
    assert "dream-internal" not in output

    handled, _, output = handle_repl_command(second, "/resume latest")
    assert handled is True
    assert first_id in output
    assert second.session["id"] == first_id


def test_cli_stream_print_emits_full_text(monkeypatch, capsys):
    from bunnybyte.cli import _stream_print

    monkeypatch.setattr("bunnybyte.cli.time.sleep", lambda _seconds: None)

    _stream_print("stream me", chunk_size=3, delay=0)

    assert capsys.readouterr().out == "stream me\n"


def test_status_bar_shows_runtime_identity(tmp_path):
    from bunnybyte.tui.widgets import StatusBar

    agent = build_agent(tmp_path, [])
    status = StatusBar()

    status.update_agent(agent)

    text = rendered_text(status)
    assert "mode default" in text
    assert "session" in text


def test_welcome_banner_shows_runtime_hints_and_activity(tmp_path):
    from bunnybyte.tui.widgets import WelcomeBanner

    agent = build_agent(tmp_path, [])
    agent.model_client.provider = "openai"
    agent.model_client.model = "gpt-test"
    banner = WelcomeBanner()

    banner.update_agent(agent)
    ready_text = rendered_text(banner)
    assert "status ready" in ready_text
    assert "provider openai" in ready_text
    assert "model gpt-test" in ready_text
    assert "context -" in ready_text

    banner.set_activity(True, "running tests")
    busy_text = rendered_text(banner)
    assert "status running tests" in busy_text

    banner.advance_activity()
    assert banner.activity_frame == 1
    assert len(banner._mascot_rows()[0].plain) == len(banner._mascot_rows()[3].plain)


def test_tool_output_plain_text_is_wrapped_as_markdown_code_block():
    from bunnybyte.tui.widgets import _format_tool_output

    output = _format_tool_output("first line\nsecond line")

    assert output.startswith("```text\n")
    assert output.endswith("\n```")
    assert "first line\nsecond line" in output


def test_tool_output_keeps_markdown_tables_renderable():
    from bunnybyte.tui.widgets import _format_tool_output

    table = "| Name | Value |\n| --- | --- |\n| mode | default |"

    assert _format_tool_output(table) == table


def test_status_bar_reads_context_usage_governance_fields():
    from bunnybyte.tui.widgets import StatusBar

    status = StatusBar()

    status.update_context_usage(
        {
            "total_estimated_tokens": 1234,
            "context_window": 200000,
            "free_tokens": 198766,
        }
    )

    assert "context 1234/200000" in rendered_text(status)


def test_cli_plan_mode_and_session_commands_expose_runtime_state(tmp_path):
    from bunnybyte.cli import handle_repl_command

    agent = build_agent(tmp_path, [])

    handled, should_exit, output = handle_repl_command(agent, "/plan refactor-auth")

    assert handled is True
    assert should_exit is False
    assert "## Plan Mode" in output
    assert "| Runtime mode | `plan` |" in output
    assert ".bunnybyte/plans/refactor-auth-plan.md" in output
    assert agent.runtime_mode == "plan"

    handled, _, output = handle_repl_command(agent, "/mode")
    assert handled is True
    assert "## Runtime Mode" in output
    assert "| Runtime mode | `plan` |" in output
    assert "| Plan path | `.bunnybyte/plans/refactor-auth-plan.md` |" in output

    handled, _, output = handle_repl_command(agent, "/session")
    assert handled is True
    assert "## Session Status" in output
    assert "| Session id |" in output
    assert "| Events path |" in output
    assert "| Runtime mode | `plan` |" in output
    assert "| Worker summary |" in output

    handled, _, output = handle_repl_command(agent, "/plan-exit")
    assert handled is True
    assert "## Runtime Mode" in output
    assert "| Runtime mode | `default` |" in output
    assert agent.runtime_mode == "default"


def test_slash_command_registry_suggests_and_parses_subagent():
    from bunnybyte.commands.slash import (
        command_help_text,
        parse_subagent_args,
        resolve_command,
        suggest_commands,
    )

    suggestions = suggest_commands("/sub")

    assert suggestions[0].name == "subagent"
    assert resolve_command("sub").name == "subagent"
    assert resolve_command("provider").name == "provider"

    payload, error = parse_subagent_args("worker --scope README.md,src update docs")

    assert error == ""
    assert payload["subagent_type"] == "worker"
    assert payload["write_scope"] == ["README.md", "src"]
    assert payload["prompt"] == "update docs"

    skill_suggestions = [command.name for command in suggest_commands("/sk")]
    assert "skills" in skill_suggestions
    assert "skill" in skill_suggestions

    help_text = command_help_text()
    assert "## Commands" in help_text
    assert "### Session" in help_text
    assert "| Command | Description |" in help_text
    assert "`/resume <id|index|latest>`" in help_text
    assert "`/provider [name]`" in help_text


@pytest.mark.asyncio
async def test_tui_hides_tool_protocol_from_model_stream_preview():
    from bunnybyte.tui.app import _model_stream_preview

    assert _model_stream_preview("我先查阅一下\n<tool") == ""
    assert _model_stream_preview("<final>Done</final>") == "Done"


@pytest.mark.asyncio
async def test_tui_slash_suggestions_complete_partial_command(tmp_path):
    from bunnybyte.tui.app import BunnyByteTuiApp
    from bunnybyte.tui.widgets import InputBar, SlashSuggestions

    app = BunnyByteTuiApp(build_agent(tmp_path, []))

    async with app.run_test() as pilot:
        bar = app.query_one(InputBar)
        bar.input.value = "/sub"
        bar.update_slash_suggestions()

        suggestions = app.query_one(SlashSuggestions)
        assert suggestions.visible is True
        assert "/subagent" in rendered_text(suggestions)

        await pilot.press("tab")
        await pilot.pause(delay=0.1)

        assert bar.input.value == "/subagent "
        assert suggestions.visible is False


def test_agents_slash_command_shows_worker_status(tmp_path):
    from bunnybyte.cli import handle_repl_command

    agent = build_agent(tmp_path, [])

    handled, should_exit, output = handle_repl_command(agent, "/agents")

    assert handled is True
    assert should_exit is False
    assert "## Subagents" in output
    assert "**Worker summary:**" in output
    assert "| Tool | Purpose |" in output


def test_skills_command_renders_readable_markdown_table(tmp_path):
    from bunnybyte.cli import handle_repl_command

    skill_dir = tmp_path / ".bunnybyte" / "skills" / "deploy"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: deploy
description: Deploy | checklist
argument-hint: target
---
Deploy $ARGUMENTS.
""",
        encoding="utf-8",
    )
    agent = build_agent(tmp_path, [])

    handled, should_exit, output = handle_repl_command(agent, "/skills")

    assert handled is True
    assert should_exit is False
    assert "## Skills" in output
    assert "| Skill | Arguments | Source | Description |" in output
    assert "`/deploy`" in output
    assert "Deploy \\| checklist" in output


def test_history_command_renders_readable_markdown_table(tmp_path):
    from bunnybyte.cli import handle_repl_command

    agent = build_agent(tmp_path, ["<final>Line one.\n\nLine two | with pipe and extra spacing.</final>"])
    assert agent.ask("format history output") == "Line one.\n\nLine two | with pipe and extra spacing."

    handled, should_exit, output = handle_repl_command(agent, "/history")

    assert handled is True
    assert should_exit is False
    assert "## Session History" in output
    assert "| # | Topic | ID | Mode | Turns | Updated | Last answer |" in output
    assert "Line one. Line two \\| with pipe and extra spacing." in output


def test_subagent_slash_command_launches_explore_worker(tmp_path):
    from bunnybyte.cli import handle_repl_command

    agent = build_agent(tmp_path, ["<final>Subagent checked README.</final>"])

    handled, should_exit, output = handle_repl_command(
        agent, "/subagent explore inspect README"
    )

    assert handled is True
    assert should_exit is False
    assert "agent_1" in output
    assert "completed" in output or "started" in output


@pytest.mark.asyncio
async def test_tui_help_command_uses_existing_repl_commands(tmp_path):
    from bunnybyte.tui.app import BunnyByteTuiApp
    from bunnybyte.tui.widgets import InputBar

    agent = build_agent(tmp_path, [])
    app = BunnyByteTuiApp(agent)

    async with app.run_test() as pilot:
        bar = app.query_one(InputBar)
        bar.input.value = "/help"
        await pilot.press("enter")
        await pilot.pause(delay=0.1)

        text = "\n".join(assistant_contents(app))
        assert "## Commands" in text
        assert "/memory" in text


@pytest.mark.asyncio
async def test_tui_dream_command_runs_without_blocking_event_loop(tmp_path, monkeypatch):
    import threading
    import time

    from bunnybyte.tui.app import BunnyByteTuiApp
    from bunnybyte.tui.widgets import InputBar

    started = threading.Event()
    release = threading.Event()

    def slow_command(_agent, text):
        assert text == "/dream"
        started.set()
        assert release.wait(timeout=2)
        return True, False, "Dream consolidation complete."

    monkeypatch.setattr("bunnybyte.tui.app.handle_repl_command", slow_command)
    agent = build_agent(tmp_path, [])
    app = BunnyByteTuiApp(agent)

    async with app.run_test() as pilot:
        bar = app.query_one(InputBar)
        bar.input.value = "/dream"
        await pilot.press("enter")
        await pilot.pause(delay=0.1)

        assert started.wait(timeout=1)
        assert bar.input.disabled is True

        ticked = False

        def mark_tick():
            nonlocal ticked
            ticked = True

        app.call_later(mark_tick)
        await pilot.pause(delay=0.1)
        assert ticked is True

        release.set()
        deadline = time.time() + 2
        while bar.input.disabled and time.time() < deadline:
            await pilot.pause(delay=0.05)

        assert bar.input.disabled is False
        assert "Dream consolidation complete." in "\n".join(assistant_contents(app))


@pytest.mark.asyncio
async def test_tui_enter_executes_selected_slash_suggestion(tmp_path):
    from bunnybyte.tui.app import BunnyByteTuiApp
    from bunnybyte.tui.widgets import InputBar

    agent = build_agent(tmp_path, [])
    app = BunnyByteTuiApp(agent)

    async with app.run_test() as pilot:
        bar = app.query_one(InputBar)
        bar.input.value = "/"
        bar.update_slash_suggestions()

        await pilot.press("enter")
        await pilot.pause(delay=0.1)

        assert bar.input.value == ""
        text = "\n".join(assistant_contents(app))
        assert "## Commands" in text
        assert "/help" in text


@pytest.mark.asyncio
async def test_tui_enter_only_completes_argument_commands(tmp_path):
    from bunnybyte.tui.app import BunnyByteTuiApp
    from bunnybyte.tui.widgets import InputBar

    agent = build_agent(tmp_path, [])
    app = BunnyByteTuiApp(agent)

    async with app.run_test() as pilot:
        bar = app.query_one(InputBar)
        bar.input.value = "/pl"
        bar.update_slash_suggestions()

        await pilot.press("enter")
        await pilot.pause(delay=0.1)

        assert bar.input.value == "/plan "
        text = "\n".join(assistant_contents(app))
        assert "mode: plan" not in text


@pytest.mark.asyncio
async def test_tui_resume_command_renders_loaded_session_history(tmp_path):
    from bunnybyte.tui.app import BunnyByteTuiApp
    from bunnybyte.tui.widgets import InputBar, UserMessage

    first = build_agent(tmp_path, ["<final>Previous answer.</final>"])
    assert first.ask("previous question") == "Previous answer."
    first_id = first.session["id"]

    second = build_agent(tmp_path, [])
    app = BunnyByteTuiApp(second)

    async with app.run_test() as pilot:
        bar = app.query_one(InputBar)
        bar.input.value = f"/resume {first_id}"
        await pilot.press("enter")
        await pilot.pause(delay=0.1)

        text = "\n".join(assistant_contents(app))
        assert "Previous answer." in text
        assert f"resumed session {first_id}" in text
        assert any("previous question" in child.content for child in app.query(UserMessage))
        assert second.session["id"] == first_id


@pytest.mark.asyncio
async def test_tui_resume_command_renders_tool_history(tmp_path):
    from bunnybyte.tui.app import BunnyByteTuiApp
    from bunnybyte.tui.widgets import InputBar, ToolCard

    first = build_agent(tmp_path, [])
    first.record({"role": "user", "content": "inspect files"})
    first.record(
        {
            "role": "tool",
            "name": "list_files",
            "args": {"path": "."},
            "content": "README.md\n",
        }
    )
    first.record({"role": "assistant", "content": "Found README."})
    first_id = first.session["id"]

    second = build_agent(tmp_path, [])
    app = BunnyByteTuiApp(second)

    async with app.run_test() as pilot:
        bar = app.query_one(InputBar)
        bar.input.value = f"/resume {first_id}"
        await pilot.press("enter")
        await pilot.pause(delay=0.1)

        cards = list(app.query(ToolCard))
        assert cards
        assert cards[-1].tool_name == "list_files"
        assert "README.md" in cards[-1].args_summary


@pytest.mark.asyncio
async def test_tui_provider_command_refreshes_welcome_banner(tmp_path):
    from bunnybyte.cli import handle_repl_command
    from bunnybyte.tui.app import BunnyByteTuiApp
    from bunnybyte.tui.widgets import InputBar, StatusBar, WelcomeBanner

    agent = build_agent(tmp_path, [])
    agent.model_client.provider = "openai"
    agent.model_client.protocol = "openai"
    agent.model_client.model = "gpt-test"

    class DeepSeekClient:
        provider = "deepseek"
        protocol = "anthropic"
        model = "deepseek-test"
        base_url = "https://api.deepseek.com/anthropic"
        supports_prompt_cache = False

        def complete(self, _prompt, _max_new_tokens, **_kwargs):
            return "<final>ok</final>"

    def switcher(provider):
        assert provider == "deepseek"
        config = type(
            "Config",
            (),
            {
                "name": "deepseek",
                "protocol": "anthropic",
                "model": "deepseek-test",
                "base_url": "https://api.deepseek.com/anthropic",
            },
        )()
        return DeepSeekClient(), config

    agent.provider_switch_factory = switcher
    app = BunnyByteTuiApp(agent)

    async with app.run_test() as pilot:
        assert "gpt-test" in rendered_text(app.query_one(WelcomeBanner))

        bar = app.query_one(InputBar)
        bar.input.value = "/provider deepseek"
        await pilot.press("enter")
        await pilot.pause(delay=0.1)

        assert "deepseek-test" in rendered_text(app.query_one(WelcomeBanner))
        assert "model deepseek-test" in rendered_text(app.query_one(StatusBar))
        handled, _, output = handle_repl_command(agent, "/provider")
        assert handled is True
        assert "## Provider" in output
        assert "| Model | `deepseek-test` |" in output


@pytest.mark.asyncio
async def test_tui_runs_agent_turn_and_renders_final_answer(tmp_path):
    from bunnybyte.tui.app import BunnyByteTuiApp
    from bunnybyte.tui.widgets import InputBar

    agent = build_agent(tmp_path, ["<final>Done from TUI.</final>"])
    app = BunnyByteTuiApp(agent)

    async with app.run_test() as pilot:
        bar = app.query_one(InputBar)
        bar.input.value = "ship it"
        await pilot.press("enter")
        await pilot.pause(delay=0.3)

        assert "Done from TUI." in "\n".join(assistant_contents(app))


@pytest.mark.asyncio
async def test_tui_renders_tool_card_result(tmp_path):
    from bunnybyte.tui.app import BunnyByteTuiApp
    from bunnybyte.tui.widgets import InputBar, ToolCard

    agent = build_agent(
        tmp_path,
        [
            '<tool name="write_file" path="notes/result.txt"><content>ok\n</content></tool>',
            "<final>Wrote it.</final>",
        ],
    )
    app = BunnyByteTuiApp(agent)

    async with app.run_test() as pilot:
        bar = app.query_one(InputBar)
        bar.input.value = "write a file"
        await pilot.press("enter")
        await pilot.pause(delay=0.5)

        cards = list(app.query(ToolCard))
        assert cards
        assert cards[-1].status == "success"
        assert (tmp_path / "notes" / "result.txt").read_text(encoding="utf-8") == "ok\n"


@pytest.mark.asyncio
async def test_tui_approval_prompt_controls_risky_tool(tmp_path):
    from bunnybyte.tui.app import BunnyByteTuiApp
    from bunnybyte.tui.widgets import ConfirmPrompt, InputBar

    agent = build_agent(
        tmp_path,
        [
            '<tool name="write_file" path="notes/result.txt"><content>ok\n</content></tool>',
            "<final>Wrote it.</final>",
        ],
        approval_policy="ask",
    )
    app = BunnyByteTuiApp(agent)

    async with app.run_test() as pilot:
        bar = app.query_one(InputBar)
        bar.input.value = "write a file"
        await pilot.press("enter")
        await pilot.pause(delay=0.2)

        assert app.query_one(ConfirmPrompt)

        await pilot.press("right")
        await pilot.press("enter")
        await pilot.pause(delay=0.5)

        assert "Wrote it." in "\n".join(assistant_contents(app))
        assert (tmp_path / "notes" / "result.txt").read_text(encoding="utf-8") == "ok\n"


@pytest.mark.asyncio
async def test_tui_ask_user_prompt_returns_selected_choice(tmp_path):
    from bunnybyte.tui.app import BunnyByteTuiApp
    from bunnybyte.tui.widgets import AskUserPrompt, InputBar

    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"ask_user","args":{"question":"Ship?","choices":["no","yes"]}}</tool>',
            "<final>User chose yes.</final>",
        ],
    )
    app = BunnyByteTuiApp(agent)

    async with app.run_test() as pilot:
        bar = app.query_one(InputBar)
        bar.input.value = "ask before shipping"
        await pilot.press("enter")
        await pilot.pause(delay=0.2)

        assert app.query_one(AskUserPrompt)

        await pilot.press("right")
        await pilot.press("enter")
        await pilot.pause(delay=0.5)

        assert "User chose yes." in "\n".join(assistant_contents(app))
