"""主 provider 故障切换后的提示与留痕。"""


def failover_notice(info):
    served = info.get("served_by", {}) or {}
    failed = info.get("failed", []) or []
    reasons = "、".join(
        f"{item.get('provider', '?')} "
        + (f"HTTP {item['http_status']}" if item.get("http_status") else str(item.get("code", "")))
        for item in failed
    )
    target = f"{served.get('provider', '?')}/{served.get('model', '?')}"
    return f"[provider 切换] {reasons or '主 provider'} 暂时不可用，本次请求已改用备用 {target}。"


def failover_events(engine, task_state):
    """取出模型 client 记录的切换事件，写入 trace / 会话事件并产出界面提示。"""
    agent = engine.runtime
    drain = getattr(agent.model_client, "drain_failover_events", None)
    if not callable(drain):
        return
    events = drain()
    if not isinstance(events, list):
        return
    for info in events:
        agent.emit_trace(task_state, "provider_fallback", dict(info))
        agent.session_event_bus.emit(
            "provider_fallback", {"run_id": task_state.run_id, **info}
        )
        yield {
            "type": "runtime_notice",
            "run_id": task_state.run_id,
            "content": failover_notice(info),
        }
