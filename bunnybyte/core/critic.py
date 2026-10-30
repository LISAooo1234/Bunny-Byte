"""独立审查（critic）：模型交卷前，另起一次不带工具的模型调用，对照需求、项目规则和本轮 diff 挑错。

这里只放不依赖引擎的部分：`[critic]` 配置、审查提示词、审查结果的解析。接入回合循环的逻辑在 critic_gate.py。
测试闸门只能发现“测试会红”的问题；“测试全绿、需求漏了一半”（忘了补回归测试、忘了写 CHANGELOG、
改名时删掉了旧选项）要靠读需求的人来发现，这一步就是做这个的。
"""

import json
import re
from dataclasses import dataclass

CRITIC_MODES = ("off", "auto")
DEFAULT_MAX_DIFF_CHARS = 12000
MAX_OBJECTIONS = 5
FIELD_CHARS = 300
RULES_CHARS = 4000
REQUEST_CHARS = 4000
FINAL_CHARS = 3000
MAX_LISTED_FILES = 200

VERDICT_ACCEPT = "accept"
VERDICT_OBJECT = "object"

# 审查模型看不到文件全文、也跑不了命令；“无法验证”类意见是它自身的局限，不是改动的问题（实测这类反对全是多余的）
UNVERIFIABLE_RE = re.compile(
    r"\b(?:cannot|can't|could not|unable to)\s+(?:verify|confirm|check|determine)|no (?:test )?(?:verification|evidence|output) "
    r"(?:is )?(?:shown|provided)|无法(?:验证|确认|判断)|看不到",
    re.IGNORECASE,
)
FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

CRITIC_PROMPT = """You are a strict, independent code reviewer. Another coding agent has just worked on a task in a \
repository and wants to hand in the final answer shown at the end. You cannot run tools or read other files: judge \
only from what is shown here.

Work like a checklist. First list every explicit requirement from the user request and every project rule that \
applies to this kind of change (a bug fix, a renamed option, ...), and decide from the diff whether it is met. A \
requirement that needs a file the diff never touched (a new test, a changelog line, docs) is NOT met, whatever the \
final answer claims. Rules whose condition does not hold (for example "if the repo has a CHANGELOG" when the file \
list has none) do not apply. Then object to each unmet requirement and to any definite bug you can point to in the \
diff. Never object to style, naming, wording, nice-to-haves, or anything you cannot back with evidence. Testing \
and verification are handled by other checks (see "Checks" below): never object because test output, logs or proof \
are not shown, or because you cannot recompute a result yourself. Only a requirement to ADD or CHANGE a file (for \
example "add a regression test") can be unmet.

Reply with exactly one JSON object and nothing else:
{{"checklist": [{{"requirement": "<short>", "applies": true, "met": false, "evidence": "<what the diff shows or lacks>"}}], \
"verdict": "accept" or "object", \
"objections": [{{"file": "<path, or the missing file>", "issue": "<what is wrong>", "evidence": "<the requirement and \
what the diff shows or lacks>"}}]}}

## User request
{request}

## Project rules
{rules}

## Files in the repository (* = changed in this turn)
{files}

## Diff of this turn{truncated}
{diff}

## Checks (commands run this turn and their exit codes)
{checks}

## The agent's final answer
{final}
"""

REPAIR_MESSAGE = (
    "An independent reviewer compared your changes with the request and the project rules and objected before "
    "your answer was accepted:\n{items}\n\n"
    "Fix the points that are right with tools, then answer again. If an objection is wrong, do not edit for it; "
    "say briefly in your final answer why. {after}"
)


@dataclass(frozen=True)
class CriticConfig:
    mode: str = "off"
    profile: str = ""
    max_rounds: int = 1
    max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS

    @property
    def enabled(self):
        return self.mode != "off"


def resolve_critic_config(values, *, mode=None):
    """从合并后的配置字典解析 `[critic]`；mode 是命令行覆盖值。"""
    section = dict((values or {}).get("critic", {}) or {})
    resolved_mode = str(mode or section.get("mode") or "off")
    if resolved_mode not in CRITIC_MODES:
        raise ValueError(f"critic.mode must be one of {list(CRITIC_MODES)}")
    rounds = int(section.get("max_rounds", 1))
    diff_chars = int(section.get("max_diff_chars", DEFAULT_MAX_DIFF_CHARS))
    if not 1 <= rounds <= 3:
        raise ValueError("critic.max_rounds must be in [1, 3]")
    if not 1000 <= diff_chars <= 200_000:
        raise ValueError("critic.max_diff_chars must be in [1000, 200000]")
    return CriticConfig(
        mode=resolved_mode,
        profile=str(section.get("profile", "") or "").strip(),
        max_rounds=rounds,
        max_diff_chars=diff_chars,
    )


