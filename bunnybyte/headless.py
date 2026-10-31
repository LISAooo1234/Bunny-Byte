"""无人值守模式（bunny -p）：不提问、不弹界面，stdout 只给结果，退出码说明这次到底做完没有。

退出码：0 完成；1 运行出错；2 用法/配置错误（由 CLI 返回）；3 中途停止（步数上限、空转、重试耗尽、花费到达 --max-cost）；
4 只在 --require-verified 下出现：改了代码，但没有覆盖当前文件内容的通过回执；130 被中断。
"""

import contextlib
import json
import sys
import time

from .core.cost_meter import STOP_REASON_BUDGET_EXCEEDED
from .core.critic_gate import critic_summary
from .core.fix_proof_gate import fix_proof_summary
from .core.pricing import format_usd
from .core.run_usage import trace_usage
from .core.task_state import STATUS_COMPLETED, STATUS_FAILED, STATUS_RUNNING
from .core.verification_verdict import VERDICT_FAILED, VERDICT_UNVERIFIED, verification_verdict
from .core.workspace import clip

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_UNFINISHED = 3
EXIT_UNVERIFIED = 4
EXIT_HOOK_BLOCKED = 5
EXIT_INTERRUPTED = 130

OUTPUT_FORMATS = ("text", "json", "stream-json")
STDIN_LIMIT = 200_000
STREAMED_EVENTS = {
    "tool_call",
    "tool_result",
    "assistant_preamble",
    "retry",
    "runtime_notice",
    "worker_notification",
    "final",
    "stop",
}
BACKGROUND_WAIT_SECONDS = 30
WORKER_STOP_SECONDS = 5
DENIED_PREFIX = "error: approval denied"


def merge_prompt(words, stdin_text):
    prompt = " ".join(word for word in words or [] if word != "-").strip()
    piped = str(stdin_text or "").strip()
    if prompt and piped:
        return f"{prompt}\n\n{piped}"
    return prompt or piped


def wants_stdin(words):
    """没有参数提示词，或参数里有单独的 `-`，才读 stdin。带参数时默认不读：被 cron / 其他 agent 的工具 shell
    继承来的 stdin 可能是一根永不关闭的管道，读它会让运行永远卡住。"""
    words = list(words or [])
    return not words or "-" in words


def read_piped_stdin(stream):
    """stdin 是管道/文件时读到 EOF（最多 STDIN_LIMIT 字符）；终端或没有 stdin 时返回空串。"""
    if stream is None or stream.isatty():
        return ""
    try:
        return stream.read(STDIN_LIMIT)
    except (OSError, ValueError):
        return ""


def prepare_agent(agent):
    """无人值守的运行时设置：审批永不弹问，后台学习类功能关闭，避免 CI 里多花 token 和时间。"""

    def approve(name, args):
        return agent.approval_policy == "auto" and not agent.read_only

    agent.approve = approve
    agent.unattended = True  # 写入型子 agent 自带 auto 审批，ask/never 下不能借它绕过拒绝
    agent.ask_user_callback = None
    agent.auto_dream = False
    for flag in ("skill_evolution", "skill_curator"):
        agent.feature_flags[flag] = False


def _brief(args):
    args = args if isinstance(args, dict) else {}
    for key in ("command", "path", "pattern", "query", "description"):
        if args.get(key):
            return clip(str(args[key]).replace("\n", " "), 100)
    return ""


def _progress_line(event):
    kind = event.get("type")
    if kind == "tool_call":
        return f"› {event.get('name', '')} {_brief(event.get('args'))}".rstrip()
    if kind == "tool_result":
        content = str(event.get("content", ""))
        failed = (event.get("metadata") or {}).get("tool_status") == "error" or content.startswith("error:")
        return f"  ✗ {clip(content.splitlines()[0] if content else '', 160)}" if failed else ""
    if kind == "runtime_notice":
        return str(event.get("content", ""))
    return ""


def _stream_record(event):
    record = {key: event[key] for key in ("type", "run_id", "name", "args") if key in event}
    if "content" in event:
        record["content"] = clip(event["content"], 4000)
    return record


def build_result(agent, state, final, started, *, require_verified, denials, error=""):
    verdict = verification_verdict(agent, state) if state is not None else ""
    code = exit_code(state, verdict, require_verified, error)
    verification = dict(state.verification) if state is not None else {}
    hook_state = dict(getattr(state, "hooks", None) or {})
    usage = {"input_tokens": None, "output_tokens": None, "cached_tokens": None}
    if state is not None:
        usage = trace_usage(agent.run_store.trace_path(state))
    result = {
        "type": "result",
        "ok": code == EXIT_OK,
        "exit_code": code,
        "result": final,
        "status": state.status if state is not None else STATUS_FAILED,
        "stop_reason": state.stop_reason if state is not None else "",
        "session_id": str(agent.session.get("id", "")),
        "run_id": state.run_id if state is not None else "",
        "model": str(getattr(agent.model_client, "model", "") or ""),
        "tool_steps": state.tool_steps if state is not None else 0,
        "wasted_steps": state.wasted_steps if state is not None else 0,
        "permission_denials": denials,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "usage": usage,
        "cost": agent.cost_meter.turn_snapshot() if state is not None else {},
        "changed_paths": list(state.changed_paths) if state is not None else [],
        "verification": {
            "verdict": verdict,
            "status": verification.get("status", ""),
            "commands": [item.get("command", "") for item in verification.get("receipts", [])],
            "repairs": int(verification.get("repairs", 0)),
            "fix_proof": fix_proof_summary(verification),
        },
        "critic": critic_summary(state) if state is not None else {},
        "hooks": {
            "final_blocked": bool(hook_state.get("final_blocked")),
            "runs": len(hook_state.get("runs", [])),
        },
    }
    if error:
        result["error"] = error
    return result


