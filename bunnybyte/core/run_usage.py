"""从一次运行的 trace 里汇总模型 token 用量（bench 与无人值守模式共用）。"""

import json
from pathlib import Path


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
        if event.get("event") != "model_parsed":
            continue
        metadata = event.get("completion_metadata") or {}
        for key in totals:
            value = metadata.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                totals[key] = (totals[key] or 0) + value
    return totals
