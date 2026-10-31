"""Provider 故障切换：主 provider 暂时不可用时自动改用备用 provider。

只在“还没有向用户输出任何内容”且错误可重试（5xx / 限流 / 超时 / 网络）时切换；
鉴权失败、请求本身有问题等错误照常抛出，避免悄悄换模型掩盖配置错误。
切换后主 provider 进入冷却期，期间直接使用备用，不再每一步都等一轮重试。
"""

import time
from dataclasses import replace

from .base import complete_model

DEFAULT_COOLDOWN_SECONDS = 120.0


def convert_native_tools(tools, protocol):
    """把原生工具定义转换成目标协议的格式（openai <-> anthropic）。"""
    if not tools:
        return tools
    converted = []
    for tool in tools:
        if protocol == "anthropic" and "input_schema" not in tool:
            converted.append(
                {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("parameters", {}),
                }
            )
        elif protocol == "openai" and "input_schema" in tool:
            converted.append(
                {
                    "type": "function",
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                }
            )
        else:
            converted.append(tool)
    return converted


def _label(client):
    provider = str(getattr(client, "provider", "") or "")
    model = str(getattr(client, "model", "") or "")
    return provider, model


def _failure_summary(client, exc):
    provider, model = _label(client)
    summary = {
        "provider": provider,
        "model": model,
        "code": str(getattr(exc, "code", "") or type(exc).__name__),
    }
    if getattr(exc, "http_status", None) is not None:
        summary["http_status"] = int(exc.http_status)
    return summary


class FallbackModelClient:
    """包装一个主 client 和若干备用 client，对上层保持同一套 complete 接口。"""

    def __init__(
        self,
        primary,
        fallbacks,
        *,
        cooldown_seconds=DEFAULT_COOLDOWN_SECONDS,
        clock=time.monotonic,
    ):
        self._primary = primary
        self._fallbacks = list(fallbacks)
        self.cooldown_seconds = float(cooldown_seconds)
        self._clock = clock
        self._primary_down_until = 0.0
        self._pending_events = []
        self.last_completion_metadata = {}

    def __getattr__(self, name):
        if name.startswith("_primary") or name in {"_fallbacks", "_clock"}:
            raise AttributeError(name)
        return getattr(self._primary, name)

    @property
    def model(self):
        return self._primary.model

    @model.setter
    def model(self, value):
        self._primary.model = value

    @property
    def fallback_clients(self):
        return list(self._fallbacks)

    def drain_failover_events(self):
        events, self._pending_events = self._pending_events, []
        return events

    def complete(self, prompt, max_new_tokens, **kwargs):
        return self.complete_result(prompt, max_new_tokens, **kwargs).text

    def complete_result(
        self,
        prompt,
        max_new_tokens,
        prompt_cache_key=None,
        prompt_cache_retention=None,
        on_delta=None,
        tools=None,
        system_chars=0,
    ):
        emitted = []

        def tracked(delta):
            if delta:
                emitted.append(True)
            on_delta(delta)

        in_cooldown = self._clock() < self._primary_down_until
        errors = []
        for client in self._attempt_order(in_cooldown):
            cache = bool(getattr(client, "supports_prompt_cache", False))
            try:
                result = complete_model(
                    client,
                    prompt,
                    max_new_tokens,
                    prompt_cache_key=prompt_cache_key if cache else None,
                    prompt_cache_retention=prompt_cache_retention if cache else None,
                    tools=convert_native_tools(
                        tools, getattr(client, "native_tool_protocol", "")
                    ),
                    on_delta=tracked if on_delta is not None else None,
                    system_chars=system_chars,
                )
            except Exception as exc:
                if emitted or not getattr(exc, "retryable", False):
                    raise
                errors.append((client, exc))
                continue
            return self._finish(client, result, errors, in_cooldown)
        raise self._final_error(errors)

    def _attempt_order(self, in_cooldown):
        if in_cooldown and self._fallbacks:
            return [*self._fallbacks, self._primary]
        return [self._primary, *self._fallbacks]

    def _finish(self, client, result, errors, in_cooldown):
        metadata = dict(result.metadata or {})
        if client is self._primary:
            self._primary_down_until = 0.0
        else:
            failures = [_failure_summary(failed, exc) for failed, exc in errors]
            if any(failed is self._primary for failed, _ in errors):
                self._primary_down_until = self._clock() + self.cooldown_seconds
            provider, model = _label(client)
            info = {
                "served_by": {"provider": provider, "model": model},
                "failed": failures,
                "primary_in_cooldown": bool(in_cooldown),
            }
            metadata["provider_fallback"] = info
            if failures:
                self._pending_events.append(info)
            result = replace(result, metadata=metadata)
        self.last_completion_metadata = dict(metadata)
        return result

    def _final_error(self, errors):
        primary_errors = [exc for client, exc in errors if client is self._primary]
        error = (primary_errors or [errors[0][1]])[0]
        if len(errors) > 1:
            error.fallback_failures = [
                _failure_summary(client, exc)
                for client, exc in errors
                if exc is not error
            ]
        return error
