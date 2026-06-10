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
    assert "## Usage" in output
    assert "| Model | `gpt-test` |" in output
    assert "| Base URL | `https://example.com/v1` |" in output
    assert "| Last input tokens | `10` |" in output
    assert "| Last output tokens | `5` |" in output
    assert "| Last cached tokens | `3` |" in output


def test_model_command_updates_current_runtime_only(tmp_path):
    from bunnybyte.cli import handle_repl_command

    agent = build_agent(tmp_path, [])
    agent.model_client.model = "old-model"

    handled, _, output = handle_repl_command(agent, "/model new-model")

    assert handled is True
    assert "| Model | `new-model` |" in output
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
    assert "## Provider" in output
    assert "| Provider | `deepseek` |" in output
    assert "| Protocol | `anthropic` |" in output
    assert "| Model | `deepseek-v4-pro` |" in output
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
    assert "| Provider profile | `deepseek` |" in output


def test_provider_list_includes_configured_profiles(tmp_path):
    from bunnybyte.cli import handle_repl_command
    from bunnybyte.config import ProviderConfig

    agent = build_agent(tmp_path, [])
    agent.model_client.provider = "deepseek"
    agent.provider_profiles_factory = lambda: [
        ProviderConfig(
            name="deepseek",
            protocol="anthropic",
            api_key="",
            base_url="https://api.deepseek.com/anthropic",
            model="deepseek-v4-flash",
        ),
        ProviderConfig(
            name="deepseek-pro",
            protocol="anthropic",
            api_key="",
            base_url="https://api.deepseek.com/anthropic",
            model="deepseek-v4-pro",
        ),
    ]

    handled, _, output = handle_repl_command(agent, "/provider list")

    assert handled is True
    assert (
        "| yes | `deepseek` | `anthropic` | `deepseek-v4-flash` | "
        "`https://api.deepseek.com/anthropic` | `/provider deepseek` |"
    ) in output
    assert (
        "|  | `deepseek-pro` | `anthropic` | `deepseek-v4-pro` | "
        "`https://api.deepseek.com/anthropic` | `/provider deepseek-pro` |"
    ) in output


def test_provider_command_reports_sanitized_full_base_url(tmp_path):
    from bunnybyte.cli import handle_repl_command

    agent = build_agent(tmp_path, [])
    agent.model_client.provider = "deepseek"
    agent.model_client.protocol = "anthropic"
    agent.model_client.model = "deepseek-v4-flash"
    agent.model_client.base_url = (
        "https://user:secret@api.deepseek.com/anthropic/v2?api_key=sk-secret#frag"
    )

    handled, _, output = handle_repl_command(agent, "/provider")

    assert handled is True
    assert "| Base URL | `https://api.deepseek.com/anthropic/v2` |" in output
    assert "secret" not in output
    assert "api_key" not in output


def test_list_provider_profiles_reads_custom_config(tmp_path):
    from bunnybyte.config import list_provider_profiles

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[providers.deepseek-pro]",
                'protocol = "anthropic"',
                'api_key = "sk-project-deepseek"',
                'base_url = "https://api.deepseek.com/anthropic"',
                'model = "deepseek-v4-pro"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    profiles = {
        profile.name: profile
        for profile in list_provider_profiles(start=tmp_path, config_path=str(config_path))
    }

    assert profiles["deepseek-pro"].protocol == "anthropic"
    assert profiles["deepseek-pro"].model == "deepseek-v4-pro"
    assert profiles["deepseek-pro"].api_key == ""


def test_setup_command_writes_global_provider_config(tmp_path, capsys):
    from bunnybyte.cli import main

    config_path = tmp_path / "config.toml"

    assert (
        main(
            [
                "setup",
                "--provider",
                "deepseek",
                "--api-key",
                "sk-test",
                "--config-path",
                str(config_path),
                "--force",
            ]
        )
        == 0
    )

    text = config_path.read_text(encoding="utf-8")
    assert 'provider = "deepseek"' in text
    assert 'api_key = "sk-test"' in text
    assert 'base_url = "https://api.deepseek.com/anthropic"' in text
    assert 'model = "deepseek-v4-pro"' in text

    capsys.readouterr()
    assert main(["config", "show", "--config-path", str(config_path)]) == 0
    output = capsys.readouterr().out
    assert "默认 provider：deepseek" in output
    assert "API key：已配置" in output
    assert "sk-test" not in output


def test_setup_command_does_not_overwrite_existing_config_without_force(
    tmp_path, capsys
):
    from bunnybyte.cli import main

    config_path = tmp_path / "config.toml"
    assert (
        main(
            [
                "setup",
                "--provider",
                "deepseek",
                "--api-key",
                "sk-deepseek",
                "--config-path",
                str(config_path),
                "--force",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "setup",
                "--provider",
                "openai",
                "--api-key",
                "sk-openai",
                "--config-path",
                str(config_path),
            ]
        )
        == 2
    )

    text = config_path.read_text(encoding="utf-8")
    assert 'provider = "deepseek"' in text
    assert "sk-openai" not in text


