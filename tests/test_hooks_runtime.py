import json
import shlex
import subprocess
import sys

import pytest

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.cli import _attach_hooks, handle_repl_command
from bunnybyte.config import resolve_project_hooks
from bunnybyte.core import hooks as hooks_module
from bunnybyte.core.hook_runner import HookRunner
from bunnybyte.core.hooks import HookSet, HookSpec
from bunnybyte.core.worker_runtime import build_child_runtime
from bunnybyte.testing import ScriptedModelClient

PY = shlex.quote(sys.executable)
needs_git = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0, reason="git is required"
)


def tool(name, **args):
    return "<tool>" + json.dumps({"name": name, "args": args}) + "</tool>"


def hook(tmp_path, name, body):
    path = tmp_path / f"{name}.py"
    path.write_text(body, encoding="utf-8")
    return f"{PY} -B {shlex.quote(str(path))}"


def build_agent(tmp_path, specs, outputs, rounds=2, **kwargs):
    agent = BunnyByte(
        model_client=ScriptedModelClient(list(outputs)),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        approval_policy=kwargs.pop("approval_policy", "auto"),
        **kwargs,
    )
    agent.hooks = HookRunner(HookSet(specs=tuple(specs), max_final_rounds=rounds))
    return agent


def results(events, name):
    return [event for event in events if event["type"] == "tool_result" and event["name"] == name]


GUARD = (
    "import json, sys\n"
    "payload = json.load(sys.stdin)\n"
    "if str(payload['tool_input'].get('path', '')).endswith('.env'):\n"
    "    print('writing .env files is not allowed here')\n"
    "    sys.exit(2)\n"
)


def test_pre_tool_block_stops_the_write_before_any_approval_prompt(tmp_path, monkeypatch):
    def no_prompt(*_):
        raise AssertionError("a blocked tool call must not reach the approval prompt")

    monkeypatch.setattr("builtins.input", no_prompt)
    guard = HookSpec("pre_tool", hook(tmp_path, "guard", GUARD), match=("write_file",))
    agent = build_agent(
        tmp_path,
        [guard],
        [tool("write_file", path=".env", content="KEY=1\n"), "<final>换个办法</final>"],
        approval_policy="ask",
    )

    events = list(agent.engine.run_turn("写 .env"))

    assert not (tmp_path / ".env").exists()
    blocked = results(events, "write_file")[0]
    assert "blocked by a pre_tool hook" in blocked["content"] and "not allowed here" in blocked["content"]
    assert blocked["metadata"]["tool_status"] == "rejected" and blocked["metadata"]["tool_error_code"] == "hook_blocked"
    assert "blocked by a pre_tool hook" in agent.model_client.prompts[1]


def test_pre_tool_allows_other_calls_and_other_tools_are_not_matched(tmp_path):
    guard = HookSpec("pre_tool", hook(tmp_path, "guard", GUARD), match=("write_file",))
    agent = build_agent(
        tmp_path, [guard], [tool("write_file", path="ok.txt", content="hi\n"), tool("list_files"), "<final>好了</final>"]
    )

    list(agent.engine.run_turn("写文件"))

    assert (tmp_path / "ok.txt").read_text() == "hi\n"
    assert [item["tool_name"] for item in agent.current_task_state.hooks["runs"]] == ["write_file"]


def test_a_crashing_hook_warns_but_the_tool_still_runs(tmp_path):
    broken = HookSpec("pre_tool", "echo boom >&2; exit 7", match=("write_file",))
    agent = build_agent(tmp_path, [broken], [tool("write_file", path="a.txt", content="x\n"), "<final>ok</final>"])

    events = list(agent.engine.run_turn("写"))

    assert (tmp_path / "a.txt").exists()
    content = results(events, "write_file")[0]["content"]
    assert "[hook warning]" in content and "code 7" in content and "boom" in content


