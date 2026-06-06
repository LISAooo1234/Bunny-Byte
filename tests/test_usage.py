from pathlib import Path
import pytest

from bunnybyte.testing import ScriptedModelClient
from bunnybyte import BunnyByte, SessionStore, WorkspaceContext


def build_agent(tmp_path, outputs=None, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    return BunnyByte(
        model_client=ScriptedModelClient(outputs or []),
        workspace=workspace,
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        approval_policy="auto",
        **kwargs,
    )


def test_usage_command_reports_provider_model_and_last_usage(tmp_path):
    from bunnybyte.cli import handle_repl_command

    agent = build_agent(tmp_path, ["<final>Done.</final>"])
    agent.model_client.model = "gpt-test"
    agent.model_client.base_url = "https://example.com/v1"
    agent.model_client.last_completion_metadata = {
        "input_tokens": 10,
        "output_tokens": 5,
        "cached_tokens": 3,
        "provider_attempts": 2,
        "provider_retry_count": 1,
    }
    agent.ask("hello")

    handled, _, output = handle_repl_command(agent, "/usage")

    assert handled is True
    assert "model: gpt-test" in output
    assert "base url host: example.com" in output
    assert "last input tokens: 10" in output
    assert "last output tokens: 5" in output
    assert "last cached tokens: 3" in output


def test_model_command_updates_current_runtime_only(tmp_path):
    from bunnybyte.cli import handle_repl_command

    agent = build_agent(tmp_path, [])
    agent.model_client.model = "old-model"

    handled, _, output = handle_repl_command(agent, "/model new-model")

    assert handled is True
    assert output == "model: new-model"
    assert agent.model_client.model == "new-model"
    assert not (Path(tmp_path) / ".bunnybyte.toml").exists()


def test_provider_command_switches_cli_runtime_only(tmp_path, monkeypatch):
    from bunnybyte.cli import (
        build_agent as build_cli_agent,
        build_arg_parser,
        handle_repl_command,
    )

    class DummyOpenAIClient:
        supports_prompt_cache = False

        def __init__(self, model="", base_url="", api_key="", temperature=None, timeout=0):
            self.model = model
            self.base_url = base_url
            self.api_key = api_key
            self.temperature = temperature
            self.timeout = timeout
            self.last_completion_metadata = {}

    class DummyAnthropicClient(DummyOpenAIClient):
        pass

    for name in (
        "BUNNYBYTE_PROVIDER",
        "BUNNYBYTE_API_KEY",
        "BUNNYBYTE_MODEL",
        "BUNNYBYTE_BASE_URL",
        "BUNNYBYTE_PROTOCOL",
        "OPENAI_MODEL",
        "DEEPSEEK_MODEL",
        "DEEPSEEK_API_BASE",
        "DEEPSEEK_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    monkeypatch.setattr("bunnybyte.cli.OpenAICompatibleModelClient", DummyOpenAIClient)
    monkeypatch.setattr(
        "bunnybyte.cli.AnthropicCompatibleModelClient", DummyAnthropicClient
    )
    args = build_arg_parser().parse_args(
        [
            "--cwd",
            str(tmp_path),
            "--config",
            str(config_path),
            "--provider",
            "openai",
            "--approval",
            "auto",
        ]
    )
    agent = build_cli_agent(args)
    pending_session_path = agent.session_path

    handled, _, output = handle_repl_command(agent, "/provider deepseek")

    assert handled is True
    assert "provider: deepseek" in output
    assert "protocol: anthropic" in output
    assert "model: deepseek-v4-pro" in output
    assert agent.model_client.provider == "deepseek"
    assert agent.model_client.protocol == "anthropic"
    assert agent.model_client.api_key == "sk-deepseek"
    assert agent.model_client_factory().provider == "deepseek"
    assert agent.is_pending_session is True
    assert not pending_session_path.exists()
    assert not agent.session_event_bus.path.exists()
    assert not (Path(tmp_path) / ".bunnybyte.toml").exists()

    handled, _, output = handle_repl_command(agent, "/usage")
    assert handled is True
    assert "provider profile: deepseek" in output


def test_session_history_resume_and_clear_commands(tmp_path):
    from bunnybyte.cli import handle_repl_command

    first = build_agent(tmp_path, ["<final>First.</final>"])
    assert first.ask("first request") == "First."
    first_id = first.session["id"]

    second = BunnyByte.from_session(
        model_client=ScriptedModelClient(["<final>Second.</final>"]),
        workspace=first.workspace,
        session_store=first.session_store,
        session_id=first_id,
        approval_policy="auto",
    )
    assert second.ask("second request") == "Second."

    handled, _, output = handle_repl_command(second, "/history")
    assert handled is True
    assert first_id in output
    assert "first request" in output
    assert "Second." in output

    handled, _, output = handle_repl_command(second, "/session")
    assert handled is True
    assert "session topic: first request" in output

    handled, _, output = handle_repl_command(second, f"/resume {first_id}")
    assert handled is True
    assert output == f"resumed session {first_id}"
    assert second.session["id"] == first_id

    old_id = second.session["id"]
    handled, _, output = handle_repl_command(second, "/clear")
    assert handled is True
    assert output.startswith("new session ")
    assert second.session["id"] != old_id
    assert second.current_task_state is None
    assert second.current_run_id == ""
    assert second.current_run_dir is None
    assert second.session_store.path(old_id).exists()


def test_session_topic_command_renames_and_resume_matches_topic(tmp_path):
    from bunnybyte.cli import handle_repl_command

    agent = build_agent(tmp_path, ["<final>Done.</final>"])
    assert agent.ask("optimize session discovery") == "Done."

    handled, _, output = handle_repl_command(agent, "/topic History Browser")
    assert handled is True
    assert output == "session topic: History Browser"

    handled, _, output = handle_repl_command(agent, "/history")
    assert handled is True
    assert "History Browser" in output

    handled, _, output = handle_repl_command(agent, "/resume Browser")
    assert handled is True
    assert output == f"resumed session {agent.session['id']}"


def test_resume_rejects_path_traversal_session_id(tmp_path):
    from bunnybyte.cli import handle_repl_command

    agent = build_agent(tmp_path, [])

    handled, _, output = handle_repl_command(agent, "/resume ../outside")

    assert handled is True
    assert output == "error: session not found"


def test_session_store_rejects_path_traversal_ids(tmp_path):
    store = SessionStore(tmp_path / ".bunnybyte" / "sessions")

    with pytest.raises(ValueError, match="invalid session id"):
        store.load("../outside")
