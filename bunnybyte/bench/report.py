"""bench 结果的汇总、落盘、历史对比与终端渲染。"""

import json
import re
import subprocess
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from ..core.pricing import format_usd

SCHEMA_VERSION = 1
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
NOISE_NOTE = "每题 = {step:.1f} 个百分点；单次结果上下浮动 1 题属于噪声，需要看多次历史才能下结论。"


def _total(rows, key):
    values = [row[key] for row in rows if isinstance(row.get(key), int)]
    return sum(values) if values else None


def _average(rows, key):
    values = [row[key] for row in rows if isinstance(row.get(key), (int, float))]
    return round(sum(values) / len(values), 2) if values else None


def _per_pass(rows, keys):
    """全部任务（含失败的）消耗的总量 / 通过数：失败题的开销也算在通过的任务头上。"""
    keys = (keys,) if isinstance(keys, str) else keys
    passed = sum(1 for row in rows if row["passed"])
    totals = [_total(rows, key) for key in keys]
    if not passed or any(total is None for total in totals):
        return None
    return round(sum(totals) / passed)


def _reuse(rows):
    prompt = sum(int(row.get("prompt_chars") or 0) for row in rows if row.get("reused_chars") is not None)
    reused = sum(int(row.get("reused_chars") or 0) for row in rows if row.get("reused_chars") is not None)
    return round(reused / prompt, 3) if prompt else None


def _cost_per_pass(rows):
    total, passed = _cost_total(rows), sum(1 for row in rows if row["passed"])
    return round(total / passed, 6) if total is not None and passed else None


def _cost_total(rows):
    """每题都有价格时才给总花费；有一题缺价格就返回 None（部分求和会偏低）。"""
    values = [row.get("cost_usd") for row in rows]
    if not values or any(not isinstance(value, (int, float)) for value in values):
        return None
    return round(sum(values), 6)


def _proof_counts(rows):
    """修复证明各状态的次数，跳过的按原因合并成 skipped:原因。"""
    counts = {}
    for row in rows:
        label = row.get("fix_proof") or ""
        if label:
            counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items()))


def _proof_agreement(rows):
    """修复证明给出结论（已证明 / 没测到）的行里，有多少和隐藏判分一致：已证明 ⇔ 判分通过。"""
    decided = [row for row in rows if row.get("fix_proof") in ("proven", "theater")]
    agree = sum(1 for row in decided if (row["fix_proof"] == "proven") == bool(row["passed"]))
    return {"decided": len(decided), "agree": agree}


def summarize(rows):
    runs = len(rows)
    passed = sum(1 for row in rows if row["passed"])
    return {
        "runs": runs,
        "passed": passed,
        "pass_rate": round(passed / runs, 4) if runs else 0.0,
        "clean_finish": sum(1 for row in rows if row.get("stop_reason") == "final_answer_returned"),
        "within_budget": sum(1 for row in rows if not row.get("over_budget")),
        "avg_steps": _average(rows, "steps"),
        "avg_model_calls": _average(rows, "model_calls"),
        "avg_wasted_steps": _average(rows, "wasted_steps"),
        "step_limit_stops": sum(1 for row in rows if row.get("stop_reason") == "step_limit_reached"),
        "loop_stalled": sum(1 for row in rows if row.get("stop_reason") == "loop_stalled"),
        "model_calls_per_pass": _per_pass(rows, "model_calls"),
        "tokens_per_pass": _per_pass(rows, ("input_tokens", "output_tokens")),
        "avg_wall_seconds": _average(rows, "wall_seconds"),
        "total_wall_seconds": round(sum(row["wall_seconds"] for row in rows), 1),
        "input_tokens": _total(rows, "input_tokens"),
        "output_tokens": _total(rows, "output_tokens"),
        "cached_tokens": _total(rows, "cached_tokens"),
        "prompt_reuse": _reuse(rows),
        "cost_usd": _cost_total(rows),
        "cost_per_pass": _cost_per_pass(rows),
        "verify_rounds": sum(row.get("verify_rounds", 0) for row in rows),
        "verify_repairs": sum(row.get("verify_repairs", 0) for row in rows),
        "false_claims": sum(1 for row in rows if row.get("false_claim")),
        "critic_rounds": sum(row.get("critic_rounds", 0) for row in rows),
        "critic_objections": sum(row.get("critic_objections", 0) for row in rows),
        "objections_on_passing": sum(row.get("objection_on_passing", 0) for row in rows),
        "critic_regressions": sum(1 for row in rows if row.get("critic_regression")),
        "fix_proof": _proof_counts(rows),
        "fix_proof_repairs": sum(row.get("fix_proof_repairs", 0) for row in rows),
        "fix_proof_agreement": _proof_agreement(rows),
    }