@needs_git
def test_post_tool_feedback_reaches_the_model_and_files_the_hook_touches_stay_undoable(tmp_path):
    formatter = hook(
        tmp_path,
        "fmt",
        "import sys\n"
        "open('formatted.txt', 'w').write('by hook\\n')\n"
        "print('lint: line 1 is too long')\n"
        "sys.exit(2)\n",
    )
    agent = build_agent(
        tmp_path,
        [HookSpec("post_tool", formatter, match=("write_file",))],
        [tool("write_file", path="app.py", content="x = 1\n"), "<final>改好了</final>"],
    )

    events = list(agent.engine.run_turn("写 app.py"))

    content = results(events, "write_file")[0]["content"]
    assert "post_tool hook feedback" in content and "line 1 is too long" in content
    assert "line 1 is too long" in agent.model_client.prompts[1]
    assert set(agent.current_task_state.changed_paths) == {"app.py", "formatted.txt"}

    handled, _, output = handle_repl_command(agent, "/undo")
    assert handled and "已撤销" in output
    assert not (tmp_path / "formatted.txt").exists() and not (tmp_path / "app.py").exists()


def test_post_tool_only_follows_write_capable_tools(tmp_path):
    marker = HookSpec("post_tool", "touch ran.txt")
    agent = build_agent(tmp_path, [marker], [tool("list_files"), "<final>ok</final>"])

    list(agent.engine.run_turn("看看"))

    assert not (tmp_path / "ran.txt").exists()


FINAL_CHECK = (
    "import json, os, sys\n"
    "payload = json.load(sys.stdin)\n"
    "counter = 'seen.txt'\n"
    "seen = os.path.exists(counter)\n"
    "open(counter, 'a').write('x')\n"
    "if not seen:\n"
    "    print('CHANGELOG.md was not updated')\n"
    "    sys.exit(2)\n"
)


def test_on_final_block_sends_the_reason_back_and_a_second_answer_passes(tmp_path):
    check = HookSpec("on_final", hook(tmp_path, "final", FINAL_CHECK))
    agent = build_agent(
        tmp_path,
        [check],
        ["<final>做完了</final>", tool("write_file", path="CHANGELOG.md", content="- fix\n"), "<final>补上了更新日志</final>"],
    )

    events = list(agent.engine.run_turn("改点东西"))

    cards = results(events, "hook")
    assert [item["metadata"]["tool_status"] for item in cards] == ["error", "ok"]
    assert "CHANGELOG.md was not updated" in cards[0]["content"]
    assert "An on_final hook blocked your final answer" in agent.model_client.prompts[1]
    assert [event["content"] for event in events if event["type"] == "final"] == ["补上了更新日志"]
    assert agent.current_task_state.hooks["final_rounds"] == 1
    assert agent.current_task_state.tool_steps == 1  # 钩子的反馈轮次不占工具步数


def test_on_final_gives_up_after_the_round_limit_and_says_so(tmp_path):
    always = HookSpec("on_final", "echo nope; exit 2")
    agent = build_agent(tmp_path, [always], ["<final>一</final>", "<final>二</final>", "<final>三</final>"])

    events = list(agent.engine.run_turn("随便"))

    assert len(results(events, "hook")) == 3  # 初次 + 2 轮反馈后的再次检查
    assert [event["content"] for event in events if event["type"] == "final"] == ["三"]
    notice = [event["content"] for event in events if event["type"] == "runtime_notice"][0]
    assert "仍然拦截了最终回答" in notice and "nope" in notice
    assert agent.current_task_state.status == "completed"
    assert agent.current_task_state.hooks["final_blocked"] is True


def test_on_final_can_skip_turns_that_changed_nothing(tmp_path):
    check = HookSpec("on_final", "touch ran.txt; exit 2", only_if_changed=True)
    agent = build_agent(tmp_path, [check], ["<final>只是回答问题</final>"])

    events = list(agent.engine.run_turn("这是什么项目"))

    assert results(events, "hook") == [] and not (tmp_path / "ran.txt").exists()


