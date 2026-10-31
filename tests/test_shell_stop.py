import os
import subprocess
import sys
import threading
import time

import pytest

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.features.sandbox.config import SandboxConfig
from bunnybyte.features.sandbox.process import STOPPED_NOTE, run_command
from bunnybyte.features.sandbox.runner import SandboxRunner
from bunnybyte.testing import ScriptedModelClient

pytestmark = pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX-only")


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_gone(pid, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def build_agent(tmp_path, outputs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return BunnyByte(
        model_client=ScriptedModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        approval_policy="auto",
    )


def test_stop_kills_running_shell_command_and_its_children_within_a_second(tmp_path):
    pid_file = tmp_path / "child.pid"
    command = f"sleep 30 & echo $! > {pid_file}; wait"
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()

    started = time.monotonic()
    result = run_command(command, cwd=tmp_path, env=os.environ.copy(), timeout=60, should_stop=stop.is_set)

    assert time.monotonic() - started < 4
    assert result.stopped is True
    assert STOPPED_NOTE in result.stderr
    assert _wait_gone(int(pid_file.read_text().strip()))


def test_timeout_kills_grandchildren_instead_of_only_the_shell(tmp_path):
    pid_file = tmp_path / "child.pid"
    command = f"sleep 30 & echo $! > {pid_file}; wait"

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_command(command, cwd=tmp_path, env=os.environ.copy(), timeout=1)

    assert time.monotonic() - started < 5
    assert _wait_gone(int(pid_file.read_text().strip()))


def test_finished_command_returns_like_subprocess_run(tmp_path):
    result = run_command(f"{sys.executable} -c 'import sys; print(1); sys.stderr.write(\"e\")'; exit 3",
                         cwd=tmp_path, env=os.environ.copy(), timeout=10, should_stop=lambda: False)

    assert (result.returncode, result.stdout.strip(), result.stderr.strip(), result.stopped) == (3, "1", "e", False)


def test_sandbox_runner_plain_path_is_stoppable(tmp_path):
    runner = SandboxRunner(SandboxConfig(mode="best_effort", backend="bubblewrap"), which=lambda name: None)
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()

    started = time.monotonic()
    result = runner.run("sleep 30", cwd=tmp_path, env=os.environ.copy(), timeout=60, should_stop=stop.is_set)

    assert time.monotonic() - started < 4
    assert result.stopped is True


def test_stop_during_run_shell_ends_the_turn_without_waiting_for_the_command(tmp_path):
    agent = build_agent(
        tmp_path,
        ['<tool>{"name":"run_shell","args":{"command":"sleep 30","timeout":60}}</tool>', "<final>never</final>"],
    )
    threading.Timer(0.5, agent.abort_current_turn).start()

    started = time.monotonic()
    events = list(agent.engine.run_turn("run it"))

    assert time.monotonic() - started < 5
    result = next(event for event in events if event["type"] == "tool_result")
    assert STOPPED_NOTE in result["content"]
    assert events[-1]["stop_reason"] == "aborted"


def test_run_shell_timeout_tells_the_model_what_happened_and_keeps_partial_output(tmp_path):
    agent = build_agent(
        tmp_path,
        ['<tool>{"name":"run_shell","args":{"command":"echo started; sleep 30","timeout":1}}</tool>', "<final>ok</final>"],
    )

    events = list(agent.engine.run_turn("run it"))

    result = next(event for event in events if event["type"] == "tool_result")
    assert "命令超时（1 秒）" in result["content"]
    assert "started" in result["content"]


def test_run_shell_does_not_share_the_terminal_stdin(tmp_path):
    result = run_command("cat", cwd=tmp_path, env=os.environ.copy(), timeout=5)

    assert (result.returncode, result.stdout) == (0, "")


def test_timeout_output_is_masked_in_the_tool_result_event(tmp_path):
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    agent = build_agent(
        tmp_path,
        [f'<tool>{{"name":"run_shell","args":{{"command":"echo {token}; sleep 30","timeout":1}}}}</tool>', "<final>ok</final>"],
    )

    events = list(agent.engine.run_turn("run it"))

    result = next(event for event in events if event["type"] == "tool_result")
    assert "命令超时" in result["content"]
    assert token not in result["content"] and "<secret:" in result["content"]


def test_detached_child_holding_the_pipe_keeps_the_output_printed_before_timeout(tmp_path):
    script = "import os, time; os.setsid(); time.sleep(6)"
    command = f"echo before; {sys.executable} -c '{script}' & echo after; wait"

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        run_command(command, cwd=tmp_path, env=os.environ.copy(), timeout=1)

    assert time.monotonic() - started < 5.5
    assert "before" in str(caught.value.output) and "after" in str(caught.value.output)