def exit_code(state, verdict, require_verified, error=""):
    if error or state is None or state.status == STATUS_FAILED:
        return EXIT_ERROR
    if state.stop_reason in {"persistence_error", "resume_load_error"}:
        return EXIT_ERROR
    if state.status != STATUS_COMPLETED:
        return EXIT_UNFINISHED
    if require_verified and verdict in {VERDICT_FAILED, VERDICT_UNVERIFIED}:
        return EXIT_UNVERIFIED
    if (getattr(state, "hooks", None) or {}).get("final_blocked"):
        return EXIT_HOOK_BLOCKED
    return EXIT_OK


def startup_failure(message, output_format, stdout=None, stderr=None):
    """启动阶段（配置、--resume 等）就失败：stderr 给人看，json 格式下 stdout 仍然给脚本一个结果对象。"""
    (stderr or sys.stderr).write(f"bunny: {message}\n")
    if output_format != "text":
        out = stdout or sys.stdout
        out.write(json.dumps({"type": "result", "ok": False, "exit_code": EXIT_USAGE, "error": message}, ensure_ascii=False) + "\n")
        out.flush()
    return EXIT_USAGE


def _settle(agent):
    """进程马上要退出：等记忆整理写完，并收走还在跑的后台 worker，避免半截写入。"""
    with contextlib.suppress(Exception):
        agent.wait_for_memory_maintenance(timeout=BACKGROUND_WAIT_SECONDS)
    with contextlib.suppress(Exception):
        agent.worker_manager.shutdown(timeout=WORKER_STOP_SECONDS)


def run_headless(
    agent,
    prompt,
    *,
    output_format="text",
    require_verified=False,
    quiet=False,
    stdout=None,
    stderr=None,
    mention_text=None,
):
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    started = time.monotonic()
    final, error, denials = "", "", 0
    interrupted = False

    def emit(text):
        out.write(text + "\n")
        out.flush()

    def note(text):
        if text and not quiet:
            err.write(text + "\n")
            err.flush()

    if output_format == "stream-json":
        emit(
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": str(agent.session.get("id", "")),
                    "model": str(getattr(agent.model_client, "model", "") or ""),
                    "cwd": str(agent.root),
                    "approval": agent.approval_policy,
                },
                ensure_ascii=False,
            )
        )
    try:
        # 工具或第三方库偶尔会直接 print；整个回合期间把 stdout 指向 stderr，保证 stdout 只有我们写的内容
        with contextlib.redirect_stdout(err):
            # 只预读参数里写的 @path；管道进来的 stdin（diff 的 @@、日志）不当成提及。
            for event in agent.engine.run_turn(prompt, mention_text=mention_text):
                kind = event.get("type")
                if kind == "tool_result" and str(event.get("content", "")).startswith(DENIED_PREFIX):
                    denials += 1
                if kind in {"final", "stop"}:
                    final = str(event.get("content", ""))
                if output_format == "stream-json" and kind in STREAMED_EVENTS:
                    emit(json.dumps(_stream_record(event), ensure_ascii=False))
                elif output_format != "stream-json":
                    note(_progress_line(event))
    except KeyboardInterrupt:
        interrupted = True
        error = "interrupted"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    state = getattr(agent, "current_task_state", None)
    if state is not None and state.status == STATUS_RUNNING:
        state = None  # 回合没走到收尾（异常/中断），不能把半截状态当结果
    with contextlib.redirect_stdout(err):
        _settle(agent)
    result = build_result(agent, state, final, started, require_verified=require_verified, denials=denials, error=error)
    code = EXIT_INTERRUPTED if interrupted else result["exit_code"]
    result["exit_code"], result["ok"] = code, code == EXIT_OK
    if output_format == "text":
        if final:
            emit(final)
    else:
        emit(json.dumps(result, ensure_ascii=False))
    if error:
        err.write(f"bunny: {error}\n")
    if denials:
        err.write(f"bunny: {denials} 个工具调用因审批策略被拒绝（无人值守时 ask 等同 never；需要放行请加 --approval auto）。\n")
    if code == EXIT_UNVERIFIED:
        err.write(f"bunny: 代码有改动但没有覆盖当前内容的通过回执（{result['verification']['verdict']}），退出码 4。\n")
    if state is not None and state.stop_reason == STOP_REASON_BUDGET_EXCEEDED:
        err.write(f"bunny: 本轮花费估算 {format_usd(result['cost'].get('usd'))} 达到上限 {format_usd(result['cost'].get('limit_usd'))}，已停止，退出码 3。\n")
    if code == EXIT_HOOK_BLOCKED:
        err.write("bunny: on_final 钩子在反馈轮次用完后仍然拦截了最终回答，退出码 5。\n")
    err.flush()
    return code
