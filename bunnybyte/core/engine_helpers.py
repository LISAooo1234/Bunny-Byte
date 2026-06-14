"""Helper routines for Engine control-loop side effects."""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from ..providers.base import complete_model
from ..providers.errors import ProviderError
from .workspace import clip, now

MAX_PARALLEL_TOOLS = 4


@dataclass(frozen=True)
class ToolExecutionRecord:
    name: str
    args: dict
    result: str
    metadata: dict
    duration_ms: int


def _execute_tool(agent, name, args, started_at):
    result, metadata = agent.run_tool_with_metadata(name, args)
    return ToolExecutionRecord(
        name=name,
        args=args,
        result=result,
        metadata=metadata,
        duration_ms=int((time.monotonic() - started_at) * 1000),
    )


def _commit_tool_record(engine, task_state, user_message, record):
    agent = engine.runtime
    agent.session_event_bus.emit(
        "tool_finished",
        {
            "run_id": task_state.run_id,
            "tool_name": record.name,
            "status": record.metadata.get("tool_status", ""),
            "tool_error_code": record.metadata.get("tool_error_code", ""),
            "workspace_changed": bool(record.metadata.get("workspace_changed", False)),
            "affected_paths": list(record.metadata.get("affected_paths", [])),
            "duration_ms": record.duration_ms,
        },
    )
    agent.record(
        {
            "role": "tool",
            "name": record.name,
            "args": record.args,
            "content": record.result,
            "created_at": now(),
        }
    )
    for notification in engine.drain_worker_notifications():
        yield {
            "type": "worker_notification",
            "run_id": getattr(agent, "current_run_id", ""),
            "content": notification,
        }
    agent.run_store.write_task_state(task_state)
    agent.emit_trace(
        task_state,
        "tool_executed",
        {
            "name": record.name,
            "args": record.args,
            "result": clip(record.result, 500),
            "duration_ms": record.duration_ms,
            **record.metadata,
        },
    )
    if record.metadata.get("workspace_changed") or not record.metadata.get("read_only", True):
        checkpoint = agent.create_checkpoint(
            task_state, user_message, trigger="tool_executed"
        )
        agent.run_store.write_task_state(task_state)
        agent.emit_trace(
            task_state,
            "checkpoint_created",
            {"checkpoint_id": checkpoint["checkpoint_id"], "trigger": "tool_executed"},
        )
    else:
        agent.run_store.write_task_state(task_state)
        agent.emit_trace(
            task_state,
            "checkpoint_skipped",
            {"trigger": "read_only_tool", "tool_name": record.name},
        )
    yield {
        "type": "tool_result",
        "run_id": task_state.run_id,
        "name": record.name,
        "content": record.result,
        "metadata": record.metadata,
    }


def execute_tool_payload(engine, task_state, user_message, payload):
    agent = engine.runtime
    name = payload.get("name", "")
    args = payload.get("args", {})
    task_state.record_tool(name)
    tool_started_at = time.monotonic()
    agent.session_event_bus.emit(
        "tool_started", {"run_id": task_state.run_id, "tool_name": name, "args": args}
    )
    yield {"type": "tool_call", "run_id": task_state.run_id, "name": name, "args": args}

    record = _execute_tool(agent, name, args, tool_started_at)
    yield from _commit_tool_record(engine, task_state, user_message, record)


def _tool_payload_name_args(payload):
    return payload.get("name", ""), payload.get("args", {})


def is_parallel_safe_tool(agent, payload):
    name, _args = _tool_payload_name_args(payload)
    tool = agent.available_tools().get(name)
    return bool(tool and not tool.risky and getattr(tool, "parallel_safe", False))


def tool_payload_batches(agent, payloads):
    batches = []
    current = []
    for payload in payloads:
        if is_parallel_safe_tool(agent, payload):
            current.append(payload)
            if len(current) >= MAX_PARALLEL_TOOLS:
                batches.append(current)
                current = []
            continue
        if current:
            batches.append(current)
            current = []
        batches.append([payload])
    if current:
        batches.append(current)
    return batches


def execute_tool_payload_batch(engine, task_state, user_message, payloads):
    payloads = list(payloads)
    if len(payloads) <= 1:
        if payloads:
            yield from execute_tool_payload(engine, task_state, user_message, payloads[0])
        return
    agent = engine.runtime
    started = []
    for payload in payloads:
        name, args = _tool_payload_name_args(payload)
        task_state.record_tool(name)
        started_at = time.monotonic()
        started.append((payload, name, args, started_at))
        agent.session_event_bus.emit(
            "tool_started", {"run_id": task_state.run_id, "tool_name": name, "args": args}
        )
        yield {"type": "tool_call", "run_id": task_state.run_id, "name": name, "args": args}

    records = [None] * len(started)
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_TOOLS, len(started))) as executor:
        futures = {
            executor.submit(_execute_tool, agent, name, args, started_at): index
            for index, (_payload, name, args, started_at) in enumerate(started)
        }
        for future in as_completed(futures):
            index = futures[future]
            records[index] = future.result()

    for record in records:
        yield from _commit_tool_record(engine, task_state, user_message, record)


def execute_tool_payloads(engine, task_state, user_message, payloads, remaining_steps):
    agent = engine.runtime
    executed = 0
    pending = list(payloads)[: max(0, int(remaining_steps))]
    for batch in tool_payload_batches(agent, pending):
        if agent.abort_requested:
            break
        yield from execute_tool_payload_batch(engine, task_state, user_message, batch)
        executed += len(batch)
        if agent.abort_requested:
            break
    return executed