def test_provider_command_uses_profile_values_when_switching(tmp_path, monkeypatch):
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
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_MODEL",
        "DEEPSEEK_API_BASE",
        "DEEPSEEK_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    (tmp_path / ".bunnybyte.toml").write_text(
        "\n".join(
            [
                'provider = "openai"',
                "",
                "[providers.deepseek]",
                'protocol = "anthropic"',
                'api_key = "sk-project-deepseek"',
                'base_url = "https://project.deepseek.example/anthropic"',
                'model = "project-deepseek-model"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-deepseek")
    monkeypatch.setattr("bunnybyte.cli.OpenAICompatibleModelClient", DummyOpenAIClient)
    monkeypatch.setattr(
        "bunnybyte.cli.AnthropicCompatibleModelClient", DummyAnthropicClient
    )
    args = build_arg_parser().parse_args(
        [
            "--cwd",
            str(tmp_path),
            "--provider",
            "openai",
            "--api-key",
            "sk-cli-override",
            "--base-url",
            "https://cli.example.test/anthropic",
            "--model",
            "cli-model-override",
            "--approval",
            "auto",
        ]
    )
    agent = build_cli_agent(args)

    handled, _, output = handle_repl_command(agent, "/provider deepseek")

    assert handled is True
    assert "| Provider | `deepseek` |" in output
    assert agent.model_client.provider == "deepseek"
    assert agent.model_client.protocol == "anthropic"
    assert agent.model_client.api_key == "sk-env-deepseek"
    assert agent.model_client.base_url == "https://project.deepseek.example/anthropic"
    assert agent.model_client.model == "project-deepseek-model"


def test_provider_command_rereads_config_without_cli_overrides_when_switching(
    tmp_path, monkeypatch
):
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

    for name in (
        "BUNNYBYTE_PROVIDER",
        "BUNNYBYTE_API_KEY",
        "BUNNYBYTE_MODEL",
        "BUNNYBYTE_BASE_URL",
        "BUNNYBYTE_PROTOCOL",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "OPENAI_API_BASE",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    config_path = tmp_path / ".bunnybyte.toml"
    config_path.write_text(
        "\n".join(
            [
                'provider = "openai"',
                "",
                "[providers.openai]",
                'protocol = "openai"',
                'api_key = "sk-old"',
                'base_url = "https://old.example.test"',
                'model = "old-model"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("bunnybyte.cli.OpenAICompatibleModelClient", DummyOpenAIClient)
    args = build_arg_parser().parse_args(
        [
            "--cwd",
            str(tmp_path),
            "--provider",
            "openai",
            "--api-key",
            "sk-cli-override",
            "--base-url",
            "https://cli.example.test/v1",
            "--model",
            "cli-model-override",
            "--approval",
            "auto",
        ]
    )
    agent = build_cli_agent(args)
    assert agent.model_client.base_url == "https://cli.example.test/v1"

    config_path.write_text(
        "\n".join(
            [
                'provider = "openai"',
                "",
                "[providers.openai]",
                'protocol = "openai"',
                'api_key = "sk-new"',
                'base_url = "https://new.example.test"',
                'model = "new-model"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    handled, _, output = handle_repl_command(agent, "/provider openai")

    assert handled is True
    assert "https://new.example.test" in output
    assert agent.model_client.base_url == "https://new.example.test"
    assert agent.model_client.api_key == "sk-new"
    assert agent.model_client.model == "new-model"

def test_model_client_factory_rereads_provider_config_without_stale_client_values(
    tmp_path, monkeypatch
):
    from bunnybyte.cli import (
        build_agent as build_cli_agent,
        build_arg_parser,
        handle_repl_command,
    )

    class DummyOpenAIClient:
        provider = "openai"
        protocol = "openai"
        supports_prompt_cache = False

        def __init__(self, model="", base_url="", api_key="", temperature=None, timeout=0):
            self.model = model
            self.base_url = base_url
            self.api_key = api_key
            self.temperature = temperature
            self.timeout = timeout
            self.last_completion_metadata = {}

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                'provider = "openai"',
                "",
                "[providers.openai]",
                'protocol = "openai"',
                'api_key = "sk-old"',
                'base_url = "https://old.example.test"',
                'model = "old-model"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("bunnybyte.cli.OpenAICompatibleModelClient", DummyOpenAIClient)
    args = build_arg_parser().parse_args(
        ["--cwd", str(tmp_path), "--config", str(config_path), "--approval", "auto"]
    )
    agent = build_cli_agent(args)

    assert agent.model_client.base_url == "https://old.example.test"
    config_path.write_text(
        "\n".join(
            [
                'provider = "openai"',
                "",
                "[providers.openai]",
                'protocol = "openai"',
                'api_key = "sk-new"',
                'base_url = "https://new.example.test"',
                'model = "new-model"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    refreshed = agent.model_client_factory()

    assert refreshed.base_url == "https://new.example.test"
    assert refreshed.api_key == "sk-new"
    assert refreshed.model == "new-model"

    handled, _, _ = handle_repl_command(agent, "/model manual-model")
    assert handled is True
    assert agent.model_client_factory().model == "manual-model"


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
    assert "| Session topic | first request |" in output

    handled, _, output = handle_repl_command(second, f"/resume {first_id}")
    assert handled is True
    assert "## Session Resumed" in output
    assert first_id in output
    assert second.session["id"] == first_id

    old_id = second.session["id"]
    handled, _, output = handle_repl_command(second, "/clear")
    assert handled is True
    assert "## New Session" in output
    assert "| Session id |" in output
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
    assert "| Session topic | History Browser |" in output

    handled, _, output = handle_repl_command(agent, "/history")
    assert handled is True
    assert "History Browser" in output

    handled, _, output = handle_repl_command(agent, "/resume Browser")
    assert handled is True
    assert agent.session["id"] in output


def test_resume_rejects_path_traversal_session_id(tmp_path):
    from bunnybyte.cli import handle_repl_command

    agent = build_agent(tmp_path, [])

    handled, _, output = handle_repl_command(agent, "/resume ../outside")

    assert handled is True
    assert "## Error" in output
    assert "session not found" in output


def test_session_store_rejects_path_traversal_ids(tmp_path):
    store = SessionStore(tmp_path / ".bunnybyte" / "sessions")

    with pytest.raises(ValueError, match="invalid session id"):
        store.load("../outside")
