"""/goal：设定一个跨多轮的目标，由运行时每轮结束后跑检查命令判断是否达成。"""

from __future__ import annotations

from ..core.goal import clear_goal, parse_goal_args, render_goal_status, resume_goal, start_goal

USAGE_LINES = (
    '`/goal --check "pytest -q" <目标>` — 设定目标并开始：每轮结束后运行时跑检查，通过才算达成',
    "`/goal --rounds N --max-cost USD ...` — 本次最多 N 轮（默认 5）、目标总花费上限",
    "`/goal` 或 `/goal status` — 查看目标状态和每轮结果",
    "`/goal resume` — 暂停 / 未达成之后接着做（Esc 会暂停）",
    "`/goal clear` — 放弃目标",
)


def handle_goal_command(agent, args):
    text = str(args or "").strip()
    word = text.split(maxsplit=1)[0].lower() if text else ""
    if word in {"", "status"} and len(text.split()) <= 1:
        return render_goal_status(agent)
    if word == "clear" and len(text.split()) == 1:
        return "## 目标\n\n已放弃目标。" if clear_goal(agent) else "## 目标\n\n当前会话没有目标。"
    if word == "resume" and len(text.split()) == 1:
        error = resume_goal(agent)
        return f"## 目标\n\n{error}" if error else "## 目标\n\n继续目标……"
    if word in {"help", "-h", "--help", "status", "clear", "resume", "pause"}:
        return _usage()  # `/goal resume now` 之类：不把多出来的字当成新目标
    parsed = parse_goal_args(text)
    if isinstance(parsed, str):
        return f"## 目标\n\n{parsed}\n\n{_usage()}"
    goal, error = start_goal(agent, parsed["objective"], check=parsed["check"], max_rounds=parsed["rounds"], max_cost=parsed["max_cost"])
    if error:
        return f"## 目标\n\n{error}"
    return f"## 目标\n\n开始：{goal['objective']}\n\n检查：`{' && '.join(goal['checks'])}` · 最多 {goal['max_rounds']} 轮。Esc 暂停。"


def _usage():
    return "## Usage\n\n" + "\n".join(f"- {line}" for line in USAGE_LINES)