def _clip(text, limit):
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit] + f"\n[... clipped {len(text) - limit} chars]"


def build_prompt(*, request, rules, files, changed, diff, truncated, checks, final):
    changed = set(changed)
    # 改动的文件和根目录文件（CHANGELOG、README）排在最前：大仓库截断时，审查判断“仓库有没有 CHANGELOG”不能失真
    listed = sorted(set(files) | changed, key=lambda path: (path not in changed, "/" in path, path))
    shown = [("* " if path in changed else "  ") + path for path in listed[:MAX_LISTED_FILES]]
    if len(listed) > MAX_LISTED_FILES:
        shown.append(f"  ... and {len(listed) - MAX_LISTED_FILES} more files")
    return CRITIC_PROMPT.format(
        request=_clip(request, REQUEST_CHARS),
        rules=_clip(rules, RULES_CHARS) or "(none)",
        files="\n".join(shown) or "(unknown)",
        truncated=" (TRUNCATED: later parts are not shown, do not object to what you cannot see)" if truncated else "",
        diff=diff or "(empty)",
        checks=checks or "(none)",
        final=_clip(final, FINAL_CHARS),
    )


def _json_candidates(text):
    text = str(text or "").strip()
    yield text
    for block in FENCE_RE.findall(text):
        yield block.strip()
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        yield text[start : end + 1]


def parse_review(text):
    """返回 (verdict, objections, reason)。解析失败或没有证据的反对都当作通过，并写明原因。"""
    data = None
    for candidate in _json_candidates(text):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            break
        data = None
    if data is None:
        return VERDICT_ACCEPT, [], "审查回复不是有效 JSON，按通过处理"
    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict not in (VERDICT_ACCEPT, VERDICT_OBJECT):
        return VERDICT_ACCEPT, [], f"审查结论无法识别（{_clip(verdict, 40) or '空'}），按通过处理"
    objections = []
    raw = list(data.get("objections") or []) if verdict == VERDICT_OBJECT else []
    if not raw:  # 清单里自己标了“适用但没做到”，结论却写 accept：以清单为准
        raw = [
            {"file": item.get("file") or "(requirement)", "issue": item.get("requirement", ""), "evidence": item.get("evidence", "")}
            for item in data.get("checklist") or []
            if isinstance(item, dict) and item.get("applies") is True and item.get("met") is False
        ]
    if verdict == VERDICT_ACCEPT and not raw:
        return VERDICT_ACCEPT, [], ""
    for item in raw:
        if not isinstance(item, dict):
            continue
        entry = {key: _clip(item.get(key, ""), FIELD_CHARS) for key in ("file", "issue", "evidence")}
        if entry["file"] and entry["issue"] and entry["evidence"] and not UNVERIFIABLE_RE.search(entry["issue"]):
            objections.append(entry)
    if not objections:
        return VERDICT_ACCEPT, [], "审查提出反对但没有给出文件和证据（或只是说无法验证），按通过处理"
    return VERDICT_OBJECT, objections[:MAX_OBJECTIONS], ""


def repair_message(objections, reviews_left):
    items = "\n".join(
        f"{index}. [{item['file']}] {item['issue']} (evidence: {item['evidence']})"
        for index, item in enumerate(objections, 1)
    )
    after = (
        f"Your next answer will be reviewed again ({reviews_left} review(s) left)."
        if reviews_left
        else "Your next answer will not be reviewed again."
    )
    return REPAIR_MESSAGE.format(items=items, after=after)


def render_review(verdict, objections, reason, duration_ms):
    """卡片正文；✓/✗ 由界面按 tool_status 加，这里不再重复。"""
    seconds = f"{duration_ms / 1000:.1f}s"
    if verdict == VERDICT_OBJECT:
        lines = [f"审查提出 {len(objections)} 条意见，已退回给模型（{seconds}）"]
        lines.extend(f"- {item['file']}：{item['issue']}" for item in objections)
        return "\n".join(lines)
    if reason:
        return f"审查未给出有效结论，已放行：{reason}（{seconds}）"
    return f"审查通过（{seconds}）"