def test_on_final_still_runs_after_the_step_budget_ends_but_cannot_ask_for_more(tmp_path):
    notify = HookSpec("on_final", "touch notified.txt; exit 2")
    agent = build_agent(tmp_path, [notify], [tool("list_files"), "目前只看了目录。"], max_steps=1)

    events = list(agent.engine.run_turn("看看"))

    assert (tmp_path / "notified.txt").exists()
    assert agent.current_task_state.stop_reason == "step_limit_reached"
    assert len(results(events, "hook")) == 1
    assert agent.current_task_state.hooks.get("final_rounds", 0) == 0


def test_subagents_inherit_pre_tool_hooks_but_not_on_final(tmp_path):
    specs = [HookSpec("pre_tool", "exit 2", match=("write_file",)), HookSpec("on_final", "touch final-ran.txt")]
    parent = build_agent(tmp_path, specs, ["<final>x</final>"])

    child = build_child_runtime(parent, "worker", ["notes"])
    child.model_client = ScriptedModelClient(["<final>子任务完成</final>"])

    assert child.hooks is parent.hooks
    assert "blocked by a pre_tool hook" in child.run_tool("write_file", {"path": "notes/a.txt", "content": "x"})
    list(child.engine.run_turn("子任务"))
    assert not (tmp_path / "final-ran.txt").exists()