def _git(*args):
    if not (PACKAGE_ROOT / ".git").exists():
        return ""  # 通过 pip 安装时包目录不在自己的仓库里，别误取外层仓库的提交
    try:
        done = subprocess.run(
            ["git", *args], cwd=PACKAGE_ROOT, capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def package_version():
    from ..version import __version__

    return __version__


def build_record(
    rows, tasks, *, provider, model, set_hash, repeat, max_steps=None, protocol="", verify=False, critic=False, fix_proof="off"
):
    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bunnybyte_version": package_version(),
        "git_sha": _git("rev-parse", "--short", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain", "--", "bunnybyte")),
        "provider": provider,
        "model": model,
        "protocol": protocol,
        "task_set_hash": set_hash,
        "task_ids": [task.id for task in tasks],
        "repeat": repeat,
        "max_steps": max_steps,
        "verify": bool(verify) or fix_proof != "off",
        "critic": bool(critic),
        "fix_proof": fix_proof,
        "summary": summarize(rows),
        "runs": rows,
    }


def _slug(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(text)).strip("-") or "unknown"


def save_record(record, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = record["captured_at"].replace("-", "").replace(":", "").replace("T", "-").rstrip("Z")
    base = f"{stamp}-{_slug(record['provider'])}-{_slug(record['model'])}"
    path = out_dir / f"{base}.json"
    suffix = 1
    while path.exists():  # 同一秒内的两次运行不能互相覆盖
        suffix += 1
        path = out_dir / f"{base}-{suffix}.json"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, dir=out_dir, prefix=path.name + ".", suffix=".tmp"
    ) as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temp_name = handle.name
    Path(temp_name).replace(path)
    return path


def load_history(out_dir):
    records = []
    for path in sorted(Path(out_dir).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("schema_version") == SCHEMA_VERSION and "summary" in data:
            records.append(data)
    return sorted(records, key=lambda item: item["captured_at"])


def previous_comparable(record, history):
    """上一次同任务集、同 provider、同模型、同步数上限、同验证闸门 / 审查 / 修复证明设置的结果；任一项变了就不比较。"""
    keys = ("task_set_hash", "provider", "model", "max_steps")
    older = [
        item
        for item in history
        if all(item.get(key) == record.get(key) for key in keys)
        and bool(item.get("verify")) == bool(record.get("verify"))
        and bool(item.get("critic")) == bool(record.get("critic"))
        and (item.get("fix_proof") or "off") == (record.get("fix_proof") or "off")  # 旧结果没有这一项：当时没跑修复证明
        and item["captured_at"] < record["captured_at"]
    ]
    return older[-1] if older else None


def _width(text):
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in str(text))


def pad(text, width):
    return str(text) + " " * max(0, width - _width(text))


def _fmt(value, suffix=""):
    return "-" if value is None else f"{value}{suffix}"


def _tokens(summary, protocol=""):
    if summary["input_tokens"] is None and summary["output_tokens"] is None:
        return "provider 未上报"
    cached = summary.get("cached_tokens")
    # OpenAI 风格的 cached_tokens 是 input_tokens 的子集；Anthropic 风格则是另外计的。
    word = "其中" if protocol == "openai" else "另有"
    extra = f"（{word} {cached} 命中缓存）" if cached else ""
    return f"输入 {_fmt(summary['input_tokens'])}{extra} / 输出 {_fmt(summary['output_tokens'])}"


def _delta(record, previous):
    if previous is None:
        return "无可比较的历史结果（任务集、provider、模型、步数上限需一致）"
    gap = (record["summary"]["pass_rate"] - previous["summary"]["pass_rate"]) * 100
    steps = ""
    now_steps, then_steps = record["summary"]["avg_steps"], previous["summary"]["avg_steps"]
    if now_steps is not None and then_steps is not None:
        steps = f"，平均步数 {now_steps - then_steps:+.1f}"
    return f"相比 {previous['captured_at'][:10]}（{previous.get('git_sha') or '?'}）：通过率 {gap:+.1f} 个百分点{steps}"


def render_row(row):
    mark = "✓" if row["passed"] else "✗"
    tokens = "" if row.get("input_tokens") is None else f"  in {row['input_tokens']} / out {row['output_tokens'] or 0}"
    note = "" if row["passed"] else f"  {row['failure'][:80]}"
    budget = " (超预算)" if row.get("over_budget") else ""
    gate = f"  [验证 {row['verify_status']}{'·修复' + str(row['verify_repairs']) if row.get('verify_repairs') else ''}]" if row.get("verify_status") else ""
    wasted = f"，空转 {row['wasted_steps']}" if row.get("wasted_steps") else ""
    if row.get("fix_proof"):
        gate += f"  [修复证明 {row['fix_proof']}{'·退回' + str(row['fix_proof_repairs']) if row.get('fix_proof_repairs') else ''}]"
    if row.get("critic_rounds"):
        gate += f"  [审查 {row['critic_rounds']} 次·反对 {row.get('critic_objections', 0)}]"
    return f"{mark} {row['task']} #{row['attempt']}  {row['steps']} 步{wasted}{budget}  {row['wall_seconds']}s{tokens}{gate}{note}"


def render_record(record, previous=None):
    summary = record["summary"]
    by_task = {}
    for row in record["runs"]:
        by_task.setdefault(row["task"], []).append(row)
    name_width = max([_width(task) for task in by_task] + [4])
    lines = [f"{pad('任务', name_width)}  通过  平均步数  平均耗时"]
    for task, rows in by_task.items():
        ok = sum(1 for row in rows if row["passed"])
        lines.append(
            f"{pad(task, name_width)}  {ok}/{len(rows)}   {_fmt(_average(rows, 'steps')):>7}  {_fmt(_average(rows, 'wall_seconds'), 's')}"
        )
    lines += [
        "",
        f"模型：{record['provider']} / {record['model']}    bunnybyte {record['bunnybyte_version']} @ {record.get('git_sha') or '?'}",
        f"通过率：{summary['passed']}/{summary['runs']} = {summary['pass_rate'] * 100:.1f}%    正常收尾 {summary['clean_finish']}/{summary['runs']}    步数达标 {summary['within_budget']}/{summary['runs']}",
        f"平均步数 {_fmt(summary['avg_steps'])}，平均模型调用 {_fmt(summary['avg_model_calls'])}，总耗时 {summary['total_wall_seconds']}s",
        f"Token：{_tokens(summary, record.get('protocol', ''))}",
        *_cost_lines(summary),
        *_reuse_lines(summary),
        *_waste_lines(summary),
        *_verify_lines(record),
        *_critic_lines(record),
        *_proof_lines(record),
        _delta(record, previous),
        NOISE_NOTE.format(step=100 / max(len(record["task_ids"]) * record["repeat"], 1)),
    ]
    return "\n".join(lines)


def _cost_lines(summary):
    if summary.get("cost_usd") is None:
        return []  # 模型不在价目表里，或旧结果文件没有这一列
    per_pass = summary.get("cost_per_pass")
    tail = f"，每个通过任务 {format_usd(per_pass)}" if per_pass is not None else ""
    return [f"花费估算：{format_usd(summary['cost_usd'])}{tail}（按官网价目，经网关转发时以实际账单为准）"]


def _reuse_lines(summary):
    if summary.get("prompt_reuse") is None:
        return []  # 旧结果文件没有这一列
    return [
        f"提示词前缀复用：第 2 次起的模型调用平均有 {summary['prompt_reuse'] * 100:.1f}% 的提示词和上一次开头逐字相同"
        "（provider 前缀缓存最多能复用这么多）"
    ]


def _waste_lines(summary):
    if summary.get("avg_wasted_steps") is None:
        return []  # 旧结果文件没有这一列
    per_pass = [
        f"{label} {_fmt(summary.get(key))}"
        for label, key in (("模型调用", "model_calls_per_pass"), ("token", "tokens_per_pass"))
        if summary.get(key) is not None
    ]
    cost = f"；每个通过任务平均 {'、'.join(per_pass)}" if per_pass else ""
    return [
        f"空转：平均每题 {_fmt(summary['avg_wasted_steps'])} 步是重复/无新信息的调用（不计入步数上限）；"
        f"撞步数上限 {summary['step_limit_stops']} 次，被判空转提前收尾 {summary['loop_stalled']} 次{cost}"
    ]


def _critic_lines(record):
    if not record.get("critic"):
        return []
    summary = record["summary"]
    return [
        f"独立审查：审查 {summary.get('critic_rounds', 0)} 次，提出反对 {summary.get('critic_objections', 0)} 次；"
        f"其中 {summary.get('objections_on_passing', 0)} 次反对时代码本来已能通过判分，"
        f"照意见修改后反而不通过 {summary.get('critic_regressions', 0)} 题"
    ]


PROOF_NAMES = {"proven": "已证明", "theater": "没测到", "weak": "弱"}


def _proof_lines(record):
    if (record.get("fix_proof") or "off") == "off":
        return []
    summary = record["summary"]
    counts = summary.get("fix_proof") or {}
    parts = [f"{PROOF_NAMES.get(label, label)} {count}" for label, count in counts.items()]
    agreement = summary.get("fix_proof_agreement") or {}
    agree = ""
    if agreement.get("decided"):
        agree = f"；给出结论的 {agreement['decided']} 次里 {agreement['agree']} 次和隐藏判分一致（已证明 ⇔ 通过）"
    return [
        f"修复证明（{record['fix_proof']}）：{'、'.join(parts) or '一次也没跑'}；退回模型 {summary.get('fix_proof_repairs', 0)} 次{agree}"
    ]


def _verify_lines(record):
    summary = record["summary"]
    if not record.get("verify") and not summary.get("false_claims"):
        return []
    claims = f"声称已验证但判分未过 {summary.get('false_claims', 0)} 次"
    if not record.get("verify"):
        return [claims]
    return [f"验证闸门：触发 {summary['verify_rounds']} 次，其中让模型修复 {summary['verify_repairs']} 次；{claims}"]


def render_history(records):
    if not records:
        return "还没有 bench 历史结果。运行 `bunny bench` 生成第一条。"
    lines = ["日期        版本    提交      模型                     通过率    平均步数  任务集"]
    for item in records:
        summary = item["summary"]
        lines.append(
            f"{item['captured_at'][:10]}  {pad(item['bunnybyte_version'], 6)}  {pad(item.get('git_sha') or '?', 8)}  "
            f"{pad(item['provider'] + '/' + item['model'], 23)}  {summary['pass_rate'] * 100:5.1f}%   "
            f"{pad(_fmt(summary['avg_steps']), 8)}  {item['task_set_hash']}"
        )
    return "\n".join(lines)
