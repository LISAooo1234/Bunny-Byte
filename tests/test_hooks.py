import json
import os
import shlex
import sys
import time

import pytest

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.core import hooks as hooks_module
from bunnybyte.core.hook_runner import HookRunner
from bunnybyte.core.hooks import HookSet, HookSpec, load_hooks, parse_hooks, trust_project_hooks
from bunnybyte.testing import ScriptedModelClient

PY = shlex.quote(sys.executable)


def spec(command, event="pre_tool", **kwargs):
    return HookSpec(event=event, command=command, **kwargs)


def build_agent(tmp_path, hookset=None, outputs=("<final>ok</final>",), **kwargs):
    agent = BunnyByte(
        model_client=ScriptedModelClient(list(outputs)),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        approval_policy=kwargs.pop("approval_policy", "auto"),
        **kwargs,
    )
    if hookset is not None:
        agent.hooks = HookRunner(hookset)
    return agent


def script(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return f"{PY} -B {shlex.quote(str(path))}"


def test_parse_accepts_entries_and_reports_mistakes_without_raising():
    raw = {
        "pre_tool": [{"command": "a", "match": "write_file|patch_file", "timeout": 5}, {"match": "x"}],
        "post_tool": {"command": "b", "on_error": "block"},
        "on_final": [{"command": "c", "only_if_changed": True}],
        "max_final_rounds": 1,
        "pre_toll": [{"command": "typo"}],
    }

    specs, problems, rounds = parse_hooks(raw, source="global")

    assert [(item.event, item.command) for item in specs] == [("pre_tool", "a"), ("post_tool", "b"), ("on_final", "c")]
    assert specs[0].match == ("write_file", "patch_file") and specs[0].timeout == 5
    assert specs[1].on_error == "block" and specs[2].only_if_changed is True
    assert rounds == 1
    assert any("缺少 command" in item for item in problems)
    assert any("pre_toll" in item for item in problems)


@pytest.mark.parametrize(
    "entry",
    [
        {"command": "a", "timeout": 0},
        {"command": "a", "timeout": "5"},
        {"command": "a", "timeout": 999},
        {"command": "a", "on_error": "maybe"},
        {"command": "a", "match": 3},
        {"command": "a", "mode": "x"},
        {"command": "a", "only_if_changed": True},  # only_if_changed 只属于 on_final
        {"command": "   "},
    ],
)
def test_invalid_entries_are_skipped_with_a_reason(entry):
    specs, problems, _ = parse_hooks({"pre_tool": [entry]}, source="global")

    assert specs == [] and len(problems) == 1


def test_match_globs_pick_tools_and_on_final_ignores_match():
    pre = spec("a", match=("write_*", "run_shell"))
    assert pre.matches("write_file") and pre.matches("run_shell") and not pre.matches("read_file")
    hookset = HookSet(specs=(pre, spec("b", event="on_final")))
    assert [item.command for item in hookset.for_event("pre_tool", "write_file")] == ["a"]
    assert hookset.for_event("pre_tool", "read_file") == []
    assert [item.command for item in hookset.for_event("on_final", "")] == ["b"]


@pytest.fixture
def trust_file(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks_module, "STARTUP_TRUST_ENV", False)
    monkeypatch.setenv("BUNNYBYTE_HOME", str(tmp_path / "home"))
    return tmp_path / "home" / "trusted_hooks.json"


def test_project_hooks_wait_for_trust_and_editing_them_revokes_it(tmp_path, trust_file):
    config = tmp_path / ".bunnybyte.toml"
    raw = {"pre_tool": [{"command": "echo one"}]}

    first = load_hooks({}, raw, config)
    assert first.specs == () and [item.command for item in first.pending] == ["echo one"]

    trust_project_hooks(config, first.project_fingerprint)
    assert [item.command for item in load_hooks({}, raw, config).specs] == ["echo one"]

    edited = load_hooks({}, {"pre_tool": [{"command": "echo two"}]}, config)
    assert edited.specs == () and len(edited.pending) == 1
    other_project = load_hooks({}, raw, tmp_path / "other" / ".bunnybyte.toml")
    assert other_project.specs == ()
    assert json.loads(trust_file.read_text(encoding="utf-8")) == {str(config): first.project_fingerprint}


def test_global_hooks_run_without_trust_and_mix_with_pending_project_hooks(tmp_path, trust_file):
    result = load_hooks({"pre_tool": [{"command": "g"}]}, {"post_tool": [{"command": "p"}]}, tmp_path / "x.toml")

    assert [item.source for item in result.specs] == ["global"]
    assert [item.source for item in result.pending] == ["project"]


def test_a_cloned_repos_dotenv_cannot_trust_its_own_hooks(tmp_path, trust_file, monkeypatch):
    monkeypatch.setenv(hooks_module.TRUST_ENV, "1")  # 模拟 load_project_env 之后 os.environ 里多出来的值

    result = load_hooks({}, {"pre_tool": [{"command": "x"}]}, tmp_path / "c.toml")

    assert result.specs == ()

    monkeypatch.setattr(hooks_module, "STARTUP_TRUST_ENV", True)  # 用户在启动 bunny 之前自己设置的才算数
    assert len(load_hooks({}, {"pre_tool": [{"command": "x"}]}, tmp_path / "c.toml").specs) == 1


def test_runner_hands_json_payload_and_env_to_the_hook(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_API_KEY", "sk-should-not-leak")
    command = script(
        tmp_path,
        "h.py",
        "import json, os, sys\n"
        "payload = json.load(sys.stdin)\n"
        "open('seen.json', 'w').write(json.dumps({'p': payload, 'event': os.environ['BUNNYBYTE_HOOK_EVENT'],"
        " 'tool': os.environ['BUNNYBYTE_TOOL_NAME'], 'dir': os.environ['BUNNYBYTE_PROJECT_DIR'],"
        " 'key': os.environ.get('SECRET_API_KEY')}))\n",
    )
    agent = build_agent(tmp_path, HookSet(specs=(spec(command),)))

    outcome = agent.hooks.run_one(agent, spec(command), {"hook_event_name": "pre_tool", "tool_input": {"path": "é.py"}}, "write_file")

    seen = json.loads((tmp_path / "seen.json").read_text(encoding="utf-8"))
    assert outcome.status == "ok" and outcome.exit_code == 0
    assert seen["p"]["tool_input"] == {"path": "é.py"}
    assert (seen["event"], seen["tool"], seen["dir"]) == ("pre_tool", "write_file", str(agent.root))
    assert seen["key"] is None


@pytest.mark.parametrize(
    "code,status,blocks",
    [(0, "ok", False), (2, "block", True), (1, "error", False), (127, "error", False)],
)
def test_exit_codes_mean_continue_block_or_hook_error(tmp_path, code, status, blocks):
    agent = build_agent(tmp_path)
    runner = HookRunner(HookSet())

    outcome = runner.run_one(agent, spec(f"echo said-something; exit {code}"), {})

    assert (outcome.status, outcome.blocks, outcome.exit_code) == (status, blocks, code)
    assert outcome.output == "said-something"


def test_on_error_block_turns_hook_failures_and_timeouts_into_blocks(tmp_path):
    agent = build_agent(tmp_path)
    runner = HookRunner(HookSet())

    failed = runner.run_one(agent, spec("exit 3", on_error="block"), {})
    slow = runner.run_one(agent, spec("sleep 30", on_error="block", timeout=1), {})

    assert failed.status == "error" and failed.blocks
    assert slow.status == "timeout" and slow.blocks


def test_timeout_kills_the_hook_and_its_children(tmp_path):
    agent = build_agent(tmp_path)
    marker = tmp_path / "child.pid"
    command = f"sleep 60 & echo $! > {shlex.quote(str(marker))}; wait"

    started = time.monotonic()
    outcome = HookRunner(HookSet()).run_one(agent, spec(command, timeout=1), {})

    assert outcome.status == "timeout" and time.monotonic() - started < 10
    pid = int(marker.read_text().strip())
    time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_background_child_does_not_hold_the_hook_open(tmp_path):
    agent = build_agent(tmp_path)

    started = time.monotonic()
    outcome = HookRunner(HookSet()).run_one(agent, spec("sleep 3 & echo notified", timeout=20), {})

    assert outcome.status == "ok" and outcome.output == "notified"
    assert time.monotonic() - started < 2.5


def test_a_payload_bigger_than_the_pipe_buffer_does_not_deadlock_a_hook_that_ignores_stdin(tmp_path):
    agent = build_agent(tmp_path)

    outcome = HookRunner(HookSet()).run_one(agent, spec("exit 0", timeout=5), {"tool_input": {"content": "x" * 400_000}})

    assert outcome.status == "ok"


def test_runs_are_recorded_in_trace_events_and_task_state(tmp_path):
    agent = build_agent(tmp_path, HookSet(specs=(spec("echo hi"),)), outputs=['<tool>{"name":"list_files","args":{}}</tool>', "<final>ok</final>"])

    list(agent.engine.run_turn("看看目录"))

    runs = agent.current_task_state.hooks["runs"]
    assert [(item["event"], item["tool_name"], item["status"]) for item in runs] == [("pre_tool", "list_files", "ok")]
    trace = [json.loads(line) for line in agent.run_store.trace_path(agent.current_task_state).read_text().splitlines()]
    assert [item["event"] for item in trace if item["event"] == "hook_run"] == ["hook_run"]
    report = json.loads((agent.current_run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["hooks"]["runs"][0]["command"] == "echo hi"


def test_an_untrusted_project_cannot_change_the_global_final_rounds_and_trust_covers_the_value(tmp_path, trust_file):
    config = tmp_path / ".bunnybyte.toml"
    global_raw = {"on_final": [{"command": "g"}], "max_final_rounds": 3}
    project_raw = {"pre_tool": [{"command": "p"}], "max_final_rounds": 0}

    pending = load_hooks(global_raw, project_raw, config)
    assert pending.max_final_rounds == 3 and pending.pending

    trust_project_hooks(config, pending.project_fingerprint)
    trusted = load_hooks(global_raw, project_raw, config)
    assert trusted.max_final_rounds == 0 and not trusted.pending

    changed = load_hooks(global_raw, {**project_raw, "max_final_rounds": 5}, config)
    assert changed.pending and changed.max_final_rounds == 3


def test_a_symlinked_project_config_never_runs_its_hooks(tmp_path, monkeypatch, trust_file):
    from bunnybyte.config import resolve_project_hooks

    monkeypatch.setattr("bunnybyte.config.DEFAULT_CONFIG_PATH", tmp_path / "none.toml")
    real = tmp_path / "trusted-project" / ".bunnybyte.toml"
    real.parent.mkdir()
    real.write_text('[[hooks.pre_tool]]\ncommand = "echo hi"\n', encoding="utf-8")
    first = resolve_project_hooks(start=real.parent)
    trust_project_hooks(first.project_config, first.project_fingerprint)
    assert len(resolve_project_hooks(start=real.parent).specs) == 1

    clone = tmp_path / "evil-clone"
    clone.mkdir()
    (clone / ".bunnybyte.toml").symlink_to(real)

    result = resolve_project_hooks(start=clone)

    assert result.specs == () and result.pending == ()
    assert any("符号链接" in problem for problem in result.problems)


def test_hooks_that_is_not_a_table_is_reported_instead_of_silently_ignored(tmp_path, monkeypatch, trust_file):
    from bunnybyte.config import resolve_project_hooks

    monkeypatch.setattr("bunnybyte.config.DEFAULT_CONFIG_PATH", tmp_path / "none.toml")
    (tmp_path / ".bunnybyte.toml").write_text('hooks = "x"\n', encoding="utf-8")

    result = resolve_project_hooks(start=tmp_path)

    assert any("必须是一个表" in problem for problem in result.problems)


def test_a_real_dotenv_in_the_cloned_repo_cannot_trust_its_hooks(tmp_path):
    import subprocess

    (tmp_path / ".bunnybyte.toml").write_text('[[hooks.pre_tool]]\ncommand = "echo hi"\n', encoding="utf-8")
    (tmp_path / ".env").write_text("BUNNYBYTE_TRUST_HOOKS=1\n", encoding="utf-8")
    code = (
        "import os\n"
        "from bunnybyte.config import load_project_env, resolve_project_hooks\n"
        f"root = {str(tmp_path)!r}\n"
        "load_project_env(root, override=True)\n"
        "assert os.environ['BUNNYBYTE_TRUST_HOOKS'] == '1'\n"
        "hookset = resolve_project_hooks(start=root)\n"
        "print(len(hookset.specs), len(hookset.pending))\n"
    )
    env = {key: value for key, value in os.environ.items() if key != "BUNNYBYTE_TRUST_HOOKS"}
    env["BUNNYBYTE_HOME"] = str(tmp_path / "home")
    env["HOME"] = str(tmp_path / "fake-home")  # 找不到用户自己的全局配置

    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=tmp_path)

    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["0", "1"]

    env["BUNNYBYTE_TRUST_HOOKS"] = "1"  # 用户自己在启动前设置才算数
    trusted = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=tmp_path)
    assert trusted.stdout.split() == ["1", "0"]
