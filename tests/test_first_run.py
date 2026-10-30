import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from bunnybyte import cli

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib
from bunnybyte.bench.report import package_version
from bunnybyte.config import LEGACY_ENV_NAMES, PROVIDER_ENV_NAMES
from bunnybyte.core.first_run import missing_key_check, model_error_hint
from bunnybyte.doctor import run_doctor
from bunnybyte.headless import EXIT_USAGE
from bunnybyte.providers import AnthropicCompatibleModelClient, OpenAICompatibleModelClient
from bunnybyte.providers.fallback import FallbackModelClient
from bunnybyte.testing import ScriptedModelClient
from bunnybyte.tui import main as tui_main
from bunnybyte.version import __version__

ROOT = Path(__file__).resolve().parents[1]
KEY_ENV = (
    "BUNNYBYTE_PROVIDER", "BUNNYBYTE_API_KEY", "BUNNYBYTE_BASE_URL", "BUNNYBYTE_MODEL", "BUNNYBYTE_PROTOCOL",
    "OPENAI_API_KEY", "OPENAI_API_BASE", "OPENAI_BASE_URL", "BUNNYBYTE_OPENAI_API_KEY",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "BUNNYBYTE_ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY", "BUNNYBYTE_DEEPSEEK_API_KEY",
)


@pytest.fixture
def no_keys(monkeypatch, tmp_path):
    names = set(KEY_ENV)
    for table in (PROVIDER_ENV_NAMES, LEGACY_ENV_NAMES):
        for fields in table.values():
            for env_names in fields.values():
                names.update(env_names)
    for name in names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("bunnybyte.config.DEFAULT_CONFIG_PATH", tmp_path / "missing-global.toml")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def _client(cls, base_url, api_key="", provider="openai"):
    client = cls(model="m", base_url=base_url, api_key=api_key, temperature=0.2, timeout=5)
    client.provider = provider
    return client


def _no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("no model call may happen without a key")

    monkeypatch.setattr(OpenAICompatibleModelClient, "complete_result", refuse)
    monkeypatch.setattr(AnthropicCompatibleModelClient, "complete_result", refuse)


def test_version_flag_prints_single_source_version(capsys):
    with pytest.raises(SystemExit) as done:
        cli.main(["--version"])
    assert done.value.code == 0
    assert capsys.readouterr().out.strip() == f"bunny {__version__}"


