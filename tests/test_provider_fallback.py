import json
from types import SimpleNamespace

import pytest

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte import cli
from bunnybyte.config import _render_config_values, _read_config_file
from bunnybyte.config.fallback import (
    parse_fallback_names,
    resolve_fallback_configs,
    resolve_fallback_names,
)
from bunnybyte.providers import ProviderError
from bunnybyte.providers.fallback import FallbackModelClient, convert_native_tools
from bunnybyte.testing import ScriptedModelClient


def server_error(provider="openai", status=521):
    return ProviderError(
        f"{provider} failed",
        provider=provider,
        code="server_error",
        http_status=status,
        retryable=True,
    )


def scripted(provider, model, outputs, **kwargs):
    client = ScriptedModelClient(outputs, **kwargs)
    client.provider = provider
    client.model = model
    return client


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_fails_over_on_retryable_error_and_records_event():
    primary = scripted("openai", "gpt", [server_error()])
    backup = scripted("deepseek", "ds", ["hello"])
    client = FallbackModelClient(primary, [backup])

    result = client.complete_result("hi", 100)

    assert result.text == "hello"
    info = result.metadata["provider_fallback"]
    assert info["served_by"] == {"provider": "deepseek", "model": "ds"}
    assert info["failed"] == [
        {"provider": "openai", "model": "gpt", "code": "server_error", "http_status": 521}
    ]
    assert client.drain_failover_events() == [info]
    assert client.drain_failover_events() == []
    assert client.last_completion_metadata["provider_fallback"] == info


def test_non_retryable_error_is_not_swallowed():
    auth = ProviderError("bad key", provider="openai", code="auth_error", http_status=401)
    primary = scripted("openai", "gpt", [auth])
    backup = scripted("deepseek", "ds", ["never"])
    client = FallbackModelClient(primary, [backup])

    with pytest.raises(ProviderError) as caught:
        client.complete_result("hi", 100)

    assert caught.value is auth
    assert backup.prompts == []


def test_no_failover_after_partial_stream_output():
    class Midstream(ScriptedModelClient):
        provider = "openai"
        model = "gpt"

        def complete(self, prompt, max_new_tokens, on_delta=None, **kwargs):
            on_delta("partial")
            raise server_error()

    backup = scripted("deepseek", "ds", ["never"])
    client = FallbackModelClient(Midstream([]), [backup])
    seen = []

    with pytest.raises(ProviderError):
        client.complete_result("hi", 100, on_delta=seen.append)

    assert seen == ["partial"]
    assert backup.prompts == []


def test_cooldown_skips_primary_then_recovers():
    clock = Clock()
    primary = scripted("openai", "gpt", [server_error(), "back"])
    backup = scripted("deepseek", "ds", ["one", "two"])
    client = FallbackModelClient(primary, [backup], cooldown_seconds=60, clock=clock)

    assert client.complete("a", 10) == "one"
    assert len(primary.prompts) == 1
    clock.now += 30
    assert client.complete("b", 10) == "two"
    assert len(primary.prompts) == 1  # still cooling down, primary untouched
    assert len(client.drain_failover_events()) == 1  # only the real failure is announced
    clock.now += 60
    assert client.complete("c", 10) == "back"
    assert len(primary.prompts) == 2


def test_cooldown_falls_back_to_primary_when_backup_also_fails():
    clock = Clock()
    primary = scripted("openai", "gpt", [server_error(), "primary again"])
    backup = scripted("deepseek", "ds", ["one", server_error("deepseek", 503)])
    client = FallbackModelClient(primary, [backup], cooldown_seconds=60, clock=clock)

    client.complete("a", 10)
    assert client.complete("b", 10) == "primary again"


def test_all_providers_failing_raises_primary_error_with_all_failures():
    primary_error = server_error("openai", 521)
    primary = scripted("openai", "gpt", [primary_error])
    backup = scripted("deepseek", "ds", [server_error("deepseek", 503)])
    client = FallbackModelClient(primary, [backup])

    with pytest.raises(ProviderError) as caught:
        client.complete_result("hi", 100)

    assert caught.value is primary_error
    payload = caught.value.to_metadata()["provider_error"]
    assert payload["fallback_failures"] == [
        {"provider": "deepseek", "model": "ds", "code": "server_error", "http_status": 503}
    ]


