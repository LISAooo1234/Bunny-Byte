"""从一次运行的 trace 里汇总模型 token 用量（bench 与无人值守模式共用）。"""

import json
from pathlib import Path

# 独立审查、步数用尽 / 空转时的总结也是模型调用，token 要算进去
USAGE_EVENTS = {"model_parsed", "critic_review", "step_limit_summary", "stall_summary"}


def trace_usage(trace_path):
    """汇总 trace 里每次模型调用上报的 token；provider 没上报就返回 None，而不是 0。"""
    totals = {"input_tokens": None, "output_tokens": None, "cached_tokens": None}
    path = Path(trace_path)
    if not path.exists():
        return totals
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") not in USAGE_EVENTS:
            continue
        metadata = event.get("completion_metadata") or {}
        for key in totals:
            value = metadata.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                totals[key] = (totals[key] or 0) + value
    return totals


def trace_prompt_reuse(trace_path):
    """一次运行里第 2 次起的模型调用，提示词开头和上一次逐字相同的比例（provider 前缀缓存最多能复用这么多）。

    返回 {"prompt_chars", "reused_chars"}，没有可比较的调用时都是 None。
    """
    totals = {"prompt_chars": None, "reused_chars": None}
    path = Path(trace_path)
    if not path.exists():
        return totals
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        reused = event.get("reused_chars")
        if event.get("event") != "model_requested" or not isinstance(reused, int):
            continue
        totals["prompt_chars"] = (totals["prompt_chars"] or 0) + int(event.get("prompt_chars") or 0)
        totals["reused_chars"] = (totals["reused_chars"] or 0) + reused
    return totals