def test_pyproject_reads_version_from_the_module_without_a_literal():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "version" in project["project"]["dynamic"] and "version" not in project["project"]
    assert project["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "bunnybyte.version.__version__"}
    # setuptools 读 attr 时不导入包的前提：version.py 只有一行字面量赋值
    source = (ROOT / "bunnybyte" / "version.py").read_text(encoding="utf-8")
    assert "import" not in source and f'__version__ = "{__version__}"' in source
    assert package_version() == __version__
    assert project["project"]["readme"] == "README.EN.md"


def test_doctor_first_row_is_the_version(no_keys):
    checks = run_doctor(no_keys, offline=True, which=lambda name: None)
    assert (checks[0].name, checks[0].status, checks[0].detail) == ("版本", "ok", f"bunny {__version__}")


@pytest.mark.parametrize(
    "cls, base_url, provider, level",
    [
        (OpenAICompatibleModelClient, "https://api.openai.com/v1", "openai", "error"),
        (AnthropicCompatibleModelClient, "https://api.anthropic.com", "anthropic", "error"),
        (AnthropicCompatibleModelClient, "https://api.deepseek.com/anthropic", "deepseek", "error"),
        (AnthropicCompatibleModelClient, "https://api.deepseek.com./anthropic", "deepseek", "error"),
        (OpenAICompatibleModelClient, "https://gateway.corp.example/v1", "oneapi", "warn"),
        (OpenAICompatibleModelClient, "http://localhost:11434/v1", "ollama", ""),
        (OpenAICompatibleModelClient, "http://127.0.0.1:1234/v1", "lmstudio", ""),
    ],
)
def test_missing_key_levels_by_endpoint(cls, base_url, provider, level):
    got_level, message = missing_key_check(_client(cls, base_url, provider=provider))
    assert got_level == level
    if level == "error":
        assert "bunny setup" in message and provider in message
    if level == "":
        assert message == ""


def test_missing_key_check_ignores_configured_keys_and_scripted_clients():
    assert missing_key_check(_client(OpenAICompatibleModelClient, "https://api.openai.com/v1", api_key="k")) == ("", "")
    assert missing_key_check(ScriptedModelClient([])) == ("", "")


def test_fallback_wrapper_is_judged_by_its_primary():
    primary = _client(AnthropicCompatibleModelClient, "https://api.deepseek.com/anthropic", provider="deepseek")
    backup = _client(OpenAICompatibleModelClient, "https://api.openai.com/v1", api_key="k")
    assert missing_key_check(FallbackModelClient(primary, [backup]))[0] == "error"
    assert missing_key_check(FallbackModelClient(backup, [primary]))[0] == ""


def test_print_mode_without_key_stops_before_any_request(no_keys, monkeypatch, capsys):
    _no_network(monkeypatch)
    code = cli.main(["-p", "hi", "--cwd", str(no_keys)])
    err = capsys.readouterr().err
    assert code == EXIT_USAGE
    assert "还没有配置 openai 的 API key" in err and "bunny setup" in err and "OPENAI_API_KEY" in err


def test_print_mode_without_key_gives_json_result(no_keys, monkeypatch, capsys):
    _no_network(monkeypatch)
    code = cli.main(["-p", "hi", "--provider", "deepseek", "--output-format", "json", "--cwd", str(no_keys)])
    result = json.loads(capsys.readouterr().out)
    assert code == EXIT_USAGE and result["exit_code"] == EXIT_USAGE and result["ok"] is False
    assert "deepseek" in result["error"] and "DEEPSEEK_API_KEY" in result["error"]


def test_interactive_entry_points_without_key_exit_before_the_ui(no_keys, monkeypatch, capsys):
    _no_network(monkeypatch)
    monkeypatch.setattr("bunnybyte.tui.app.BunnyByteTuiApp.run", lambda self: pytest.fail("TUI must not start"))
    assert cli.main(["--repl", "--cwd", str(no_keys)]) == 2
    assert cli.main(["hello", "--cwd", str(no_keys)]) == 2
    assert tui_main.main(["--cwd", str(no_keys)]) == 2
    assert capsys.readouterr().err.count("bunny setup") >= 3


def test_gateway_without_key_warns_and_still_runs(no_keys, monkeypatch, capsys):
    real_build = cli.build_agent

    def build(args):
        agent = real_build(args)
        gateway = _client(OpenAICompatibleModelClient, "https://gateway.corp.example/v1", provider="oneapi")
        gateway.complete_result = ScriptedModelClient(["<final>好</final>"]).complete_result
        agent.model_client = gateway
        return agent

    monkeypatch.setattr(cli, "build_agent", build)
    assert cli.main(["-p", "hi", "--cwd", str(no_keys)]) == 0
    captured = capsys.readouterr()
    assert "gateway.corp.example" in captured.err and "好" in captured.out


def test_gateway_warning_names_raw_url_without_scheme():
    level, message = missing_key_check(_client(OpenAICompatibleModelClient, "gw.example/v1", provider="oneapi"))
    assert level == "warn" and "gw.example/v1" in message


def test_quiet_print_mode_suppresses_the_gateway_warning(no_keys, monkeypatch, capsys):
    real_build = cli.build_agent

    def build(args):
        agent = real_build(args)
        gateway = _client(OpenAICompatibleModelClient, "https://gateway.corp.example/v1", provider="oneapi")
        gateway.complete_result = ScriptedModelClient(["<final>好</final>"]).complete_result
        agent.model_client = gateway
        return agent

    monkeypatch.setattr(cli, "build_agent", build)
    assert cli.main(["-p", "-q", "hi", "--cwd", str(no_keys)]) == 0
    assert "gateway.corp.example" not in capsys.readouterr().err


def test_configured_key_passes_through_real_build_agent(no_keys, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-value")
    agent = cli.build_agent(cli.build_arg_parser().parse_args(["--cwd", str(no_keys)]))
    assert cli.key_check_passes(agent) is True


def test_auth_error_model_message_points_to_doctor_and_setup(tmp_path):
    from bunnybyte.core.runtime import BunnyByte
    from bunnybyte.core.session_store import SessionStore
    from bunnybyte.core.workspace import WorkspaceContext
    from bunnybyte.providers import ProviderError

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    agent = BunnyByte(
        model_client=ScriptedModelClient([ProviderError("bad key", code="auth_error", http_status=401)]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        approval_policy="auto",
    )
    events = list(agent.engine.run_turn("hi"))
    stop = [event for event in events if event["type"] == "stop"][-1]["content"]
    assert stop.startswith("模型错误：auth_error") and "bunny doctor" in stop and "bunny setup --force" in stop
    assert model_error_hint("rate_limited") == ""
