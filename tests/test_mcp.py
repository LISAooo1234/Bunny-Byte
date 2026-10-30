import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.cli import handle_repl_command
from bunnybyte.core import mcp_config as mcp_config_module
from bunnybyte.core.mcp_client import McpError, McpServer
from bunnybyte.core.mcp_config import load_mcp_config
from bunnybyte.core.mcp_tools import McpManager, compact_schema, register_mcp_tools, render_result
from bunnybyte.core.runtime import DEFAULT_SHELL_ENV_ALLOWLIST
from bunnybyte.testing import ScriptedModelClient
from bunnybyte.tools.schema import native_tool_specs

FAKE = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")


def tool(name, **args):
    return "<tool>" + json.dumps({"name": name, "args": args}) + "</tool>"


def server_raw(name="fake", *flags, **extra):
    entry = {"command": sys.executable, "args": [FAKE, *flags]}
    entry.update(extra)
    return {"servers": {name: entry}}


def build_agent(tmp_path, outputs, approval_policy="auto", **kwargs):
    return BunnyByte(
        model_client=ScriptedModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        approval_policy=approval_policy,
        **kwargs,
    )


def attach(agent, raw, project_raw=None, project_config=None, trust_path=None):
    config = load_mcp_config(raw, project_raw or {}, project_config, trust_path=trust_path)
    manager = McpManager(config, agent.root, env_allowlist=agent.shell_env_allowlist)
    agent.mcp = manager
    manager.start()
    register_mcp_tools(agent, manager)
    return manager


@pytest.fixture
def started(tmp_path):
    managers = []

    def factory(agent, raw, **kwargs):
        manager = attach(agent, raw, **kwargs)
        managers.append(manager)
        return manager

    yield factory
    for manager in managers:
        manager.close()


def test_client_lists_paginated_tools_and_calls_one(tmp_path):
    server = McpServer("fake", sys.executable, [FAKE, "--paginate"], env={"PATH": "/usr/bin:/bin"}, cwd=str(tmp_path))
    try:
        server.start(timeout=10)
        names = [item["name"] for item in server.list_tools()]
        assert names == ["echo", "write_note", "fail", "env", "slow", "union"]
        result = server.call_tool("echo", {"text": "ab", "times": 2})
        assert render_result(result) == "abab"
        assert server.server_info["name"] == "fake"
    finally:
        server.close()
    assert not server.alive


def test_client_skips_stdout_noise_and_answers_server_requests(tmp_path):
    server = McpServer("noisy", sys.executable, [FAKE, "--noisy"], env={}, cwd=str(tmp_path))
    try:
        server.start(timeout=10)
        assert render_result(server.call_tool("echo", {"text": "hi"})) == "hi"
    finally:
        server.close()


def test_call_times_out_and_can_be_aborted(tmp_path):
    server = McpServer("slow", sys.executable, [FAKE, "--slow-call", "30"], env={}, cwd=str(tmp_path))
    try:
        server.start(timeout=10)
        started = time.monotonic()
        with pytest.raises(McpError, match="超时"):
            server.call_tool("slow", {}, timeout=0.5)
        with pytest.raises(McpError, match="中止"):
            server.call_tool("slow", {}, timeout=30, should_abort=lambda: time.monotonic() - started > 1)
        assert time.monotonic() - started < 10
    finally:
        server.close()


def test_server_crash_mid_call_raises_instead_of_hanging(tmp_path):
    server = McpServer("crash", sys.executable, [FAKE, "--crash-on-call"], env={}, cwd=str(tmp_path))
    server.start(timeout=10)
    started = time.monotonic()
    with pytest.raises(McpError, match="已断开"):
        server.call_tool("echo", {"text": "x"}, timeout=30)
    assert time.monotonic() - started < 10
    server.close()


def test_failed_server_does_not_break_startup_and_others_still_register(tmp_path, started):
    agent = build_agent(tmp_path, [])
    raw = {"servers": {
        "bad": {"command": sys.executable, "args": [FAKE, "--exit-at-start"]},
        "missing": {"command": "/nonexistent/mcp-server"},
        "good": {"command": sys.executable, "args": [FAKE]},
    }}
    manager = started(agent, raw)

    failures = {state.spec.name: state.error for state in manager.failures()}
    assert set(failures) == {"bad", "missing"}
    assert "无法启动" in failures["missing"]
    assert "mcp__good__echo" in agent.tools
    assert not any(name.startswith(("mcp__bad", "mcp__missing")) for name in agent.tools)
    assert "boom at start" in (tmp_path / ".bunnybyte" / "mcp" / "bad.log").read_text()


