"""花费估算与本轮预算：每次模型调用按实际服务的模型计价，累加到本轮和整个会话。

模型调用散在好几处（主循环、独立审查、步数用尽时的总结、历史压缩、回合索引、子 agent、记忆整理），
这里不包装 model client，而是由各调用点显式 `add()`。父 agent 和它派生的子 runtime 共用同一个计量器，
所以 worker 花的钱也算进本轮。
"""

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .pricing import PRICES_AS_OF, call_cost, format_usd, lookup_price, parse_price_overrides

STOP_REASON_BUDGET_EXCEEDED = "budget_exceeded"
WARN_RATIO = 0.8
TOKEN_KEYS = ("input_tokens", "output_tokens", "cached_tokens", "cache_creation_tokens")


@dataclass
class BudgetConfig:
    max_turn_cost: float | None = None
    prices: dict = field(default_factory=dict)
    problems: list = field(default_factory=list)


def resolve_budget_config(values, max_cost=None):
    """配置里的 `[budget] max_turn_cost` 与 `[pricing."模型"]`；命令行 --max-cost 优先。"""
    budget = values.get("budget") or {}
    problems = []
    limit = max_cost
    if limit is None and isinstance(budget, dict) and budget.get("max_turn_cost") is not None:
        raw = budget.get("max_turn_cost")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
            problems.append("[budget].max_turn_cost 应该是大于 0 的数字（美元），已忽略")
        else:
            limit = float(raw)
    prices, price_problems = parse_price_overrides(values.get("pricing") or {})
    return BudgetConfig(max_turn_cost=limit, prices=prices, problems=problems + price_problems)


def _empty():
    return {"usd": 0.0, "calls": 0, "unpriced_calls": 0, "unpriced_models": [], "by_model": {}, "by_source": {}}


def _served_model(metadata, fallback_model):
    served = (metadata.get("provider_fallback") or {}).get("served_by") or {}
    return str(served.get("model") or metadata.get("provider_model") or fallback_model or "")


def _has_tokens(metadata):
    return any(isinstance(metadata.get(key), int) and not isinstance(metadata.get(key), bool) for key in TOKEN_KEYS)


class CostMeter:
    def __init__(self, owner=None, clock=None):
        self.owner = owner
        self.prices = {}
        self.limit = None
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.turn = _empty()
        self._session = None
        self._warned = False
        self._unpriced_warned = False
        self._forward = None  # 后台计量器：花费只记进这个父计量器的会话累计，不进它的本轮
        self._lock = threading.Lock()

    def background(self, owner):
        """给记忆整理、技能演化这类后台 runtime 用：自己的本轮、没有上限，花费计入会话累计但不占用户这一轮的预算。

        和父计量器共用一个“本轮”会出两个问题：用户这一轮刚超上限，紧接着的记忆整理一启动就被拦下；
        后台线程在下一轮跑起来时又把钱记到用户的下一轮头上。
        """
        child = CostMeter(owner=owner, clock=self.clock)
        child.prices = self.prices
        child._forward = self
        return child

    def configure(self, config):
        self.prices = dict(config.prices or {})
        self.limit = config.max_turn_cost

    def bind_session(self, session):
        """会话累计写在 session["cost"] 里，跟着 session JSON 落盘，/resume 后接着累加。"""
        self._session = session
        saved = session.get("cost")
        session["cost"] = _merged(_empty(), saved if isinstance(saved, dict) else {})

    def start_turn(self, runtime):
        if runtime is not self.owner:
            return  # 子 agent / 记忆整理的回合不重置父 agent 的本轮计数
        with self._lock:
            self.turn = _empty()
            self._warned = False
            self._unpriced_warned = False

    def add(self, metadata, *, source="main", client=None, protocol=""):
        """记一次模型调用；返回这次的美元估算，没上报 token 或模型没有价格时返回 None。"""
        metadata = dict(metadata or {})
        if not _has_tokens(metadata):
            return None
        model = _served_model(metadata, getattr(client, "model", ""))
        protocol = str(
            metadata.get("provider_protocol") or protocol or getattr(client, "native_tool_protocol", "") or ""
        )
        price, _source = lookup_price(model, self.prices)
        cost = call_cost(price, metadata, protocol, self.clock()) if price else None
        with self._lock:
            self.turn = _added(self.turn, model or "?", source, metadata, cost)
            self._add_to_session(model or "?", source, metadata, cost)
        if self._forward is not None:
            with self._forward._lock:
                self._forward._add_to_session(model or "?", source, metadata, cost)
        return cost

    def _add_to_session(self, model, source, metadata, cost):
        if self._session is not None:
            # 整体替换而不是原地修改：别的线程正在把 session 写盘时也不会遇到“字典在迭代中被改”
            self._session["cost"] = _added(self._session.get("cost") or _empty(), model, source, metadata, cost)

    @property
    def session_totals(self):
        return dict((self._session or {}).get("cost") or _empty())

    def turn_snapshot(self):
        with self._lock:
            snapshot = _merged(_empty(), self.turn)
        snapshot["usd"] = round(snapshot["usd"], 6)
        snapshot.update(estimate=True, prices_as_of=PRICES_AS_OF, limit_usd=self.limit)
        return snapshot

    def over_budget(self):
        return self.limit is not None and self.turn["usd"] >= self.limit

    def take_warning(self):
        """本轮花费第一次越过上限的 80% 时返回一条提示，之后返回空串。"""
        if self.limit is None or self._warned or self.over_budget() or self.turn["usd"] < self.limit * WARN_RATIO:
            return ""
        self._warned = True
        return f"本轮花费估算 {format_usd(self.turn['usd'])}，已到上限 {format_usd(self.limit)} 的 80%。"

    def stop_text(self):
        unpriced = self.turn["unpriced_calls"]
        extra = f"另有 {unpriced} 次调用的模型没有价格，未计入。" if unpriced else ""
        return (
            f"本轮模型花费估算 {format_usd(self.turn['usd'])}，已达到上限 {format_usd(self.limit)}"
            f"（--max-cost / [budget] max_turn_cost），已停止，没有再调用模型。{extra}"
            "改动和进度都已保存：说「继续」会开始新一轮、重新计算预算；也可以调高上限。"
        )

    def take_unpriced_notice(self):
        if self._unpriced_warned:
            return ""
        text = self.unpriced_notice()
        self._unpriced_warned = bool(text)
        return text

    def unpriced_notice(self):
        """设了上限但这一轮的模型都没有价格：上限实际不生效，要明说。"""
        turn = self.turn
        if self.limit is None or not turn["unpriced_calls"] or turn["calls"] > turn["unpriced_calls"]:
            return ""
        models = "、".join(turn["unpriced_models"][:3])
        return (
            f"模型 {models} 不在价目表里，花费上限 {format_usd(self.limit)} 无法生效；"
            '在配置里加 [pricing."模型名"] input = … output = …（美元 / 百万 token）后才会计费。'
        )


