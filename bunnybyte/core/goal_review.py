"""目标评审：检查通过之后、宣布达成之前，另起一次不带工具的模型调用，对照目标和改过的文件挑错。

评审只能否决“达成”，不能增加轮数：有证据的反对意见让目标多跑一轮（整个目标最多否决一次，
而且只在还剩轮数、剩余预算够再跑一轮时才评审）；回复不是 JSON、provider 出错都按通过处理并记下原因。
被评审的文件内容来自仓库，转义后才放进提示词；最坏情况是被诱导多跑一轮，不会因此宣布达成。
"""

import time
from dataclasses import dataclass

from ..providers.base import complete_model
from .cost_meter import record_call
from .critic import VERDICT_OBJECT, parse_review
from .project_rules import render_rules
from .shadow_snapshots import ShadowStore
from .transcript_fence import fence
from .turn_diff import text_or_none
from .workspace import clip

MAX_VETOES = 1
MAX_FILE_CHARS = 6000
MAX_TOTAL_CHARS = 24000
RULES_CHARS = 4000
TAIL_CHARS = 1500
SOURCE = "goal_review"
BUDGET_MARGIN = 1.5
REVIEW_SHARE = 0.25

REVIEW_PROMPT = """You are a strict, independent reviewer. A coding agent has been working toward the goal below \
over several turns, and the check command chosen for the goal now passes. Before the goal is accepted as done, \
decide whether the changes really meet it. You cannot run tools or read other files: judge only from what is shown.

Work like a checklist. First list every explicit requirement in the goal and every project rule that applies, and \
decide from the files shown whether each is met. A passing check proves only what it tests. Then object to each \
unmet requirement and to any definite bug you can point to in the files shown. Never object to style, naming, \
nice-to-haves, or anything you cannot back with evidence from the files shown; never object because test output \
is not shown or because you cannot run the code.

Reply with exactly one JSON object and nothing else:
{{"checklist": [{{"requirement": "<short>", "applies": true, "met": false, "evidence": "<what the files show>"}}], \
"verdict": "accept" or "object", \
"objections": [{{"file": "<path>", "issue": "<what is wrong>", "evidence": "<the requirement and what the file shows>"}}]}}

## Goal
{objective}

## Project rules
{rules}

## Check (passed)
`{command}`
{tail}

## Current content of the files changed while pursuing the goal{clipped}
{files}
"""

VETO_MESSAGE = (
    "{objective}\n\n[Goal mode, round {number}/{limit}: this message was written by the runtime, not typed by the "
    "user.] After your previous turn the check `{command}` passed, but an independent reviewer compared the changed "
    "files with the goal and objected before the goal was accepted:\n```\n{items}\n```\n"
    "Fix the points that are right with tools and keep the check passing. If an objection is wrong, do not edit for "
    "it; say briefly why in your final answer. The runtime re-runs the check when this turn ends; there is no second "
    "review. Do not edit the check to make it pass."
)


@dataclass(frozen=True)
class GoalReviewConfig:
    evaluator: str = ""  # "" = 关闭，"main" = 当前主模型，其他 = provider profile 名

    @property
    def enabled(self):
        return bool(self.evaluator)


def resolve_goal_review_config(values, *, evaluator=None):
    """从合并后的配置字典解析 `[goal].evaluator`；evaluator 是命令行覆盖值（"off" 表示关闭）。"""
    section = dict((values or {}).get("goal", {}) or {})
    value = str(evaluator if evaluator is not None else section.get("evaluator", "") or "").strip()
    return GoalReviewConfig(evaluator="" if value.lower() in {"", "off", "none"} else value)


def review_skip_reason(agent, goal):
    """检查通过后要不要评审：返回 None 表示要评审，否则是跳过原因（空字符串表示没开评审，不必记录）。"""
    config = getattr(agent, "goal_review_config", None)
    if config is None or not config.enabled:
        return ""
    if vetoes(goal) >= MAX_VETOES:
        return ""
    used = len(goal["rounds"]) - int(goal.get("round_base", 0))
    if used >= goal["max_rounds"]:
        return "没有剩余轮数，评审否决了也无法修改"
    if goal.get("max_cost"):
        spent = max(float(goal["rounds"][-1].get("cost_usd") or 0.0), 0.0)  # 自上次预算重置以来的花费
        counted = len(goal["rounds"]) - int(goal.get("cost_round_base", 0))
        per_round = spent / max(counted, 1)
        # 否决后的那一轮要能跑完：按平均每轮的 1.5 倍留余量，再加上评审本身（按一轮的 1/4 估）
        if goal["max_cost"] - spent < per_round * (BUDGET_MARGIN + REVIEW_SHARE):
            return "剩余预算不够再跑一轮"
    if agent.abort_requested:
        return "已请求停止"
    return None


def vetoes(goal):
    return sum(1 for item in goal.get("reviews") or [] if item.get("verdict") == VERDICT_OBJECT)