def test_tools_are_registered_risky_by_default_with_compact_and_native_schemas(tmp_path, started):
    agent = build_agent(tmp_path, [])
    started(agent, server_raw(read_only_tools=["echo"]))

    echo, write = agent.tools["mcp__fake__echo"], agent.tools["mcp__fake__write_note"]
    assert echo.risky is False and echo.parallel_safe is True
    assert write.risky is True
    assert echo.schema == {"text": "str", "times": "int?"}
    assert "- mcp__fake__echo(text: str, times: int?) [safe] [MCP] Echo the text back." in agent.prefix
    spec = next(item for item in native_tool_specs(agent.available_tools(), "anthropic") if item["name"] == "mcp__fake__echo")
    assert spec["input_schema"]["required"] == ["text"]
    assert "$schema" not in spec["input_schema"]
    assert agent.prefix_state.tool_signature == agent.tool_signature()


def test_risky_mcp_tools_stay_out_of_worker_and_dream_profiles(tmp_path, started):
    agent = build_agent(tmp_path, [])
    started(agent, server_raw(read_only_tools=["echo"]))

    worker = agent.tool_profiles["worker"]
    assert not worker.allows("mcp__fake__write_note")
    assert worker.allows("mcp__fake__echo")
    assert not agent.tool_profiles["dream"].allows("mcp__fake__echo")
    assert agent.tool_profiles["readonly"].allows("mcp__fake__echo")
    assert not agent.tool_profiles["readonly"].allows("mcp__fake__write_note")


def test_full_turn_calls_mcp_tool_and_coerces_text_protocol_args(tmp_path, started):
    agent = build_agent(
        tmp_path,
        ['<tool name="mcp__fake__echo" text="ha" times="3"></tool>', "<final>Echoed.</final>"],
    )
    started(agent, server_raw())

    assert agent.ask("echo please") == "Echoed."
    result = [item for item in agent.session["history"] if item.get("name") == "mcp__fake__echo"][-1]
    assert result["content"] == "hahaha"


def test_missing_required_argument_is_rejected_before_the_server_is_called(tmp_path, started):
    agent = build_agent(tmp_path, [tool("mcp__fake__echo", times=2), "<final>gave up</final>"])
    started(agent, server_raw())

    agent.ask("echo")
    result = [item for item in agent.session["history"] if item.get("name") == "mcp__fake__echo"][-1]
    assert "missing required argument(s): text" in result["content"]


def test_is_error_result_is_a_failed_tool_call(tmp_path, started):
    agent = build_agent(tmp_path, [tool("mcp__fake__fail"), "<final>reported</final>"])
    started(agent, server_raw())

    agent.ask("try it")
    result = [item for item in agent.session["history"] if item.get("name") == "mcp__fake__fail"][-1]
    assert result["content"].startswith("error: tool mcp__fake__fail failed: disk is on fire")


def test_risky_mcp_tool_obeys_approval_policy(tmp_path, started):
    agent = build_agent(
        tmp_path,
        [tool("mcp__fake__write_note", path="note.txt", body="hi"), "<final>denied</final>"],
        approval_policy="never",
    )
    started(agent, server_raw())

    agent.ask("write a note")
    assert not (tmp_path / "note.txt").exists()
    result = [item for item in agent.session["history"] if item.get("name") == "mcp__fake__write_note"][-1]
    assert "approval denied" in result["content"]


@pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0, reason="git is required")
def test_files_written_by_an_mcp_server_are_tracked_and_undone(tmp_path, started):
    (tmp_path / "keep.txt").write_text("keep\n")
    agent = build_agent(tmp_path, [tool("mcp__fake__write_note", path="note.txt", body="from mcp\n"), "<final>noted</final>"])
    started(agent, server_raw())

    agent.ask("write a note")
    assert (tmp_path / "note.txt").read_text() == "from mcp\n"
    assert "note.txt" in agent.current_task_state.changed_paths

    handled, _, output = handle_repl_command(agent, "/undo")
    assert handled and "已撤销" in output
    assert not (tmp_path / "note.txt").exists()
    assert (tmp_path / "keep.txt").read_text() == "keep\n"