def _merged(base, extra):
    result = dict(base)
    for key, value in extra.items():
        result[key] = dict(value) if isinstance(value, dict) else (list(value) if isinstance(value, list) else value)
    return result


def _added(totals, model, source, metadata, cost):
    totals = _merged(_empty(), totals)
    totals["calls"] += 1
    entry = dict(totals["by_model"].get(model) or {"usd": 0.0, "calls": 0, "priced": cost is not None})
    entry["calls"] += 1
    for key in TOKEN_KEYS:
        value = metadata.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            entry[key] = entry.get(key, 0) + value
    if cost is None:
        totals["unpriced_calls"] += 1
        if model not in totals["unpriced_models"]:
            totals["unpriced_models"].append(model)
    else:
        totals["usd"] += cost
        entry["usd"] += cost
        entry["priced"] = True
        totals["by_source"][source] = totals["by_source"].get(source, 0.0) + cost
    totals["by_model"][model] = entry
    return totals


def record_call(agent, metadata, source="main", client=None):
    """各调用点用的入口：agent 没有计量器（测试替身、旧对象）时什么也不做。"""
    meter = getattr(agent, "cost_meter", None)
    if meter is None:
        return None
    if source == "main" and getattr(agent, "depth", 0) > 0:
        source = "worker"
    return meter.add(metadata, source=source, client=client or getattr(agent, "model_client", None))


def budget_stop_needed(agent):
    meter = getattr(agent, "cost_meter", None)
    return bool(meter is not None and meter.over_budget())


def cost_notices(agent, task_state):
    """主循环每次模型调用之后：越过 80% 或上限无法生效时各提示一次。"""
    meter = getattr(agent, "cost_meter", None)
    if meter is None:
        return
    for text in (meter.take_warning(), meter.take_unpriced_notice()):
        if text:
            agent.session_event_bus.emit("assistant_message", {"run_id": task_state.run_id, "kind": "runtime_notice", "content": text})
            yield {"type": "runtime_notice", "run_id": task_state.run_id, "content": text}


def _totals_text(totals):
    text = format_usd(totals.get("usd", 0.0))
    unpriced = int(totals.get("unpriced_calls", 0) or 0)
    calls = int(totals.get("calls", 0) or 0)
    detail = f"{calls} 次调用"
    if unpriced:
        detail += f"，其中 {unpriced} 次模型没有价格未计入（{'、'.join(totals.get('unpriced_models', [])[:3])}）"
    return f"{text}（{detail}）"


def cost_rows(agent):
    """/usage 里的花费行：本轮、会话、按模型拆分、上限与价格来源。"""
    meter = getattr(agent, "cost_meter", None)
    if meter is None:
        return []
    turn, session = meter.turn_snapshot(), meter.session_totals
    rows = [
        ("Cost this turn (estimate)", _totals_text(turn)),
        ("Cost this session (estimate)", _totals_text(session)),
        ("Turn cost limit", format_usd(meter.limit) if meter.limit is not None else "未设置（--max-cost / [budget] max_turn_cost）"),
    ]
    for model, entry in sorted((session.get("by_model") or {}).items()):
        price, source = lookup_price(model, meter.prices)
        where = source if price else '没有价格，可在配置里加 [pricing."模型名"]'
        tokens = f"in {entry.get('input_tokens', 0)} / cached {entry.get('cached_tokens', 0)} / out {entry.get('output_tokens', 0)}"
        rows.append((f"Cost · {model}", f"{format_usd(entry.get('usd', 0.0))} · {entry.get('calls', 0)} 次 · {tokens} · {where}"))
    if session.get("by_source"):
        parts = "、".join(f"{name} {format_usd(value)}" for name, value in sorted(session["by_source"].items()))
        rows.append(("Cost by source", parts))
    rows.append(("Cost note", f"按官网价目（{PRICES_AS_OF}）估算；经网关转发、缓存没有上报时以实际账单为准"))
    return rows
