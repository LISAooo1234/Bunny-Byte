"""回合收尾前的 on_final 钩子：模型想交卷时先跑用户的钩子，钩子拦截就把输出塞回去让模型继续。"""

from .hook_runner import STATUS_ABORTED, base_payload
from .workspace import clip, now

FEEDBACK_MESSAGE = (
    "An on_final hook blocked your final answer ({left} more round(s) allowed): `{command}` "
    "{what}.\n{output}\n\n"
    "Fix the problem with tools, then answer again. If it is unrelated to your change, say so in the "
    "final answer instead of editing."
)


def final_hook_gate(engine, task_state, final, allow_block=True):
    """生成器。返回 True 表示钩子拦下了最终回答且还有轮次，调用方应 continue 回到模型循环。

    allow_block=False 用于预算耗尽的收尾：钩子照样运行（通知类钩子仍然要响），但不再让模型继续。
    """
    agent = engine.runtime
    runner = getattr(agent, "hooks", None)
    if runner is None or agent.depth != 0 or not runner.has("on_final"):
        return False
    state = task_state.hooks
    payload = dict(
        base_payload(agent, "on_final"),
        final_message=clip(final, 20000),
        changed_paths=list(task_state.changed_paths),
        verification=task_state.verification.get("status", ""),
        stop_reason=task_state.stop_reason,
    )
    blocker = None
    for spec in runner.specs_for("on_final"):
        if spec.only_if_changed and not task_state.changed_paths:
            continue
        yield {"type": "tool_call", "run_id": task_state.run_id, "name": "hook", "args": {"command": spec.command}}
        outcome = runner.run_one(agent, spec, payload)
        yield {
            "type": "tool_result",
            "run_id": task_state.run_id,
            "name": "hook",
            "content": _render(outcome),
            "metadata": {"tool_status": "error" if outcome.blocks or outcome.failed else "ok"},
            "duration_ms": outcome.duration_ms,
        }
        if agent.abort_requested or outcome.status == STATUS_ABORTED:
            return False
        if outcome.blocks:
            blocker = outcome
            break
    if blocker is None:
        return False
    rounds = int(state.get("final_rounds", 0))
    limit = runner.hookset.max_final_rounds
    if not allow_block or rounds >= limit:
        state["final_blocked"] = True
        reason = "已达反馈轮次上限" if allow_block else "本回合已因预算耗尽收尾"
        yield {
            "type": "runtime_notice",
            "run_id": task_state.run_id,
            "content": f"⚠ on_final 钩子 `{blocker.spec.command}` 仍然拦截了最终回答（{reason}），请人工确认：{clip(blocker.output, 300)}",
        }
        return False
    state["final_rounds"] = rounds + 1
    agent.record(
        {
            "role": "tool",
            "name": "hook",
            "args": {"command": blocker.spec.command},
            "content": FEEDBACK_MESSAGE.format(
                left=limit - rounds - 1,
                command=blocker.spec.command,
                what=blocker.describe() if blocker.status != "block" else "asked for changes (exit code 2)",
                output=blocker.output or "(no output)",
            ),
            "created_at": now(),
        }
    )
    agent.run_store.write_task_state(task_state)
    return True


def _render(outcome):
    seconds = outcome.duration_ms / 1000
    if outcome.status == "ok":
        head = f"✓ on_final 钩子通过 ({seconds:.1f}s)"
    elif outcome.blocks:
        head = f"✗ on_final 钩子拦截了最终回答（{outcome.describe(zh=True)}，{seconds:.1f}s）"
    else:
        head = f"⚠ on_final 钩子出错（{outcome.describe(zh=True)}，{seconds:.1f}s），已放行"
    return head + (f"\n{outcome.output}" if outcome.output else "")