def test_server_env_is_an_allowlist_plus_configured_values(tmp_path, monkeypatch, started):
    monkeypatch.setenv("SECRET_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("MY_TOKEN_SOURCE", "tok-123")
    agent = build_agent(tmp_path, [tool("mcp__fake__env"), "<final>ok</final>"])
    started(agent, server_raw(env={"FAKE_TOKEN": "${MY_TOKEN_SOURCE}"}))

    agent.ask("env")
    result = [item for item in agent.session["history"] if item.get("name") == "mcp__fake__env"][-1]
    seen = json.loads(result["content"])
    assert seen["SECRET_API_KEY"] is None
    assert seen["FAKE_TOKEN"] == "tok-123"
    assert "HOME" in DEFAULT_SHELL_ENV_ALLOWLIST and seen["HOME"] == __import__("os").environ.get("HOME")


def test_project_servers_wait_for_trust_and_trust_breaks_on_change(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_config_module, "STARTUP_TRUST_ENV", False)
    trust_path = tmp_path / "trusted_mcp.json"
    project = tmp_path / ".bunnybyte.toml"
    raw = server_raw("proj")

    pending = load_mcp_config({}, raw, project, trust_path=trust_path)
    assert pending.servers == () and [spec.name for spec in pending.pending] == ["proj"]

    mcp_config_module.trust_project_servers(pending, trust_path)
    trusted = load_mcp_config({}, raw, project, trust_path=trust_path)
    assert [spec.name for spec in trusted.servers] == ["proj"] and trusted.pending == ()

    changed = server_raw("proj", read_only_tools=["*"])
    assert load_mcp_config({}, changed, project, trust_path=trust_path).pending, "read_only_tools must be in the fingerprint"


def test_mcp_trust_command_starts_pending_servers(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_config_module, "STARTUP_TRUST_ENV", False)
    monkeypatch.setenv("BUNNYBYTE_HOME", str(tmp_path / "home"))
    agent = build_agent(tmp_path, [])
    manager = attach(agent, {}, project_raw=server_raw("proj"), project_config=tmp_path / ".bunnybyte.toml")
    try:
        assert not any(name.startswith("mcp__") for name in agent.tools)
        listing = handle_repl_command(agent, "/mcp")[2]
        assert "未信任，不会启动" in listing

        output = handle_repl_command(agent, "/mcp trust")[2]
        assert "已信任" in output and "mcp__proj__echo" in output
        assert "mcp__proj__echo" in agent.tools
        assert "mcp__proj__echo" in agent.prefix
        assert (tmp_path / "home" / "trusted_mcp.json").exists()
    finally:
        manager.close()


def test_config_validation_rejects_bad_entries():
    config = load_mcp_config(
        {"servers": {
            "a__b": {"command": "x"},
            "nocmd": {"args": []},
            "badtimeout": {"command": "x", "timeout": "soon"},
            "off": {"command": "x", "enabled": False},
            "ok": {"command": "x", "extra": 1},
        }},
        {},
    )
    assert [spec.name for spec in config.servers] == ["ok"]
    joined = " ".join(config.problems)
    assert "a__b" in joined and "nocmd" in joined and "badtimeout" in joined and "extra" in joined


def test_project_server_with_global_name_is_ignored():
    config = load_mcp_config(server_raw("same"), server_raw("same"), "/repo/.bunnybyte.toml")
    assert [spec.source for spec in config.servers] == ["global"] and config.pending == ()
    assert any("同名" in problem for problem in config.problems)


def test_names_are_sanitized_capped_and_unique(tmp_path):
    class FakeClient:
        alive = True

        def close(self):
            pass

    agent = build_agent(tmp_path, [])
    manager = McpManager(load_mcp_config({}, {}), tmp_path)
    from bunnybyte.core.mcp_tools import ServerState
    from bunnybyte.core.mcp_config import McpServerSpec

    state = ServerState(spec=McpServerSpec(name="srv", command="x"), status="running", client=FakeClient())
    state.tools = [{"name": "x" * 100}, {"name": "write_file"}, {"name": "get.page"}, {"name": "get page"}]
    manager.states["srv"] = state
    added = register_mcp_tools(agent, manager)

    assert len(set(added)) == 4
    assert all(len(name) <= 64 and name.startswith("mcp__srv__") for name in added)
    # 按服务器里的工具名排序注册（"get page" < "get.page"），前缀顺序与服务器返回顺序无关
    assert added[:2] == ["mcp__srv__get_page", "mcp__srv__get_page_2"]
    assert agent.tools["write_file"].description != agent.tools["mcp__srv__write_file"].description
    order = [line.split("(")[0][2:] for line in agent.prefix.splitlines() if line.startswith("- mcp__")]
    assert order == added
    manager.close()


def test_compact_schema_handles_nullable_and_arrays():
    schema = {
        "properties": {"q": {"type": ["string", "null"]}, "ids": {"type": "array", "items": {"type": "integer"}}, "o": {}},
        "required": ["q"],
    }
    assert compact_schema(schema) == {"q": "str", "ids": "list[int]?", "o": "str?"}


def test_render_result_summarizes_non_text_content():
    result = {"content": [
        {"type": "text", "text": "hello"},
        {"type": "image", "mimeType": "image/png", "data": "aaaa"},
        {"type": "resource", "resource": {"uri": "file:///a.txt", "text": "body"}},
    ]}
    rendered = render_result(result)
    assert "hello" in rendered and "image/png" in rendered and "file:///a.txt" in rendered and "body" in rendered
    assert render_result({"content": [], "structuredContent": {"a": 1}}) == '{\n  "a": 1\n}'
    assert render_result({}) == "(no output)"


def test_tui_card_title_names_the_server_and_tool():
    from bunnybyte.tui.tool_cards import tool_title

    assert tool_title("mcp__github__create_issue") == "MCP github/create_issue"
    assert tool_title("read_file") == "读取"


def test_workers_share_connections_but_only_get_approval_free_mcp_tools(tmp_path, started):
    from bunnybyte.core.worker_runtime import build_child_runtime

    agent = build_agent(tmp_path, [])
    manager = started(agent, server_raw(read_only_tools=["echo"]))
    worker = build_child_runtime(agent, "worker", ["notes/"])
    explore = build_child_runtime(agent, "Explore", [])

    for child in (worker, explore):
        visible = set(child.available_tools())
        assert "mcp__fake__echo" in visible and "mcp__fake__write_note" not in visible
        assert child.mcp is manager
    assert "mcp__fake__echo" in worker.prefix and "mcp__fake__write_note" not in worker.prefix
    worker.abort_requested = True
    with pytest.raises(McpError, match="中止"):
        worker.tools["mcp__fake__slow"].execute({})  # runner 绑定的是 worker 自己的中止标记


def test_native_schema_drops_constructs_providers_reject(tmp_path, started):
    agent = build_agent(tmp_path, [])
    started(agent, server_raw())

    schema = agent.tools["mcp__fake__union"].json_schema
    assert set(schema) <= {"type", "properties", "required", "description"}
    assert schema["required"] == ["id"]  # 不存在的属性不进 required
    assert schema["properties"]["id"] == {}
    assert schema["properties"]["tags"] == {"type": "array", "items": {}}
    assert schema["properties"]["value"] == {}
    assert "$ref" not in json.dumps(schema) and "$defs" not in json.dumps(schema)
    for protocol in ("anthropic", "openai"):
        specs = native_tool_specs(agent.available_tools(), protocol)
        assert {spec["name"] for spec in specs} >= {"mcp__fake__union", "read_file"}


def test_pending_listing_shows_everything_trust_covers(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_config_module, "STARTUP_TRUST_ENV", False)
    agent = build_agent(tmp_path, [])
    raw = server_raw("proj", read_only_tools=["*"], env={"X": "${DEEPSEEK_API_KEY}"}, cwd="sub")
    manager = attach(agent, {}, project_raw=raw, project_config=tmp_path / ".bunnybyte.toml", trust_path=tmp_path / "t.json")
    try:
        listing = handle_repl_command(agent, "/mcp")[2]
        assert "X = ${DEEPSEEK_API_KEY}" in listing
        assert "全部工具免审批" in listing and "cwd：`sub`" in listing and FAKE in listing
    finally:
        manager.close()


def test_close_also_kills_children_the_server_spawned(tmp_path):
    import os

    pid_file = tmp_path / "child.pid"
    server = McpServer("kids", sys.executable, [FAKE, "--spawn-child", str(pid_file)], env={}, cwd=str(tmp_path))
    server.start(timeout=10)
    child_pid = int(pid_file.read_text())
    server.close()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(child_pid, 9)
        raise AssertionError("child of the MCP server survived close()")


def test_cli_build_agent_starts_configured_servers(tmp_path, monkeypatch):
    from unittest.mock import patch

    import bunnybyte.cli as bunnybyte_cli

    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            pass

    config = tmp_path / "bb.toml"
    config.write_text(
        'provider = "openai"\n[providers.openai]\napi_key = "x"\n'
        f'[mcp.servers.fake]\ncommand = {json.dumps(sys.executable)}\nargs = [{json.dumps(FAKE)}]\n'
        '[mcp.servers.broken]\ncommand = "/nonexistent/mcp"\n',
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    with patch("bunnybyte.cli.OpenAICompatibleModelClient", DummyModelClient):
        args = bunnybyte_cli.build_arg_parser().parse_args(["--cwd", str(repo), "--config", str(config), "--approval", "auto"])
        agent = bunnybyte_cli.build_agent(args)
    try:
        assert "mcp__fake__echo" in agent.tools and "mcp__fake__echo" in agent.prefix
        assert [state.spec.name for state in agent.mcp.failures()] == ["broken"]
    finally:
        agent.mcp.close()


def test_resolve_project_mcp_refuses_symlinked_project_config(tmp_path, monkeypatch):
    import bunnybyte.config as config_module

    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", tmp_path / "none.toml")
    real = tmp_path / "real.toml"
    real.write_text(f'[mcp.servers.p]\ncommand = {json.dumps(sys.executable)}\n', encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".bunnybyte.toml").symlink_to(real)

    config = config_module.resolve_project_mcp(start=repo)
    assert config.servers == () and config.pending == ()
    assert any("符号链接" in problem for problem in config.problems)
