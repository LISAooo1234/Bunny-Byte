"""Run a shell command that can be stopped mid-way and never leaves children behind.

`subprocess.run(shell=True, timeout=...)` only kills the shell on timeout: a
`sleep`, test runner or dev server started by it keeps running (and keeps the
output pipes open, so the call can hang), and the agent's Stop button has to
wait for the whole command. Here the command gets its own process group, the
wait loop polls `should_stop` a few times a second, and timeout or stop kills
the whole group (SIGTERM, then SIGKILL after a short grace period).
"""

import os
import signal
import subprocess
import time

POLL_SECONDS = 0.1
TERM_GRACE_SECONDS = 1.0
DRAIN_SECONDS = 2.0
STOPPED_NOTE = "[命令已按用户要求停止，进程组已终止]"


def run_command(command, *, cwd, env, timeout, should_stop=None, shell=True):
    """Like `subprocess.run(..., capture_output=True, text=True)`.

    Raises `subprocess.TimeoutExpired` on timeout (same as before). When
    `should_stop()` turns true the group is killed and a result is returned with
    `stopped = True` and a note appended to stderr.
    """
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        shell=shell,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=max(0.01, min(POLL_SECONDS, deadline - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                stopped = bool(should_stop and should_stop())
                if not stopped and time.monotonic() < deadline:
                    continue
                kill_group(process)
                stdout, stderr = _drain(process)
                if not stopped:
                    raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr) from None
                result = subprocess.CompletedProcess(command, _returncode(process), stdout, _with_note(stderr))
                result.stopped = True
                return result
    except BaseException:
        kill_group(process)  # Ctrl-C 等中断：新会话里的子进程收不到终端的 SIGINT，不能留下孤儿
        raise
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    result.stopped = False
    return result


def timeout_message(exc, limit=2000):
    """Tool error text for a timed-out command: say what happened and keep the tail of what it printed."""
    output = "\n".join(part.strip() for part in (_text(exc.output), _text(exc.stderr)) if part and part.strip())
    message = f"命令超时（{exc.timeout:g} 秒），已终止整个进程组"
    if not output:
        return message + "，没有输出"
    tail = output if len(output) <= limit else "…" + output[-limit:]
    return f"{message}。超时前的输出（末尾）：\n{tail}"


def kill_group_now(process):
    _signal_group(process, getattr(signal, "SIGKILL", signal.SIGTERM))


def kill_group(process):
    if process.poll() is None or _group_alive(process):
        _signal_group(process, signal.SIGTERM)
        grace = time.monotonic() + TERM_GRACE_SECONDS
        while time.monotonic() < grace and (process.poll() is None or _group_alive(process)):
            time.sleep(0.05)
        _signal_group(process, getattr(signal, "SIGKILL", signal.SIGTERM))
    try:
        process.wait(timeout=DRAIN_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def _signal_group(process, sig):
    try:
        if hasattr(os, "killpg"):
            os.killpg(process.pid, sig)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        pass


def _group_alive(process):
    if not hasattr(os, "killpg"):
        return False
    try:
        os.killpg(process.pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _drain(process):
    try:
        return process.communicate(timeout=DRAIN_SECONDS)
    except subprocess.TimeoutExpired as exc:
        # 有进程自己另起了会话还占着管道：不再等它，但已经读到的输出要留下
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except (OSError, ValueError, AttributeError):
                pass
        return _text(exc.output), _text(exc.stderr)


def _returncode(process):
    code = process.poll()
    return code if code is not None else -int(getattr(signal, "SIGKILL", signal.SIGTERM))


def _text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _with_note(stderr):
    stderr = str(stderr or "").rstrip()
    return f"{stderr}\n{STOPPED_NOTE}" if stderr else STOPPED_NOTE
