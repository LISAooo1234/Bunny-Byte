"""执行钩子子进程，并把结果接到工具调用上。

协议（和 Claude Code 的钩子脚本兼容的那一部分）：钩子从 stdin 读到一份 JSON，退出码 0 表示放行，
退出码 2 表示拦截（pre_tool / on_final）或反馈（post_tool），其余退出码和超时按“钩子自己出错”处理，
默认放行并给出警告，除非该钩子配置了 `on_error = "block"`。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass

from .hooks import HookSet, HookSpec
from .verification import _kill_group
from .workspace import clip

BLOCK_EXIT_CODE = 2
OUTPUT_LIMIT = 1500
RESPONSE_LIMIT = 20000
MAX_RECORDED = 40
OUTPUT_READ_LIMIT = 64_000

STATUS_OK = "ok"
STATUS_BLOCK = "block"
STATUS_ERROR = "error"
STATUS_TIMEOUT = "timeout"
STATUS_ABORTED = "aborted"


@dataclass(frozen=True)
class HookOutcome:
    spec: HookSpec
    status: str
    exit_code: int | None
    output: str
    duration_ms: int

    @property
    def blocks(self):
        if self.status == STATUS_BLOCK:
            return True
        return self.spec.on_error == "block" and self.status in (STATUS_ERROR, STATUS_TIMEOUT)

    @property
    def failed(self):
        return self.status in (STATUS_ERROR, STATUS_TIMEOUT)

    def describe(self, zh=False):
        if self.status == STATUS_TIMEOUT:
            return f"超时（{self.spec.timeout} 秒）" if zh else f"timed out after {self.spec.timeout}s"
        return f"退出码 {self.exit_code}" if zh else f"exited with code {self.exit_code}"

    def to_record(self, tool_name=""):
        record = {
            "event": self.spec.event,
            "command": self.spec.command,
            "source": self.spec.source,
            "status": self.status,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "blocked": self.blocks,
        }
        if tool_name:
            record["tool_name"] = tool_name
        if self.output:
            record["output"] = clip(self.output, 500)
        return record


class HookRunner:
    def __init__(self, hookset=None):
        self.hookset = hookset or HookSet()
        self._lock = threading.Lock()  # 记录
        self._exec_lock = threading.Lock()  # 父 agent 和后台 worker 共用同一个 runner：钩子一次只跑一个，免得格式化类钩子互相踩

    def has(self, event):
        return self.hookset.has(event)

    def specs_for(self, event, tool_name=""):
        return self.hookset.for_event(event, tool_name)

    def activate(self, hookset):
        self.hookset = hookset

    def run(self, agent, event, payload, tool_name=""):
        """按顺序跑某个事件的全部匹配钩子；拦截类事件遇到第一个拦截就停。"""
        outcomes = []
        for spec in self.specs_for(event, tool_name):
            outcome = self.run_one(agent, spec, payload, tool_name)
            outcomes.append(outcome)
            if agent.abort_requested or (outcome.blocks and event != "post_tool"):
                break
        return outcomes

    def run_one(self, agent, spec, payload, tool_name=""):
        started = time.monotonic()
        try:
            with self._exec_lock:
                outcome = _execute(agent, spec, payload, tool_name)
        except Exception as exc:  # 临时文件、fork 之类的故障算“钩子出错”，不能让回合循环崩掉
            outcome = _outcome(spec, STATUS_ERROR, None, f"{type(exc).__name__}: {exc}", started)
        try:
            self._record(agent, outcome, tool_name)
        except Exception:
            pass  # 记录失败不影响钩子结果
        return outcome

    def _record(self, agent, outcome, tool_name):
        record = agent.redact_artifact(outcome.to_record(tool_name))
        agent.session_event_bus.emit("hook_run", dict(record))
        task_state = getattr(agent, "current_task_state", None)
        if task_state is None:
            return
        with self._lock:
            runs = task_state.hooks.setdefault("runs", [])
            runs.append(record)
            del runs[:-MAX_RECORDED]
        agent.emit_trace(task_state, "hook_run", dict(record))


def _execute(agent, spec, payload, tool_name):
    started = time.monotonic()
    env = dict(
        agent.shell_env(),
        BUNNYBYTE_HOOK_EVENT=spec.event,
        BUNNYBYTE_PROJECT_DIR=str(agent.root),
        BUNNYBYTE_TOOL_NAME=str(tool_name or ""),
    )
    # 输出写进临时文件而不是管道：钩子里 `cmd &` 起的后台进程会一直攥着管道，communicate 就等不到结束
    with tempfile.TemporaryFile() as sink:
        try:
            process = subprocess.Popen(
                spec.command,
                cwd=agent.root,
                shell=True,
                env=env,
                stdin=subprocess.PIPE,
                stdout=sink,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                start_new_session=True,
            )
        except OSError as exc:
            return _outcome(spec, STATUS_ERROR, None, f"{type(exc).__name__}: {exc}", started)
        data = json.dumps(payload, ensure_ascii=False, default=str)
        try:
            code, timed_out, aborted = _wait(process, data, started + spec.timeout, agent)
        except BaseException:
            _kill_group(process)  # Ctrl-C 等中断时不留下孤儿进程
            raise
        sink.seek(0)
        output = sink.read(OUTPUT_READ_LIMIT).decode("utf-8", errors="replace")
    if aborted:
        return _outcome(spec, STATUS_ABORTED, None, output, started)
    if timed_out:
        return _outcome(spec, STATUS_TIMEOUT, None, output, started)
    status = STATUS_OK if code == 0 else STATUS_BLOCK if code == BLOCK_EXIT_CODE else STATUS_ERROR
    return _outcome(spec, status, code, output, started)


def _wait(process, data, deadline, agent):
    pending_input = data
    while True:
        try:
            process.communicate(input=pending_input, timeout=max(0.05, min(0.5, deadline - time.monotonic())))
            return process.returncode, False, False
        except subprocess.TimeoutExpired:
            pending_input = None  # 输入已经交给 communicate，重试时不能再传
            aborted = bool(getattr(agent, "abort_requested", False))
            if not aborted and time.monotonic() < deadline:
                continue
            _kill_group(process)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            return None, not aborted, aborted


def _outcome(spec, status, code, output, started):
    return HookOutcome(spec, status, code, clip(str(output or "").strip(), OUTPUT_LIMIT), int((time.monotonic() - started) * 1000))


def base_payload(agent, event):
    task_state = getattr(agent, "current_task_state", None)
    return {
        "hook_event_name": event,
        "session_id": agent.session.get("id", ""),
        "run_id": getattr(task_state, "run_id", ""),
        "cwd": str(agent.root),
        "agent_depth": agent.depth,
        "write_scope": list(agent.write_scope),
    }


def pre_tool_hooks(agent, tool, args):
    """工具真正执行前的钩子。返回 (拦截消息, 附加提示)；拦截消息为空表示放行。"""
    runner = getattr(agent, "hooks", None)
    if runner is None or not runner.has("pre_tool"):
        return "", []
    try:
        payload = dict(base_payload(agent, "pre_tool"), tool_name=tool.name, tool_input=args or {})
        outcomes = runner.run(agent, "pre_tool", payload, tool.name)
    except Exception as exc:  # 钩子机制自己的故障不能拖垮工具调用
        return "", [f"[hook warning] pre_tool hooks could not run: {type(exc).__name__}: {exc}"]
    notes = []
    for outcome in outcomes:
        if outcome.blocks:
            return _blocked_message(outcome), notes
        notes += _warning(outcome)
    if agent.abort_requested or any(outcome.status == STATUS_ABORTED for outcome in outcomes):
        return "error: tool call cancelled because the run was aborted while a pre_tool hook was running", notes
    return "", notes


def post_tool_hooks(agent, tool, args, response):
    """工具执行后、工作区快照前的钩子，所以格式化类钩子改的文件也会算进这次工具调用的改动。"""
    runner = getattr(agent, "hooks", None)
    if runner is None or not tool.risky or not runner.has("post_tool"):
        return []  # 只读工具没有前后快照，钩子要是改了文件就逃过 /undo 和验证指纹，所以 post_tool 只跟写入类工具
    try:
        payload = dict(
            base_payload(agent, "post_tool"),
            tool_name=tool.name,
            tool_input=args or {},
            tool_response=clip(response, RESPONSE_LIMIT),
        )
        outcomes = runner.run(agent, "post_tool", payload, tool.name)
    except Exception as exc:
        return [f"[hook warning] post_tool hooks could not run: {type(exc).__name__}: {exc}"]
    notes = []
    for outcome in outcomes:
        if outcome.status == STATUS_BLOCK:
            notes.append(f"[post_tool hook feedback from `{outcome.spec.command}`]\n{outcome.output or '(no output)'}")
        else:
            notes += _warning(outcome)
    return notes


def render_notes(notes):
    return "".join(f"\n\n{note}" for note in notes)


def _blocked_message(outcome):
    detail = outcome.output or "(the hook gave no reason)"
    if outcome.status == STATUS_BLOCK:
        return f"error: blocked by a pre_tool hook (`{outcome.spec.command}`): {detail}"
    return f"error: blocked because the pre_tool hook `{outcome.spec.command}` {outcome.describe()} and is configured with on_error = \"block\": {detail}"


def _warning(outcome):
    if not outcome.failed:
        return []
    tail = f": {outcome.output}" if outcome.output else ""
    return [f"[hook warning] `{outcome.spec.command}` {outcome.describe()}{tail}"]
