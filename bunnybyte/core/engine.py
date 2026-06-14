"""Turn-level runtime engine.

The runtime owns state and persistence. Engine owns the control loop that turns
one user request into model calls, tool executions, and user-visible events.
"""

import re
import time

from .model_stream import complete_model_with_deltas
from ..providers.base import ModelToolCall
from .model_errors import finish_model_error
from .engine_helpers import (
    execute_tool_payloads,
    finish_limited_run,
    finish_stopped_run,
    maintain_memory_safely,
    request_step_limit_summary,
    should_retry_model_error,
)
from .task_state import TaskState
from .workspace import clip, now

CHECKPOINT_NONE_STATUS = "no-checkpoint"
CHECKPOINT_PARTIAL_STALE_STATUS = "partial-stale"
CHECKPOINT_WORKSPACE_MISMATCH_STATUS = "workspace-mismatch"
NON_TERMINAL_FINAL_CUE_PATTERN = re.compile(
    r"(?:^|[\n。！？])\s*(?:"
    r"(?:我(?:接下来|随后|之后|然后|马上)?(?:会|将|准备|打算|先)|"
    r"接下来我(?:会|将|准备|打算)|"
    r"下一步我(?:会|将|准备|打算)|"
    r"继续(?:做|执行|读取|查看|检查)|"
    r"(?:now|next)\s+i\s+(?:will|am going to)|"
    r"i(?:'ll|\s+will|\s+am going to))"
    r")",
    re.IGNORECASE,
)


def _with_protocol_retry_notice(user_message, notice, native_tools=False):
    notice = str(notice or "").strip()
    if not notice:
        return user_message
    if native_tools:
        return (
            f"{user_message}\n\n"
            "Protocol correction for your immediately previous response in this turn:\n"
            f"{notice}\n"
            "Continue the original user request. Use native tool calls when action is needed, "
            "or return a normal final assistant answer."
        )
    return (
        f"{user_message}\n\n"
        "Protocol correction for your immediately previous response in this turn:\n"
        f"{notice}\n"
        "Continue the original user request. Return exactly one valid "
        "<tool>...</tool> call or one <final>...</final> answer."
    )


def _tool_call_payload(call):
    if isinstance(call, ModelToolCall):
        return {"name": call.name, "args": dict(call.args or {})}
    if isinstance(call, dict):
        return {"name": call.get("name", ""), "args": dict(call.get("args") or {})}
    return {"name": getattr(call, "name", ""), "args": dict(getattr(call, "args", {}) or {})}


def _merge_provider_usage_into_context_usage(prompt_metadata, completion_metadata):
    context_usage = dict(prompt_metadata.get("context_usage") or {})
    if not context_usage:
        return
    provider_input = completion_metadata.get("input_tokens")
    provider_output = completion_metadata.get("output_tokens")
    provider_total = completion_metadata.get("total_tokens")
    if provider_input is None and provider_total is None:
        return
    context_usage.update(
        {
            "provider_input_tokens": provider_input,
            "provider_output_tokens": provider_output,
            "provider_total_tokens": provider_total,
            "display_tokens": provider_input if provider_input is not None else provider_total,
            "display_source": "provider",
        }
    )
    prompt_metadata["context_usage"] = context_usage


