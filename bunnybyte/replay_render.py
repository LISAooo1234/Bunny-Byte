"""bunny replay 的时间线渲染：把一次运行的 trace.jsonl + report.json 变成按时间排列的纯文本。

只认识白名单里的事件，其余（检查点、记忆整理等）合并成一行“其他 N 个事件”；trace 里的内容
写入时已经过密钥遮蔽，这里不再处理。
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from .core.provider_failover import failover_notice
from .tui.tool_cards import card_args_summary, clip_cells, format_duration, summarize_tool_output, tool_title

STATUS_TEXT = {
    "completed": "已完成",
    "stopped": "已停止",
    "failed": "失败",
    "running": "未正常结束（进程可能被中断）",
}
MAX_EVENT_LINES = 400
MAX_FINAL_LINES = 60
MODEL_EVENTS = {"model_parsed", "step_limit_summary", "stall_summary"}
QUIET_EVENTS = {"run_started", "prompt_built", "model_requested", "run_finished"}


def _parse_time(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def format_tokens(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "?"
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 1000:
        return f"{number / 1000:.1f}k"
    return str(number)


def _tool_counts(events):
    """每次模型调用之后跟着执行了几个工具（文本协议下 completion_metadata 里没有 tool_calls）。"""
    counts, current = {}, None
    for index, event in enumerate(events):
        name = event.get("event")
        if name in MODEL_EVENTS:
            current = index
            counts[index] = 0
        elif name == "tool_executed" and current is not None:
            counts[current] += 1
        elif name == "run_finished":
            current = None
    return counts


def _usage(metadata):
    metadata = metadata or {}
    parts = [f"↑{format_tokens(metadata['input_tokens'])}"] if metadata.get("input_tokens") is not None else []
    if metadata.get("output_tokens") is not None:
        parts.append(f"↓{format_tokens(metadata['output_tokens'])}")
    if metadata.get("cached_tokens"):
        parts.append(f"缓存 {format_tokens(metadata['cached_tokens'])}")
    return " ".join(parts)


def _join(*parts):
    return " · ".join(str(part) for part in parts if part not in (None, ""))


def _model_line(event, number, tools):
    name = event.get("event")
    label = {"step_limit_summary": "步数用尽总结", "stall_summary": "空转总结"}.get(name, f"模型 #{number}")
    kind = str(event.get("kind", ""))
    outcome = {"final": "给出回答", "retry": "格式不对，重试"}.get(kind, "")
    if kind == "tools" or tools:
        outcome = f"{tools} 个工具调用"
    served = (event.get("completion_metadata") or {}).get("provider_model", "")
    return _join(f"◆ {label}", format_duration(event.get("duration_ms")), _usage(event.get("completion_metadata")), outcome, served)


def _tool_line(event):
    name = str(event.get("name", ""))
    status = str(event.get("tool_status") or event.get("status") or "ok")
    icon = "✓" if status == "ok" else ("◐" if status == "partial_success" else "✗")
    args = event.get("args") if isinstance(event.get("args"), dict) else {}
    summary = summarize_tool_output(name, str(event.get("result", "")), args)
    paths = list(event.get("affected_paths") or [])
    changed = f"改动 {'、'.join(paths[:4])}{' 等' if len(paths) > 4 else ''}" if paths else ""
    error = event.get("tool_error_code") if status != "ok" and not summary.startswith("error") else ""
    return _join(f"  {icon} {tool_title(name)} {card_args_summary(name, args)}".rstrip(), summary, error, changed, format_duration(event.get("duration_ms")))


def _event_line(event):
    """白名单外的事件返回 None。"""
    name = str(event.get("event", ""))
    if name == "verification_run":
        icon = "✓" if event.get("status") == "passed" else ("✗" if event.get("status") in {"failed", "timeout"} else "·")
        return _join(f"  {icon} 验证 {clip_cells(str(event.get('command', '')), 60)}", event.get("status"), format_duration(event.get("duration_ms")))
    if name == "verification_declined":
        return f"  · 验证未运行（未获批准）：{clip_cells('；'.join(event.get('commands') or []), 60)}"
    if name == "critic_review":
        objections = event.get("objections") or []
        verdict = "通过" if event.get("verdict") == "accept" else f"{len(objections)} 条意见，退回修复"
        reason = clip_cells(str(event.get("reason", "")), 50)
        return _join(f"  ◇ 审查 {event.get('model', '')}".rstrip(), verdict, reason, _usage(event.get("completion_metadata")), format_duration(event.get("duration_ms")))
    if name == "hook_run":
        blocked = "拦下" if event.get("blocked") else ""
        return _join(f"  ⚑ 钩子 {event.get('hook_event', '')}".rstrip(), clip_cells(str(event.get("command", "")), 50), f"exit {event.get('exit_code')}", blocked, format_duration(event.get("duration_ms")))
    if name == "provider_fallback":
        return "  ⇄ " + failover_notice(event)
    if name == "compaction_finished":
        return _join("  ⇣ 压缩历史", event.get("trigger"), f"{format_tokens(event.get('pre_tokens'))} → {format_tokens(event.get('post_tokens'))} tok")
    if name == "user_steer":
        return "  » 用户插话 " + clip_cells("；".join(event.get("messages") or []), 80)
    if name == "mentions_preloaded":
        return f"  @ 预读 {len(event.get('tools') or [])} 个提及的文件"
    if name == "loop_guard_stall" or name.startswith("stall_") and name != "stall_summary":
        detail = f"已浪费 {event['wasted_total']} 步" if "wasted_total" in event else ""
        return _join(f"  ! 空转保护 {name.removeprefix('stall_')}", detail)
    if name == "model_retry_scheduled":
        return _join("  ↻ 模型请求重试", event.get("code"), f"第 {event.get('retry_count')} 次")
    if name == "model_error":
        error = event.get("error") or {}
        return _join("  ✗ 模型错误", error.get("code"), clip_cells(str(error.get("body_excerpt", "")), 60))
    if name.endswith("_failed"):
        return _join(f"  ✗ {name}", clip_cells(str(event.get("error", "")), 60))
    return None


def render_timeline(events, report=None, state=None):
    report = report or {}
    state = state or report.get("task_state") or {}
    started = _parse_time(events[0].get("created_at")) if events else None
    finished = next((event for event in events if event.get("event") == "run_finished"), {})
    lines = [_header(report, state, finished, events), ""]
    tools_after = _tool_counts(events)
    other = Counter()
    shown = model_number = 0
    for index, event in enumerate(events):
        name = str(event.get("event", ""))
        if name in QUIET_EVENTS:
            continue
        if name in MODEL_EVENTS:
            model_number += name == "model_parsed"
            line = _model_line(event, model_number, tools_after.get(index, 0))
        elif name == "tool_executed":
            line = _tool_line(event)
        else:
            line = _event_line(event)
        if line is None:
            other[name] += 1
            continue
        shown += 1
        if shown > MAX_EVENT_LINES:
            continue
        lines.append(f"{_offset(started, event):>7}  {line}")
    if shown > MAX_EVENT_LINES:
        lines.append(f"         … 还有 {shown - MAX_EVENT_LINES} 个事件未显示")
    if other:
        detail = "、".join(f"{name} ×{count}" for name, count in other.most_common(6))
        lines.append(f"         （其他 {sum(other.values())} 个事件：{detail}）")
    lines += _footer(report, state, finished, events)
    return "\n".join(lines)


def _offset(started, event):
    moment = _parse_time(event.get("created_at"))
    if started is None or moment is None:
        return ""
    seconds = max(0.0, (moment - started).total_seconds())
    return f"+{seconds:.1f}s" if seconds < 60 else f"+{int(seconds // 60)}m{int(seconds % 60):02d}s"


def _header(report, state, finished, events):
    run_id = report.get("run_id") or (events[0].get("trace_id") if events else "") or state.get("run_id")
    status = str(report.get("status") or finished.get("status") or state.get("status") or "running")
    stop_reason = report.get("stop_reason") or finished.get("stop_reason") or state.get("stop_reason") or ""
    cost = (report.get("cost") or {}).get("usd")
    duration = format_duration(finished.get("run_duration_ms")) if finished else ""
    steps = state.get("tool_steps")
    head = _join(
        run_id,
        STATUS_TEXT.get(status, status) + (f"（{stop_reason}）" if stop_reason else ""),
        duration,
        f"{steps} 步" if steps is not None else "",
        f"花费约 ${cost:.4f}" if isinstance(cost, (int, float)) and cost else "",
    )
    request = str(state.get("user_request") or (events[0].get("user_request") if events else "") or "")
    first_line = request.strip().splitlines()[0] if request.strip() else ""
    return f"{head}\n请求：{clip_cells(first_line, 120)}"


def _footer(report, state, finished, events):
    lines = [""]
    total_in = total_out = calls = 0
    for event in events:
        metadata = event.get("completion_metadata") or {}
        if event.get("event") in MODEL_EVENTS | {"critic_review"} and metadata:
            calls += 1
            total_in += int(metadata.get("input_tokens") or 0)
            total_out += int(metadata.get("output_tokens") or 0)
    if calls:
        lines.append(f"模型调用 {calls} 次 · 输入 {format_tokens(total_in)} · 输出 {format_tokens(total_out)} tok")
    paths = (report.get("changes") or {}).get("paths") or state.get("changed_paths") or []
    if paths:
        shown = "、".join(paths[:12]) + (f" 等 {len(paths)} 个" if len(paths) > 12 else "")
        lines.append(f"改动的文件：{shown}")
    final = str(report.get("final_answer") or finished.get("final_answer") or state.get("final_answer") or "").strip()
    if final:
        body = final.splitlines()
        if len(body) > MAX_FINAL_LINES:
            body = body[:MAX_FINAL_LINES] + [f"…（还有 {len(body) - MAX_FINAL_LINES} 行）"]
        lines += ["", "最终回答：", *body]
    return lines
