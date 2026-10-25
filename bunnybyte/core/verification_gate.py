"""回合收尾前的验证闸门：模型想交卷时，先真跑一遍项目检查，失败就把输出塞回去让它修。"""

from .artifacts import build_artifact_graph
from .tool_loop_guard import current_turn_history
from .verification import (
    NON_FAILURE_STATUSES,
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_SKIPPED,
    STATUS_TIMEOUT,
    STATUS_UNAVAILABLE,
    changes_fingerprint,
    claims_verified,
    detect_commands,
    run_command,
    shell_evidence,
)
from .workspace import clip, now

MAX_RECEIPTS = 12

REPAIR_MESSAGE = (
    "Verification failed ({left} repair attempt(s) left): `{command}` exited with {code} after {seconds:.1f}s.\n"
    "{tail}\n\n"
    "Your final answer was held back because of this. Fix the problem with tools, then answer again. "
    "If these failures already existed before your change and are unrelated to it, say so in your final "
    "answer instead of editing. The check re-runs automatically when you answer again, so do not run "
    "it yourself through run_shell."
)


def _gate_allowed(agent, config):
    return not (
        config is None
        or not config.enabled
        or agent.depth != 0
        or agent.read_only
        or agent.runtime_mode == "plan"
        or agent.approval_policy == "never"
    )


def _planned_commands(config, root, paths):
    if not config.commands:
        return detect_commands(root, paths)
    docs = build_artifact_graph(root, paths)["categories"]["docs"]
    if all(path in docs for path in paths):
        return []
    return [{"command": command, "reason": "配置的 [verify].commands"} for command in config.commands]


def _approved(agent, config, commands):
    if config.mode == "auto" or agent.approval_policy == "auto":
        return True
    return bool(agent.approve("verify", {"commands": [item["command"] for item in commands]}))


def _overall(receipts):
    statuses = [item["status"] for item in receipts]
    if STATUS_FAILED in statuses:
        return STATUS_FAILED
    if STATUS_TIMEOUT in statuses:
        return STATUS_TIMEOUT
    if STATUS_PASSED in statuses:
        return STATUS_PASSED
    return STATUS_UNAVAILABLE if STATUS_UNAVAILABLE in statuses else STATUS_SKIPPED


def verification_gate(engine, task_state, final, allow_repair=True):
    """生成器。返回 True 表示验证失败且还有修复机会，调用方应 continue 回到模型循环。

    allow_repair=False 用于预算耗尽的收尾：只留下回执，不再让模型继续改。
    """
    agent = engine.runtime
    config = getattr(agent, "verify_config", None)
    if not _gate_allowed(agent, config):
        return False
    paths = list(task_state.changed_paths)
    state = task_state.verification
    fingerprint = changes_fingerprint(agent.root, paths) if paths else ""
    if not paths or state.get("declined") or state.get("fingerprint") == fingerprint:
        return False
    commands = _planned_commands(config, agent.root, paths)
    if not commands:
        return False
    if not _approved(agent, config, commands):
        state.update(status="declined", declined=True, fingerprint=fingerprint)
        agent.emit_trace(task_state, "verification_declined", {"commands": [item["command"] for item in commands]})
        return False

    state["rounds"] = int(state.get("rounds", 0)) + 1
    run_receipts = []
    for entry in commands:
        yield {"type": "tool_call", "run_id": task_state.run_id, "name": "verify", "args": {"command": entry["command"]}}
        receipt = run_command(agent, entry["command"], config.timeout)
        receipt["round"] = state["rounds"]
        receipt["reason"] = entry["reason"]
        run_receipts.append(receipt)
        agent.emit_trace(task_state, "verification_run", dict(receipt))
        yield {
            "type": "tool_result",
            "run_id": task_state.run_id,
            "name": "verify",
            "content": _render_receipt(receipt),
            "metadata": {"tool_status": "ok" if receipt["status"] in NON_FAILURE_STATUSES else "error"},
        }
        if receipt["status"] in (STATUS_FAILED, STATUS_TIMEOUT) or agent.abort_requested:
            break
    overall = _overall(run_receipts)
    state["receipts"] = (list(state.get("receipts", [])) + run_receipts)[-MAX_RECEIPTS:]
    state.update(status=overall, fingerprint=fingerprint, repairs=int(state.get("repairs", 0)))
    agent.session_event_bus.emit(
        "verification_finished",
        {"run_id": task_state.run_id, "status": overall, "round": state["rounds"], "commands": [r["command"] for r in run_receipts]},
    )
    if agent.abort_requested or not allow_repair or overall not in (STATUS_FAILED, STATUS_TIMEOUT) or state["repairs"] >= config.max_repair_rounds:
        return False
    state["repairs"] += 1
    failing = run_receipts[-1]
    code = failing["exit_code"] if failing["exit_code"] is not None else f"timeout after {config.timeout}s"
    agent.record(
        {
            "role": "tool",
            "name": "verify",
            "args": {"command": failing["command"]},
            "content": REPAIR_MESSAGE.format(
                left=config.max_repair_rounds - state["repairs"],
                command=failing["command"],
                code=code,
                seconds=failing["duration_ms"] / 1000,
                tail=failing["output_tail"] or "(no output)",
            ),
            "created_at": now(),
        }
    )
    agent.run_store.write_task_state(task_state)
    return True


def _render_receipt(receipt):
    head = f"{receipt['status']} (exit {receipt['exit_code']}, {receipt['duration_ms'] / 1000:.1f}s)"
    return head if receipt["status"] == STATUS_PASSED else f"{head}\n{clip(receipt['output_tail'], 800)}"


def verification_notice(agent, task_state, final):
    """回合结束时给用户的一行回执摘要，以及“声称已验证却没有回执”的警告；没什么可说就返回空串。"""
    config = getattr(agent, "verify_config", None)
    if config is None or not config.enabled:
        return ""
    state = task_state.verification
    lines = []
    status = state.get("status", "")
    last = (state.get("receipts") or [{}])[-1]
    repairs = int(state.get("repairs", 0))
    if status == STATUS_PASSED:
        suffix = f"，经 {repairs} 轮修复" if repairs else ""
        lines.append(f"✓ 已验证：`{last['command']}` 通过（{last['duration_ms'] / 1000:.1f}s{suffix}）")
    elif status in (STATUS_FAILED, STATUS_TIMEOUT):
        reason = "超时" if status == STATUS_TIMEOUT else f"退出码 {last.get('exit_code')}"
        fed_back = f"，失败输出已反馈给模型 {repairs} 轮仍未通过" if repairs else ""
        lines.append(f"✗ 验证未通过：`{last['command']}`（{reason}）{fed_back}，请人工确认。")
    elif status in (STATUS_UNAVAILABLE, STATUS_SKIPPED):
        lines.append(f"⚠ 未能完成验证：`{last['command']}` {'不可用' if status == STATUS_UNAVAILABLE else '没有收集到测试'}。")
    elif status == "declined":
        lines.append("⚠ 本回合的代码改动没有经过验证（你拒绝了验证命令）。")
    if claims_verified(final) and status != STATUS_PASSED:
        if not shell_evidence(current_turn_history(agent.session["history"])):
            state["claim_warning"] = True
            lines.append(
                "⚠ 回答声称测试/验证已通过，但本回合没有任何通过的验证回执"
                + ("，最近一次验证是失败的。" if status in (STATUS_FAILED, STATUS_TIMEOUT) else "。")
            )
    return "\n".join(lines)