@pytest.fixture
def isolated_trust(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks_module, "STARTUP_TRUST_ENV", False)
    monkeypatch.setenv("BUNNYBYTE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("bunnybyte.config.DEFAULT_CONFIG_PATH", tmp_path / "home" / "global.toml")
    return tmp_path


def write_project_config(root, body):
    (root / ".bunnybyte.toml").write_text(body, encoding="utf-8")


PROJECT_HOOKS = """
[[hooks.pre_tool]]
match = "run_shell"
command = "echo guard"

[[hooks.on_final]]
command = "echo done"
only_if_changed = true
"""


def test_config_loads_project_hooks_as_pending_and_global_or_explicit_ones_as_active(isolated_trust):
    root = isolated_trust
    write_project_config(root, PROJECT_HOOKS)
    (root / "home").mkdir()
    (root / "home" / "global.toml").write_text('[[hooks.post_tool]]\ncommand = "echo fmt"\n', encoding="utf-8")

    hookset = resolve_project_hooks(start=root)

    assert [(item.event, item.source) for item in hookset.specs] == [("post_tool", "global")]
    assert [(item.event, item.source) for item in hookset.pending] == [("pre_tool", "project"), ("on_final", "project")]
    assert hookset.pending[1].only_if_changed is True

    explicit = resolve_project_hooks(start=root, config_path=str(root / ".bunnybyte.toml"))
    assert len(explicit.specs) == 2 and explicit.pending == ()


def test_hooks_command_lists_state_and_trust_activates_pending_hooks(isolated_trust):
    root = isolated_trust
    write_project_config(root, PROJECT_HOOKS + "\n[[hooks.nope]]\ncommand = 'x'\n")
    agent = build_agent(root, [], ["<final>ok</final>"])
    agent.hooks = HookRunner(resolve_project_hooks(start=root))

    _, _, listing = handle_repl_command(agent, "/hooks")
    assert "未信任，不会运行" in listing and "`echo guard`" in listing and "nope" in listing
    assert agent.hooks.specs_for("pre_tool", "run_shell") == []

    _, _, trusted = handle_repl_command(agent, "/hooks trust")
    assert "已信任项目钩子" in trusted and "运行中" in trusted
    assert [item.command for item in agent.hooks.specs_for("pre_tool", "run_shell")] == ["echo guard"]
    assert resolve_project_hooks(start=root).pending == ()  # 下次启动不用再确认

    write_project_config(root, PROJECT_HOOKS.replace("echo guard", "echo changed"))
    assert len(resolve_project_hooks(start=root).pending) == 2  # 内容改了，信任作废

    _, _, again = handle_repl_command(agent, "/hooks trust now")
    assert "Usage" in again


def test_hooks_command_without_any_hooks_explains_how_to_add_one(tmp_path):
    agent = build_agent(tmp_path, [], ["<final>ok</final>"])
    agent.hooks = None

    _, _, output = handle_repl_command(agent, "/hooks")

    assert "还没有配置钩子" in output and "hooks.pre_tool" in output
    _, _, nothing = handle_repl_command(agent, "/hooks trust")
    assert "没有等待确认" in nothing


def test_startup_warns_about_untrusted_project_hooks_and_config_mistakes(isolated_trust, capsys):
    root = isolated_trust
    write_project_config(root, PROJECT_HOOKS + "\n[[hooks.pre_tool]]\nmatch = 'x'\n")
    agent = build_agent(root, [], ["<final>ok</final>"])
    agent.hooks = None

    _attach_hooks(agent, resolve_project_hooks(start=root))

    err = capsys.readouterr().err
    assert "2 条钩子，确认前不会运行" in err and "缺少 command" in err
    assert agent.hooks is not None and agent.hooks.specs_for("pre_tool", "run_shell") == []


def test_doctor_reports_hook_state(isolated_trust):
    from bunnybyte.doctor import check_hooks

    root = isolated_trust
    assert check_hooks(root).status == "skip"

    write_project_config(root, PROJECT_HOOKS)
    pending = check_hooks(root)
    assert pending.status == "warn" and "2 条未信任" in pending.detail and "/hooks trust" in pending.hint

    found = resolve_project_hooks(start=root)
    hooks_module.trust_project_hooks(found.project_config, found.project_fingerprint)
    trusted = check_hooks(root)
    assert trusted.status == "ok" and "pre_tool 1" in trusted.detail and "on_final 1" in trusted.detail

    write_project_config(root, "[hooks\nbroken")
    assert check_hooks(root).status == "fail"


def test_aborting_while_a_pre_tool_hook_runs_cancels_the_tool_instead_of_letting_it_through(tmp_path):
    import threading
    import time

    slow_guard = HookSpec("pre_tool", "sleep 3; exit 2", match=("write_file",), on_error="block")
    agent = build_agent(tmp_path, [slow_guard], ["<final>ok</final>"])
    threading.Timer(0.6, lambda: setattr(agent, "abort_requested", True)).start()

    started = time.monotonic()
    result = agent.run_tool("write_file", {"path": "secret.env", "content": "K=1\n"})

    assert time.monotonic() - started < 2.5
    assert "cancelled" in result and not (tmp_path / "secret.env").exists()


def test_on_error_block_stops_the_tool_when_the_guard_itself_crashes(tmp_path):
    crashing = HookSpec("pre_tool", "echo guard-crashed; exit 7", match=("write_file",), on_error="block")
    agent = build_agent(tmp_path, [crashing], ["<final>ok</final>"])

    result = agent.run_tool("write_file", {"path": "a.txt", "content": "x\n"})

    assert "on_error" in result and "guard-crashed" in result and not (tmp_path / "a.txt").exists()


def test_the_parent_and_a_background_worker_never_run_hooks_at_the_same_time(tmp_path):
    import threading

    log = tmp_path / "log.txt"
    body = (
        "import time\n"
        f"log = open({str(log)!r}, 'a')\n"
        "log.write('S\\n'); log.flush()\n"
        "time.sleep(0.4)\n"
        "log.write('E\\n'); log.flush()\n"
    )
    parent = build_agent(tmp_path, [HookSpec("pre_tool", hook(tmp_path, "slow", body), match=("list_files",))], ["<final>ok</final>"])
    child = build_child_runtime(parent, "worker", ["notes"])

    threads = [threading.Thread(target=agent.run_tool, args=("list_files", {"path": "."})) for agent in (parent, child)]
    [thread.start() for thread in threads]
    [thread.join() for thread in threads]

    assert log.read_text().split() == ["S", "E", "S", "E"]


def test_subagents_still_do_not_run_on_final_while_the_parent_does(tmp_path):
    parent = build_agent(tmp_path, [HookSpec("on_final", "echo ran >> finals.txt")], ["<final>主任务完成</final>"])
    child = build_child_runtime(parent, "worker", ["notes"])
    child.model_client = ScriptedModelClient(["<final>子任务完成</final>"])

    list(child.engine.run_turn("子任务"))
    assert not (tmp_path / "finals.txt").exists()
    list(parent.engine.run_turn("主任务"))
    assert (tmp_path / "finals.txt").read_text().split() == ["ran"]


def test_only_if_changed_hooks_do_run_once_files_changed(tmp_path):
    check = HookSpec("on_final", "echo ran >> finals.txt", only_if_changed=True)
    agent = build_agent(tmp_path, [check], [tool("write_file", path="a.txt", content="x\n"), "<final>写好了</final>"])

    list(agent.engine.run_turn("写个文件"))

    assert (tmp_path / "finals.txt").read_text().split() == ["ran"]


def test_a_hook_machinery_failure_in_on_final_is_a_warning_not_a_crashed_turn(tmp_path, monkeypatch):
    def broken(*_args, **_kwargs):
        raise OSError("too many open files")

    monkeypatch.setattr("bunnybyte.core.hook_runner.tempfile.TemporaryFile", broken)
    agent = build_agent(tmp_path, [HookSpec("on_final", "exit 0")], ["<final>好了</final>"])

    events = list(agent.engine.run_turn("随便"))

    card = results(events, "hook")[0]
    assert "出错" in card["content"] and "too many open files" in card["content"]
    assert [event["content"] for event in events if event["type"] == "final"] == ["好了"]
    assert agent.current_task_state.status == "completed"


def test_hook_warnings_survive_a_tool_that_raises(tmp_path):
    import dataclasses

    agent = build_agent(tmp_path, [HookSpec("pre_tool", "echo odd; exit 9", match=("write_file",))], ["<final>ok</final>"])

    def explode(_args):
        raise RuntimeError("disk full")

    agent.tools["write_file"] = dataclasses.replace(agent.tools["write_file"], runner=explode)

    result = agent.run_tool("write_file", {"path": "a.txt", "content": "x\n"})

    assert "disk full" in result and "[hook warning]" in result and "code 9" in result


def test_long_hook_output_is_clipped(tmp_path):
    agent = build_agent(tmp_path, [], ["<final>ok</final>"])

    outcome = HookRunner(HookSet()).run_one(agent, HookSpec("pre_tool", f"{PY} -c \"print('y' * 500000)\""), {})

    assert outcome.status == "ok" and len(outcome.output) < 1700 and "truncated" in outcome.output


def test_headless_exits_5_when_an_on_final_hook_still_blocks_after_its_rounds(tmp_path):
    import io

    from bunnybyte.headless import EXIT_HOOK_BLOCKED, EXIT_OK, prepare_agent, run_headless

    def headless(command):
        agent = build_agent(
            tmp_path / command[0], [HookSpec("on_final", command[1])], ["<final>一</final>", "<final>二</final>", "<final>三</final>"]
        )
        prepare_agent(agent)
        out, err = io.StringIO(), io.StringIO()
        code = run_headless(agent, "干活", stdout=out, stderr=err, output_format="json")
        return code, json.loads(out.getvalue()), err.getvalue()

    (tmp_path / "blocked").mkdir()
    (tmp_path / "fine").mkdir()

    code, result, err = headless(("blocked", "echo nope; exit 2"))
    assert code == EXIT_HOOK_BLOCKED and result["ok"] is False and result["hooks"]["final_blocked"] is True
    assert result["status"] == "completed" and "退出码 5" in err

    code, result, _ = headless(("fine", "exit 0"))
    assert code == EXIT_OK and result["hooks"] == {"final_blocked": False, "runs": 1}