def finish_stopped_run(
    engine, task_state, user_message, final, stop_reason, run_started_at
):
    agent = engine.runtime
    task_state.stop(stop_reason, final_answer=final)
    agent.abort_requested = False
    agent.record({"role": "assistant", "content": final, "created_at": now()})
    agent.session_event_bus.emit(
        "assistant_message",
        {"run_id": task_state.run_id, "kind": "stop", "content": clip(final, 500)},
    )
    agent.run_store.write_task_state(task_state)
    checkpoint = agent.create_checkpoint(task_state, user_message, trigger=stop_reason)
    agent.emit_trace(
        task_state,
        "checkpoint_created",
        {"checkpoint_id": checkpoint["checkpoint_id"], "trigger": stop_reason},
    )
    agent.emit_trace(
        task_state,
        "run_finished",
        {
            "status": task_state.status,
            "stop_reason": task_state.stop_reason,
            "final_answer": final,
            "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
        },
    )
    agent.session_event_bus.emit(
        "turn_finished",
        {
            "run_id": task_state.run_id,
            "status": task_state.status,
            "stop_reason": task_state.stop_reason,
            "duration_ms": int((time.monotonic() - run_started_at) * 1000),
        },
    )
    agent.run_store.write_report(
        task_state, agent.redact_artifact(agent.build_report(task_state))
    )
    agent.current_turn_id = ""
    agent.current_run_id = ""
    yield {"type": "stop", "run_id": task_state.run_id, "content": final}
    yield {
        "type": "turn_finished",
        "run_id": task_state.run_id,
        "status": task_state.status,
        "stop_reason": task_state.stop_reason,
    }


def finish_limited_run(engine, task_state, user_message, final, run_started_at):
    agent = engine.runtime
    agent.record({"role": "assistant", "content": final, "created_at": now()})
    agent.session_event_bus.emit(
        "assistant_message",
        {"run_id": task_state.run_id, "kind": "stop", "content": clip(final, 500)},
    )
    agent.promote_durable_memory(user_message, final)
    maintain_memory_safely(agent, task_state, final)
    agent.run_store.write_task_state(task_state)
    checkpoint = agent.create_checkpoint(
        task_state, user_message, trigger=task_state.stop_reason or "run_stopped"
    )
    agent.emit_trace(
        task_state,
        "checkpoint_created",
        {
            "checkpoint_id": checkpoint["checkpoint_id"],
            "trigger": task_state.stop_reason or "run_stopped",
        },
    )
    agent.emit_trace(
        task_state,
        "run_finished",
        {
            "status": task_state.status,
            "stop_reason": task_state.stop_reason,
            "final_answer": final,
            "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
        },
    )
    agent.session_event_bus.emit(
        "turn_finished",
        {
            "run_id": task_state.run_id,
            "status": task_state.status,
            "stop_reason": task_state.stop_reason,
            "duration_ms": int((time.monotonic() - run_started_at) * 1000),
        },
    )
    agent.run_store.write_report(
        task_state, agent.redact_artifact(agent.build_report(task_state))
    )
    agent.current_turn_id = ""
    agent.current_run_id = ""
    yield {"type": "stop", "run_id": task_state.run_id, "content": final}
    yield {
        "type": "turn_finished",
        "run_id": task_state.run_id,
        "status": task_state.status,
        "stop_reason": task_state.stop_reason,
    }


def should_retry_model_error(exc, provider_retries):
    if not isinstance(exc, ProviderError):
        return False
    code = str(getattr(exc, "code", "") or "")
    if code not in {"empty_response"}:
        return False
    return provider_retries.get(code, 0) < 1


def maintain_memory_safely(agent, task_state, final_answer):
    try:
        agent.maintain_memory_after_turn(final_answer)
    except Exception as exc:
        audit = getattr(agent, "last_memory_maintenance", {"errors": []})
        errors = audit.setdefault("errors", [])
        errors.append(str(exc))
        agent.last_memory_maintenance = audit
        agent.session_event_bus.emit(
            "memory_maintenance_failed",
            {"run_id": task_state.run_id, "error": clip(str(exc), 300)},
        )
        agent.emit_trace(
            task_state, "memory_maintenance_failed", {"error": clip(str(exc), 300)}
        )


_STEP_LIMIT_SUMMARY_NOTICE = (
    "You have hit the per-turn tool budget (max_steps). Do not call any more tools. "
    "Right now, return a concise answer in the user's language that briefly covers: "
    "(1) what you accomplished this turn, (2) what remains undone, "
    "(3) how the user can continue (e.g., `/resume` then `继续`)."
)


def request_step_limit_summary(engine, task_state, user_message):
    """Ask the model to write a graceful step-limit summary.

    Returns the final text, or None if the model fails or refuses to comply.
    Side effects: emits a trace event but does NOT mutate session history —
    the caller decides whether to record the resulting final.
    """
    agent = engine.runtime
    started_at = time.monotonic()
    try:
        prompt, _ = agent._build_prompt_and_metadata(_STEP_LIMIT_SUMMARY_NOTICE)
        result = complete_model(
            agent.model_client, prompt, agent.max_new_tokens
        )
    except Exception as exc:
        agent.emit_trace(
            task_state,
            "step_limit_summary_failed",
            {"error": clip(str(exc), 200)},
        )
        return None
    raw = (result.text or "").strip() if result else ""
    if getattr(agent.model_client, "supports_native_tools", False):
        kind, payload = ("final", raw) if raw else ("retry", "")
    else:
        kind, payload = agent.parse(raw)
    duration_ms = int((time.monotonic() - started_at) * 1000)
    agent.emit_trace(
        task_state,
        "step_limit_summary",
        {"kind": kind, "duration_ms": duration_ms, "produced": bool(kind == "final")},
    )
    if kind == "final" and payload:
        return str(payload).strip()
    return None
