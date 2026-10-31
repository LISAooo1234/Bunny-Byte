"""把修复证明接进验证闸门：验证通过后跑一次，结果写进回执、卡片和回合末尾的提示；repair 模式下“测试没测到改动”退回给模型一次。"""

from .fix_proof import PROVEN, SILENT_SKIPS, SKIPPED, THEATER, WEAK, plan_fix_proof, prove_fix, skip_text, skipped
from .shadow_snapshots import ShadowStore
from .turn_diff import current_turn_entry
from .workspace import now

MAX_REPAIRS = 1

REPAIR_MESSAGE = (
    "Fix proof: the runtime reverted your source changes ({sources}) in a scratch copy and re-ran the tests "
    "you changed ({tests}). They still PASS on the old code, so they do not exercise this change{detail}.\n"
    "Add or adjust a test that fails on the old code and passes on yours, then answer again. If this change "
    "is not meant to alter behaviour (a refactor, logging, performance), say so in your final answer instead."
)


def _names(paths, limit=3):
    paths = list(paths)
    return "、".join(paths[:limit]) + (f" 等 {len(paths)} 个" if len(paths) > limit else "")


def render_fix_proof(result):
    """卡片正文。"""
    status = result.get("status")
    tests, sources = _names(result.get("tests", [])), _names(result.get("reverted", []))
    if status == PROVEN:
        failed = result.get("failed_tests") or []
        which = f"（{_names(failed, 2)}）" if failed else ""
        return f"已证明：撤回 {sources} 的改动后，{tests} 失败{which}；用本轮代码则通过"
    if status == THEATER:
        lines = [f"撤回 {sources} 的改动后，{tests} 仍然通过：这些测试没有测到本轮改动"]
        imported = result.get("imported")
        if imported is not None and not imported:
            lines.append("改过的测试根本没有导入改过的源码")
        if result.get("assertions_removed_only"):
            lines.append("本轮对测试的改动只删除了断言")
        return "\n".join(lines)
    if status == WEAK:
        return f"撤回 {sources} 后 {tests} 导入失败：测试依赖本轮新增的接口，行为本身测没测到无法判断（新功能常见）"
    text = f"未能证明：{skip_text(result.get('reason', ''))}"
    tail = result.get("output_tail")
    return f"{text}\n{tail}" if tail else text


def fix_proof_notice(state):
    """回合末尾的一行；只有证明 / 没测到 / 只能弱证明三种情况才说话。"""
    result = (state or {}).get("fix_proof") or {}
    status, count = result.get("status"), len(result.get("tests") or [])
    if status == PROVEN:
        return f"✓ 修复已证明：撤回源码改动后，{count} 个改过的测试文件会失败"
    if status == THEATER:
        return "⚠ 修复未证明：撤回源码改动后，改过的测试仍然通过，它们没有测到这次改动"
    if status == WEAK:
        return "· 修复证明较弱：撤回源码后测试导入失败（依赖新增接口），行为是否被测到无法判断"
    return ""


def _sandboxed(agent):
    config = getattr(agent, "sandbox_config", None)
    return config is not None and config.enabled  # 副本里的测试不经过沙箱，宁可不跑


def _plan(agent):
    if _sandboxed(agent):
        return None, skipped("sandbox")
    entry = current_turn_entry(agent)
    store = ShadowStore.for_root(agent.root)
    if entry is None or not store.available:
        return store, skipped("no_snapshot")
    # 回合里最后一次工具快照：验证命令自己写出的 coverage.xml 之类不算本轮改动
    after = entry.get("after_tree") or store.snapshot()
    try:
        return store, plan_fix_proof(store, entry.get("before_tree"), after)
    except Exception as exc:  # 只是附加证据：任何意外都不能让用户这一轮失败
        return store, skipped("error", detail=f"{type(exc).__name__}: {exc}")


def fix_proof_gate(engine, task_state, allow_repair=True):
    """生成器，验证通过后由 verification_gate 调用。返回 True 表示退回给模型修一轮。"""
    agent = engine.runtime
    config = agent.verify_config
    if config.fix_proof == "off" or agent.abort_requested:
        return False
    state = task_state.verification
    store, plan = _plan(agent)
    if plan.get("status") == SKIPPED:
        state["fix_proof"] = plan
        if plan.get("reason") not in SILENT_SKIPS:
            yield {"type": "tool_call", "run_id": task_state.run_id, "name": "fix_proof", "args": {}}
            yield {"type": "tool_result", "run_id": task_state.run_id, "name": "fix_proof", "content": render_fix_proof(plan), "metadata": {"tool_status": "ok"}, "duration_ms": 0}
        return False
    yield {"type": "tool_call", "run_id": task_state.run_id, "name": "fix_proof", "args": {"tests": plan["tests"]}}
    try:
        result = prove_fix(
            store,
            agent.root,
            plan,
            env=agent.shell_env(),
            timeout=config.timeout,
            should_stop=lambda: bool(agent.abort_requested),
        )
    except Exception as exc:
        result = skipped("error", detail=f"{type(exc).__name__}: {exc}")
    if result.get("output_tail"):
        result["output_tail"] = agent.redact_text(result["output_tail"])
    state["fix_proof"] = result
    agent.emit_trace(task_state, "fix_proof", dict(result))
    content = "已停止" if result.get("reason") == "aborted" else render_fix_proof(result)
    seconds = float(result.get("control_seconds") or 0) + float(result.get("reverted_seconds") or 0)
    yield {
        "type": "tool_result",
        "run_id": task_state.run_id,
        "name": "fix_proof",
        "content": content,
        "metadata": {"tool_status": "error" if result.get("status") == THEATER else "ok"},
        "duration_ms": int(seconds * 1000),
    }
    repairs = int(state.get("fix_proof_repairs", 0))
    if config.fix_proof != "repair" or result.get("status") != THEATER or not allow_repair or agent.abort_requested:
        return False
    if repairs >= MAX_REPAIRS:
        return False
    state["fix_proof_repairs"] = repairs + 1
    detail = " (the changed tests never import the changed sources)" if result.get("imported") == [] else ""
    agent.record(
        {
            "role": "tool",
            "name": "fix_proof",
            "args": {"tests": result.get("tests", [])},
            "content": REPAIR_MESSAGE.format(
                sources=", ".join(result.get("reverted", [])[:5]),
                tests=", ".join(result.get("tests", [])[:5]),
                detail=detail,
            ),
            "created_at": now(),
        }
    )
    agent.run_store.write_task_state(task_state)
    return True


def fix_proof_summary(state):
    """给 `bunny -p` JSON 用的一小段。"""
    result = (state or {}).get("fix_proof") or {}
    if not result:
        return {}
    return {
        "status": result.get("status", ""),
        "reason": result.get("reason", ""),
        "tests": list(result.get("tests") or []),
        "reverted": list(result.get("reverted") or []),
        "repairs": int((state or {}).get("fix_proof_repairs", 0)),
    }