class Engine:
    def __init__(self, runtime):
        self.runtime = runtime

    def ask(self, user_message):
        final_answer = ""
        for event in self.run_turn(user_message):
            if event["type"] in {"final", "stop"}:
                final_answer = event["content"]
        return final_answer

    def drain_worker_notifications(self):
        agent = self.runtime
        notifications = agent.worker_manager.drain_notifications()
        for notification in notifications:
            agent.record({"role": "user", "content": notification, "created_at": now()})
            agent.session_event_bus.emit(
                "worker_notification_drained",
                {
                    "run_id": getattr(agent, "current_run_id", ""),
                    "content": clip(notification, 500),
                },
            )
        return notifications

    def _drain_worker_notification_events(self):
        for notification in self.drain_worker_notifications():
            yield {
                "type": "worker_notification",
                "run_id": getattr(self.runtime, "current_run_id", ""),
                "content": notification,
            }

    def run_turn(self, user_message):
        agent = self.runtime
        agent.ensure_session_started()
        run_started_at = time.monotonic()
        task_state = TaskState.create(
            run_id=agent.new_run_id(),
            task_id=agent.new_task_id(),
            user_request=user_message,
        )
        task_state.resume_status = agent.resume_state.get(
            "status", CHECKPOINT_NONE_STATUS
        )
        agent.current_task_state = task_state
        agent.current_turn_id = task_state.task_id
        agent.current_run_id = task_state.run_id
        agent.current_run_dir = agent.run_store.start_run(task_state)
        agent.session_event_bus.emit(
            "turn_started",
            {
                "run_id": task_state.run_id,
                "task_id": task_state.task_id,
                "runtime_mode": agent.runtime_mode,
            },
        )
        yield {
            "type": "turn_started",
            "run_id": task_state.run_id,
            "task_id": task_state.task_id,
        }

        agent.memory.set_task_summary(user_message)
        agent.record({"role": "user", "content": user_message, "created_at": now()})
        agent.session_event_bus.emit(
            "user_message",
            {"run_id": task_state.run_id, "content": clip(user_message, 300)},
        )
        agent.emit_trace(
            task_state,
            "run_started",
            {
                "task_id": task_state.task_id,
                "user_request": clip(user_message, 300),
            },
        )

        tool_steps = 0
        attempts = 0
        provider_retries = {}
        protocol_retry_notice = ""
        # 不放大 attempts，避免出现"看不见的隐形重试"——失败必须被用户察觉。
        max_attempts = agent.max_steps + 2

        while tool_steps < agent.max_steps and attempts < max_attempts:
            if agent.abort_requested:
                yield from finish_stopped_run(
                    self,
                    task_state,
                    user_message,
                    "Stopped after abort request.",
                    "aborted",
                    run_started_at,
                )
                return
            yield from self._drain_worker_notification_events()
            attempts += 1
            task_state.record_attempt()
            agent.run_store.write_task_state(task_state)
            prompt_started_at = time.monotonic()
            native_tools = agent.native_tools() if hasattr(agent, "native_tools") else None
            prompt_user_message = _with_protocol_retry_notice(
                user_message, protocol_retry_notice, native_tools=bool(native_tools)
            )
            prompt, prompt_metadata = agent._build_prompt_and_metadata(
                prompt_user_message
            )
            agent.emit_trace(
                task_state,
                "prompt_built",
                {
                    "prompt_metadata": prompt_metadata,
                    "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
                },
            )
            if prompt_metadata.get("resume_status") == CHECKPOINT_PARTIAL_STALE_STATUS:
                checkpoint = agent.create_checkpoint(
                    task_state, user_message, trigger="freshness_mismatch"
                )
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "freshness_mismatch",
                    },
                )
            elif (
                prompt_metadata.get("resume_status")
                == CHECKPOINT_WORKSPACE_MISMATCH_STATUS
            ):
                agent.emit_trace(
                    task_state,
                    "runtime_identity_mismatch",
                    {
                        "fields": list(
                            prompt_metadata.get("runtime_identity_mismatch_fields", [])
                        ),
                    },
                )
                checkpoint = agent.create_checkpoint(
                    task_state, user_message, trigger="workspace_mismatch"
                )
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "workspace_mismatch",
                    },
                )
            if prompt_metadata.get("budget_reductions"):
                checkpoint = agent.create_checkpoint(
                    task_state, user_message, trigger="context_reduction"
                )
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "context_reduction",
                    },
                )
            agent.emit_trace(
                task_state,
                "model_requested",
                {
                    "attempts": task_state.attempts,
                    "tool_steps": task_state.tool_steps,
                    "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
                },
            )
            agent.session_event_bus.emit(
                "model_requested",
                {
                    "run_id": task_state.run_id,
                    "attempts": task_state.attempts,
                    "tool_steps": task_state.tool_steps,
                },
            )
            yield {
                "type": "model_requested",
                "run_id": task_state.run_id,
                "attempts": task_state.attempts,
                "tool_steps": task_state.tool_steps,
            }

            prompt_cache_key = None
            prompt_cache_retention = None
            if getattr(agent.model_client, "supports_prompt_cache", False):
                prompt_cache_key = prompt_metadata.get("prompt_cache_key")
                prompt_cache_retention = "in_memory"

            model_started_at = time.monotonic()
            try:
                result = yield from complete_model_with_deltas(
                    self,
                    task_state,
                    prompt,
                    prompt_cache_key=prompt_cache_key,
                    prompt_cache_retention=prompt_cache_retention,
                    tools=native_tools,
                )
            except Exception as exc:
                if agent.abort_requested:
                    yield from finish_stopped_run(
                        self,
                        task_state,
                        user_message,
                        "Stopped after abort request.",
                        "aborted",
                        run_started_at,
                    )
                    return
                if should_retry_model_error(exc, provider_retries):
                    code = getattr(exc, "code", type(exc).__name__)
                    provider_retries[code] = provider_retries.get(code, 0) + 1
                    agent.session_event_bus.emit(
                        "model_retry_scheduled",
                        {
                            "run_id": task_state.run_id,
                            "code": code,
                            "attempts": task_state.attempts,
                            "retry_count": provider_retries[code],
                        },
                    )
                    agent.emit_trace(
                        task_state,
                        "model_retry_scheduled",
                        {
                            "code": code,
                            "duration_ms": int(
                                (time.monotonic() - model_started_at) * 1000
                            ),
                            "retry_count": provider_retries[code],
                        },
                    )
                    continue
                yield from finish_model_error(
                    self,
                    task_state,
                    user_message,
                    prompt_metadata,
                    exc,
                    int((time.monotonic() - model_started_at) * 1000),
                    int((time.monotonic() - run_started_at) * 1000),
                )
                return
            if agent.abort_requested:
                yield from finish_stopped_run(
                    self,
                    task_state,
                    user_message,
                    "Stopped after abort request.",
                    "aborted",
                    run_started_at,
                )
                return
            raw = result.text
            result_tool_calls = list(getattr(result, "tool_calls", []) or [])
            completion_metadata = dict(
                result.metadata
                or getattr(agent.model_client, "last_completion_metadata", {})
                or {}
            )
            metadata_tool_calls = list(completion_metadata.get("tool_calls", []) or [])
            if not result_tool_calls and metadata_tool_calls:
                result_tool_calls = metadata_tool_calls
            trace_completion_metadata = dict(completion_metadata)
            if trace_completion_metadata.get("tool_calls"):
                trace_completion_metadata["tool_calls"] = [
                    {"name": _tool_call_payload(call).get("name", ""), "args": _tool_call_payload(call).get("args", {})}
                    for call in trace_completion_metadata["tool_calls"]
                ]
            if trace_completion_metadata:
                prompt_metadata.update(trace_completion_metadata)
                _merge_provider_usage_into_context_usage(prompt_metadata, trace_completion_metadata)
            agent.last_completion_metadata = trace_completion_metadata
            agent.last_prompt_metadata = prompt_metadata
            native_tool_calls = result_tool_calls
            if native_tool_calls:
                kind = "tool" if len(native_tool_calls) == 1 else "tools"
                payload = (
                    _tool_call_payload(native_tool_calls[0])
                    if kind == "tool"
                    else [_tool_call_payload(call) for call in native_tool_calls]
                )
                parse_metadata = {"native_tool_calls": len(native_tool_calls)}
            elif native_tools:
                raw_text = str(raw or "").strip()
                kind, payload, parse_metadata = (
                    ("final", raw_text, {})
                    if raw_text
                    else (
                        "retry",
                        "Return a normal final answer or request a native tool call.",
                        {},
                    )
                )
            else:
                kind, payload, parse_metadata = agent.parse_with_metadata(
                    raw,
                    allow_truncated_json_tool=True,
                )
            preamble = str(parse_metadata.get("preamble", "") or "").strip()
            if preamble and kind in {"tool", "tools"}:
                agent.record(
                    {"role": "assistant", "content": preamble, "created_at": now()}
                )
                agent.session_event_bus.emit(
                    "assistant_message",
                    {
                        "run_id": task_state.run_id,
                        "kind": "preamble",
                        "content": clip(preamble, 500),
                    },
                )
                yield {
                    "type": "assistant_preamble",
                    "run_id": task_state.run_id,
                    "content": preamble,
                }
            duration_ms = int((time.monotonic() - model_started_at) * 1000)
            agent.emit_trace(
                task_state,
                "model_parsed",
                {
                    "kind": kind,
                    "completion_metadata": trace_completion_metadata,
                    "duration_ms": duration_ms,
                },
            )
            agent.session_event_bus.emit(
                "model_parsed",
                {"run_id": task_state.run_id, "kind": kind, "duration_ms": duration_ms},
            )
            yield {
                "type": "model_parsed",
                "run_id": task_state.run_id,
                "kind": kind,
                "duration_ms": duration_ms,
            }
            if kind != "retry":
                protocol_retry_notice = ""

            if kind in {"tool", "tools"}:
                tools = [payload] if kind == "tool" else list(payload)
                executed = yield from execute_tool_payloads(
                    self,
                    task_state,
                    user_message,
                    tools,
                    agent.max_steps - tool_steps,
                )
                tool_steps += executed
                if agent.abort_requested:
                    yield from finish_stopped_run(
                        self,
                        task_state,
                        user_message,
                        "Stopped after abort request.",
                        "aborted",
                        run_started_at,
                    )
                    return
                continue

            if kind == "retry":
                protocol_retry_notice = payload
                agent.session_event_bus.emit(
                    "assistant_message",
                    {
                        "run_id": task_state.run_id,
                        "kind": "retry",
                        "content": clip(payload, 500),
                    },
                )
                agent.run_store.write_task_state(task_state)
                yield {"type": "retry", "run_id": task_state.run_id, "content": payload}
                continue

            final = (payload or raw).strip()
            yield from self._drain_worker_notification_events()
            if self._looks_like_premature_final(final, task_state):
                notice = self._premature_final_notice(final)
                agent.record(
                    {"role": "assistant", "content": notice, "created_at": now()}
                )
                agent.session_event_bus.emit(
                    "assistant_message",
                    {
                        "run_id": task_state.run_id,
                        "kind": "retry",
                        "content": clip(notice, 500),
                    },
                )
                agent.run_store.write_task_state(task_state)
                yield {"type": "retry", "run_id": task_state.run_id, "content": notice}
                continue
            if agent.runtime_mode == "plan" and not agent.plan_mode.can_finish():
                notice = agent.plan_mode.final_notice()
                agent.record(
                    {"role": "assistant", "content": notice, "created_at": now()}
                )
                agent.session_event_bus.emit(
                    "assistant_message",
                    {
                        "run_id": task_state.run_id,
                        "kind": "runtime_notice",
                        "content": notice,
                    },
                )
                agent.run_store.write_task_state(task_state)
                yield {
                    "type": "runtime_notice",
                    "run_id": task_state.run_id,
                    "content": notice,
                }
                continue

            agent.record({"role": "assistant", "content": final, "created_at": now()})
            final_event_id = str((agent.session.get("history", []) or [{}])[-1].get("event_id", ""))
            if agent.runtime_mode == "plan":
                agent.exit_plan_mode()
            agent.session_event_bus.emit(
                "assistant_message",
                {
                    "run_id": task_state.run_id,
                    "kind": "final",
                    "content": clip(final, 500),
                },
            )
            task_state.finish_success(final)
            agent.promote_durable_memory(user_message, final)
            maintain_memory_safely(agent, task_state, final)
            checkpoint = agent.create_checkpoint(
                task_state, user_message, trigger="run_finished"
            )
            agent.run_store.write_task_state(task_state)
            agent.emit_trace(
                task_state,
                "checkpoint_created",
                {
                    "checkpoint_id": checkpoint["checkpoint_id"],
                    "trigger": "run_finished",
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
            yield from self._drain_worker_notification_events()
            agent.current_turn_id = ""
            agent.current_run_id = ""
            yield {"type": "final", "run_id": task_state.run_id, "content": final, "event_id": final_event_id}
            yield {
                "type": "turn_finished",
                "run_id": task_state.run_id,
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
            }
            return

        if attempts >= max_attempts and tool_steps < agent.max_steps:
            final = "Stopped after too many malformed model responses without a valid tool call or final answer."
            task_state.stop_retry_limit(final)
        else:
            summary = None
            if tool_steps > 0:
                summary = request_step_limit_summary(self, task_state, user_message)
            if summary:
                final = (
                    summary
                    + "\n\n— 已达本轮 step 预算上限（max_steps）。以上是当前进展总结。"
                    "继续工作：在 REPL 输入 /resume 续接本会话，或直接说"
                    "「继续」让我接着干。"
                )
            else:
                final = "Stopped after reaching the step limit without a final answer."
            task_state.stop_step_limit(final)
        yield from finish_limited_run(
            self, task_state, user_message, final, run_started_at
        )

    @staticmethod
    def _looks_like_premature_final(final, task_state):
        text = str(final or "").strip()
        if not text or task_state.tool_steps > 0:
            return False
        return bool(NON_TERMINAL_FINAL_CUE_PATTERN.search(text))

    @staticmethod
    def _premature_final_notice(final):
        return (
            "Your previous assistant answer said you were about to do more work, "
            "but a final answer ends the turn. If work remains, call the appropriate "
            "tool now instead of narrating the plan. Previous answer: "
            f"{clip(final, 500)}"
        )