def run_review(agent, goal, receipt, state):
    """生成器：调用评审模型，返回评审记录（verdict / objections / reason …），并写进 goal["reviews"]。"""
    client = getattr(agent, "goal_review_client", None) or agent.model_client  # 用时再取：/model 切换后跟着变
    files, clipped = _changed_files(agent, goal["paths"])
    prompt = REVIEW_PROMPT.format(
        objective=clip(agent.redact_text(goal["objective"]), 4000),
        rules=clip(agent.redact_text(render_rules(getattr(agent.workspace, "project_rules", ()))), RULES_CHARS) or "(none)",
        command=" && ".join(goal["checks"]),
        tail=_fenced(agent.redact_text(clip(str(receipt.get("output_tail", "")), TAIL_CHARS))),
        clipped=" (CLIPPED: parts are not shown, do not object to what you cannot see)" if clipped else "",
        files=files or "(none readable)",
    )
    model = str(getattr(client, "model", "") or getattr(client, "provider", "") or "主模型")
    yield {"type": "tool_call", "run_id": state.run_id, "name": SOURCE, "args": {"model": model}}
    started = time.monotonic()
    metadata = {}
    try:
        result = complete_model(client, prompt, agent.max_new_tokens)
        metadata = dict(getattr(result, "metadata", None) or {})
        record_call(agent, metadata, source=SOURCE, client=client)
        verdict, objections, reason = parse_review(getattr(result, "text", ""))
    except Exception as exc:  # 评审只能否决：provider 出错不能让一个检查已通过的目标失败
        verdict, objections, reason = "accept", [], f"评审调用失败：{clip(str(exc), 160)}"
    duration_ms = int((time.monotonic() - started) * 1000)
    review = {
        "round": int(goal["rounds"][-1]["round"]),
        "verdict": verdict,
        "objections": objections,
        "reason": reason,
        "model": model,
        "duration_ms": duration_ms,
        "files_clipped": clipped,
        # bench（无人值守）用它判断“否决时代码本来能不能过”；交互使用时不额外做快照
        "tree": _snapshot_tree(agent) if verdict == VERDICT_OBJECT and getattr(agent, "unattended", False) else "",
        "input_tokens": metadata.get("input_tokens"),
        "output_tokens": metadata.get("output_tokens"),
    }
    goal.setdefault("reviews", []).append(review)
    agent.emit_trace(state, "goal_review", {**{key: value for key, value in review.items() if key != "tree"}, "completion_metadata": metadata})
    yield {
        "type": "tool_result",
        "run_id": state.run_id,
        "name": SOURCE,
        "content": render_review(review),
        "metadata": {"tool_status": "error" if verdict == VERDICT_OBJECT else "ok"},
        "duration_ms": duration_ms,
        "output_tokens": metadata.get("output_tokens"),
    }
    return review


def veto_message(goal, number, shown):
    """否决后下一轮的续接消息：带上评审意见，而不是“检查失败”的输出（那一次检查是通过的）。"""
    review = next((item for item in reversed(goal.get("reviews") or []) if item.get("verdict") == VERDICT_OBJECT), {})
    return VETO_MESSAGE.format(
        objective=goal["objective"],
        number=shown,
        limit=goal["max_rounds"],
        command=" && ".join(goal["checks"]),
        items=_fenced(format_objections(review.get("objections") or [])),
    )


def format_objections(objections):
    return "\n".join(
        f"{index}. [{item['file']}] {item['issue']} (evidence: {item['evidence']})" for index, item in enumerate(objections, 1)
    )


def render_review(review):
    """卡片正文；✓/✗ 由界面按 tool_status 加。"""
    seconds = f"{review['duration_ms'] / 1000:.1f}s"
    if review["verdict"] == VERDICT_OBJECT:
        lines = [f"评审提出 {len(review['objections'])} 条意见，目标多跑一轮（{seconds}）"]
        lines.extend(f"- {item['file']}：{item['issue']}" for item in review["objections"])
        return "\n".join(lines)
    if review.get("reason"):
        return f"评审未给出有效结论，按通过处理：{review['reason']}（{seconds}）"
    return f"评审通过（{seconds}）"


def _changed_files(agent, paths):
    """目标期间改过的文件的当前内容：每个最多 MAX_FILE_CHARS 字，总共 MAX_TOTAL_CHARS 字。返回 (文本, 是否截断)。"""
    parts, total, clipped = [], 0, False
    for path in paths:
        target = agent.root / path
        try:
            data = target.read_bytes() if target.is_file() else None
        except OSError:
            data = None
        text = text_or_none(data) if data is not None else None
        body = "(deleted)" if data is None else ("(binary)" if text is None else agent.redact_text(text))
        if len(body) > MAX_FILE_CHARS:
            body, clipped = body[:MAX_FILE_CHARS] + f"\n[... clipped {len(body) - MAX_FILE_CHARS} chars]", True
        block = f"=== {path} ===\n```\n{_fenced(body)}\n```\n"
        if total + len(block) > MAX_TOTAL_CHARS:
            parts.append(f"[... {len(paths) - len(parts)} more changed files not shown]\n")
            return "".join(parts), True
        parts.append(block)
        total += len(block)
    return "".join(parts), clipped


def _fenced(text):
    """仓库内容：转义冒充对话结构的行，并且不让它提前闭合代码块。"""
    return fence(str(text or "").replace("```", "ˋˋˋ")) or "(no output)"


def _snapshot_tree(agent):
    store = ShadowStore.for_root(agent.root)
    return (store.snapshot() or "") if store.available else ""


def review_summary(goal):
    """`-p` JSON / bench 用：每次评审的结论和意见。"""
    return [
        {key: item.get(key) for key in ("round", "verdict", "reason", "model", "objections")}
        for item in goal.get("reviews") or []
    ]