def test_tools_are_converted_for_the_backup_protocol():
    openai_tools = [
        {
            "type": "function",
            "name": "read_file",
            "description": "read",
            "parameters": {"type": "object", "properties": {}},
        }
    ]
    primary = scripted(
        "openai", "gpt", [server_error()], supports_native_tools=True,
        native_tool_protocol="openai",
    )
    backup = scripted(
        "deepseek", "ds", ["ok"], supports_native_tools=True,
        native_tool_protocol="anthropic",
    )
    client = FallbackModelClient(primary, [backup])

    client.complete_result("hi", 100, tools=openai_tools)

    assert primary.tool_batches[0] == openai_tools
    assert backup.tool_batches[0] == [
        {
            "name": "read_file",
            "description": "read",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]
    assert convert_native_tools(backup.tool_batches[0], "openai") == openai_tools


def test_wrapper_delegates_identity_to_primary_and_forwards_model_changes():
    primary = scripted("openai", "gpt", [], supports_native_tools=True)
    client = FallbackModelClient(primary, [scripted("deepseek", "ds", [])])

    assert client.provider == "openai"
    assert client.supports_native_tools is True
    client.model = "gpt-next"
    assert primary.model == "gpt-next"
    assert [item.provider for item in client.fallback_clients] == ["deepseek"]


def test_engine_announces_failover_and_traces_it(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    primary = scripted("openai", "gpt", [server_error()])
    backup = scripted("deepseek", "ds", ["<final>done via backup</final>"])
    agent = BunnyByte(
        model_client=FallbackModelClient(primary, [backup]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        approval_policy="auto",
    )

    events = list(agent.engine.run_turn("hello"))

    notices = [e["content"] for e in events if e["type"] == "runtime_notice"]
    assert len(notices) == 1
    assert "deepseek/ds" in notices[0] and "HTTP 521" in notices[0]
    assert [e for e in events if e["type"] == "final"][0]["content"] == "done via backup"
    run_dir = next((tmp_path / ".bunnybyte" / "runs").iterdir())
    trace = [
        json.loads(line)
        for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [e for e in trace if e["event"] == "provider_fallback"]
    session_events = [
        json.loads(line)
        for line in agent.session_event_bus.path.read_text(encoding="utf-8").splitlines()
    ]
    assert "provider_fallback" in [e["event"] for e in session_events]


# ---- 配置解析 -------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for name in (
        "BUNNYBYTE_PROVIDER", "BUNNYBYTE_API_KEY", "BUNNYBYTE_BASE_URL",
        "BUNNYBYTE_MODEL", "BUNNYBYTE_FALLBACK", "DEEPSEEK_API_KEY",
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("bunnybyte.config.DEFAULT_CONFIG_PATH", tmp_path / "no-global.toml")
    return tmp_path


def write_config(root, text):
    (root / ".bunnybyte.toml").write_text(text, encoding="utf-8")


def test_parse_fallback_names_handles_strings_lists_and_disable():
    assert parse_fallback_names("deepseek, claude  gpt,deepseek") == [
        "deepseek", "anthropic", "openai",
    ]
    assert parse_fallback_names(["deepseek", ""]) == ["deepseek"]
    assert parse_fallback_names("none") == []
    assert parse_fallback_names(None) == []
    assert parse_fallback_names(42) == []


def test_fallback_precedence_cli_env_profile_top(clean_env, monkeypatch):
    write_config(
        clean_env,
        'provider = "openai"\nfallback = ["anthropic"]\n'
        '[providers.openai]\napi_key = "k"\nfallback = ["deepseek", "openai"]\n',
    )
    names = lambda **kw: resolve_fallback_names("openai", start=clean_env, **kw)  # noqa: E731
    assert names() == ["deepseek"]  # profile beats top; self is dropped
    monkeypatch.setenv("BUNNYBYTE_FALLBACK", "anthropic")
    assert names() == ["anthropic"]
    assert names(cli_value="none") == []
    assert resolve_fallback_names("deepseek", start=clean_env) == ["anthropic"]


def test_fallback_configs_skip_incomplete_and_ignore_generic_env(clean_env, monkeypatch):
    write_config(
        clean_env,
        'fallback = ["deepseek", "anthropic"]\n'
        '[providers.deepseek]\napi_key = "ds-key"\n',
    )
    monkeypatch.setenv("BUNNYBYTE_API_KEY", "primary-key")
    monkeypatch.setenv("BUNNYBYTE_MODEL", "primary-model")

    configs = resolve_fallback_configs("openai", start=clean_env)

    assert [c.name for c in configs] == ["deepseek"]  # anthropic has no key
    assert configs[0].api_key == "ds-key"
    assert configs[0].model == "deepseek-v4-pro"


def test_config_roundtrip_preserves_top_level_fallback(clean_env):
    path = clean_env / "cfg.toml"
    path.write_text(
        'provider = "openai"\nfallback = ["deepseek"]\n'
        '[providers.openai]\napi_key = "k"\nfallback = ["anthropic", "deepseek"]\n',
        encoding="utf-8",
    )
    values = _read_config_file(path)
    rendered = _render_config_values(values)
    path.write_text(rendered, encoding="utf-8")
    again = _read_config_file(path)
    assert again["top"]["fallback"] == ["deepseek"]
    assert again["providers"]["openai"]["fallback"] == ["anthropic", "deepseek"]


def test_cli_wraps_primary_only_when_a_usable_fallback_exists(clean_env):
    write_config(
        clean_env,
        'provider = "openai"\nfallback = ["deepseek"]\n'
        '[providers.openai]\napi_key = "k"\n'
        '[providers.deepseek]\napi_key = "ds"\n',
    )
    args = SimpleNamespace(
        cwd=str(clean_env), config=None, provider=None, model=None,
        base_url=None, api_key=None, fallback=None, temperature=0.2, openai_timeout=5,
    )
    client, config = cli._build_model_client_with_config(args)
    assert isinstance(client, FallbackModelClient)
    assert config.name == "openai"
    assert [c.provider for c in client.fallback_clients] == ["deepseek"]

    args.fallback = "none"
    plain, _ = cli._build_model_client_with_config(args)
    assert not isinstance(plain, FallbackModelClient)
    assert plain.provider == "openai"
