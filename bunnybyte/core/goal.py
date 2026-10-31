"""目标模式：一个跨多轮的目标，由运行时自己跑检查命令判断做没做完。

`/goal` 或 `bunny -p --goal` 给出目标和检查命令后，驱动器一轮接一轮地调用 Engine.run_turn：
开始前先跑一次检查（必须是失败的，否则它分不出做没做完），每轮结束后运行时亲自再跑，通过才算达成；
失败就把输出带进下一轮。为了不在便宜模型上空烧钱，轮数、花费、“两轮之间什么都没变”和
“一轮没调用工具”都会让它停下。状态存在会话里，--resume 之后用 /goal resume 接着做。
"""

import re
from dataclasses import replace

from .cost_meter import STOP_REASON_BUDGET_EXCEEDED
from .fix_proof import is_test_path
from .pricing import format_usd
from .task_state import STATUS_FAILED, STATUS_RUNNING
from .transcript_fence import fence
from .verification import (
    DEFAULT_TIMEOUT,
    STATUS_FAILED as CHECK_FAILED,
    STATUS_PASSED,
    STATUS_SKIPPED,
    STATUS_TIMEOUT,
    VerifyConfig,
    changes_fingerprint,
    run_command,
)
from .workspace import clip, now

GOAL_SOURCE = "goal"
PURSUING, PAUSED, ACHIEVED, UNMET, BUDGET_LIMITED = "pursuing", "paused", "achieved", "unmet", "budget_limited"
STATUS_LABELS = {PURSUING: "进行中", PAUSED: "已暂停", ACHIEVED: "已达成", UNMET: "未达成", BUDGET_LIMITED: "预算用完"}
DEFAULT_ROUNDS = 5
MAX_ROUNDS = 20
MAX_PATHS = 200
NOISE = (
    (re.compile(r"\d+(?:\.\d+)?\s*(?:ms|s|秒|seconds?)(?![A-Za-z])"), "<t>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "<addr>"),
    (re.compile(r"(?:/private)?/(?:tmp|var/folders)/\S+"), "<tmp>"),
)
OPTION_RE = re.compile(r"(?:^|\s)--(check|rounds|max-cost)(?:=|\s+)(\"[^\"]*\"|'[^']*'|`[^`]*`|\S+)")

FIRST_ROUND_NOTE = (
    "\n\n[Goal mode] After this turn the runtime itself runs `{command}`; the goal counts as done only when it "
    "passes. Before you started it {status}; run it yourself to see the output. If the task is too large for one "
    "turn, make as much progress as you can; the runtime continues in a new turn. Do not edit the check to make it pass."
)
CONTINUE_MESSAGE = (
    "{objective}\n\n[Goal mode, round {number}/{limit}: this message was written by the runtime, not typed by the "
    "user.] After your previous turn the runtime ran `{command}`: {status} (exit {code}). Output tail:\n```\n{tail}\n```\n"
    "Continue working toward the goal above. The runtime re-runs the check when this turn ends; only a pass counts. "
    "Do not edit the check to make it pass."
)


def parse_goal_args(text):
    """`--check CMD --rounds N --max-cost USD 目标文字`（选项位置随意，带空格的命令用引号）。返回 dict 或错误文字。"""
    options = {}
    for name, value in OPTION_RE.findall(str(text or "")):
        options[name] = value[1:-1] if value[:1] in "\"'`" and value[-1:] == value[:1] and len(value) > 1 else value
    objective = OPTION_RE.sub(" ", str(text or "")).strip()
    try:
        rounds = int(options["rounds"]) if "rounds" in options else None
        max_cost = float(options["max-cost"]) if "max-cost" in options else None
    except ValueError:
        return "--rounds 要整数，--max-cost 要美元金额"
    if rounds is not None and not 1 <= rounds <= MAX_ROUNDS:
        return f"--rounds 应在 1 到 {MAX_ROUNDS} 之间"
    if max_cost is not None and not max_cost > 0:
        return "--max-cost 必须大于 0"
    return {"objective": objective, "check": options.get("check", "").strip(), "rounds": rounds, "max_cost": max_cost}


def start_goal(agent, objective, *, check="", max_rounds=None, max_cost=None):
    """设定目标并标记待驱动；返回 (goal, 错误文字)。显式写的检查命令是用户亲手给的，本进程内不再询问。"""
    objective = str(objective or "").strip()
    if not objective:
        return None, "请写出目标，例如 `/goal --check \"pytest -q\" 让所有测试通过`"
    if agent.read_only or agent.runtime_mode == "plan":
        return None, "只读 / 计划模式下不能开始目标"
    config = getattr(agent, "verify_config", None)
    checks = [check] if check else list(getattr(config, "commands", ()) or ())
    if not checks:
        return None, '没有可运行的检查：用 `--check "命令"` 指定（例如 `pytest -q`），或在 .bunnybyte.toml 的 [verify] commands 里配置'
    agent.session["goal"] = {
        "objective": objective,
        "checks": checks,
        "status": PAUSED,
        "reason": "",
        "max_rounds": int(max_rounds or DEFAULT_ROUNDS),
        "max_cost": max_cost,
        "cost_at_start": _session_usd(agent),
        "round_base": 0,
        "baseline": {},
        "rounds": [],
        "paths": [],
        "created_at": now(),
    }
    agent._goal_approved = tuple(checks) if check else None
    agent._goal_pending = True
    _save(agent)
    return agent.session["goal"], ""


def goal_state(agent):
    goal = agent.session.get("goal")
    if not isinstance(goal, dict):
        return None
    if goal.get("status") == PURSUING and not getattr(agent, "_goal_driving", False):
        goal.update(status=PAUSED, reason="上次运行在目标进行中退出")  # 进程被杀 / 崩溃：别假装还在跑
        _save(agent)
    return goal


def resume_goal(agent):
    goal = goal_state(agent)
    if goal is None:
        return "当前会话没有目标。用 `/goal --check \"命令\" <目标>` 设定一个。"
    if goal["status"] == ACHIEVED:
        return "这个目标已经达成。设定新目标会替换它。"
    goal.update(round_base=len(goal["rounds"]), reason="")
    if goal["status"] == BUDGET_LIMITED:  # 预算用完后再 resume = 同意再花一份同样的预算
        goal["cost_at_start"] = _session_usd(agent)
    agent._goal_pending = True
    _save(agent)
    return ""


def clear_goal(agent):
    had = agent.session.pop("goal", None) is not None
    agent._goal_pending = False
    _save(agent)
    return had


def take_goal_drive(agent):
    """前端（REPL / TUI / -p）在命令处理完后调用：有待驱动的目标就返回 True 并清掉标记。"""
    pending = bool(getattr(agent, "_goal_pending", False))
    agent._goal_pending = False
    return pending and isinstance(agent.session.get("goal"), dict)


def drive_goal(agent, mention_text=None):
    """生成器：产出与普通回合相同的事件，外加 goal_check 卡片和 runtime_notice 行。

    mention_text：用户亲手输入的目标文字（-p 参数），第一轮照常预读其中的 `@path`。
    """
    goal = goal_state(agent)
    if goal is None:
        return
    agent._goal_driving = True
    try:
        yield from _drive(agent, goal, mention_text)
    finally:
        agent._goal_driving = False
        if goal.get("status") == PURSUING:  # 异常 / Ctrl-C / 生成器被关闭：别留下“进行中”
            goal.update(status=PAUSED, reason="目标被中断")
        _save(agent)


def _drive(agent, goal, mention_text=None):
    if not _approved(agent, goal):
        yield _notice(_finish(agent, goal, PAUSED, "检查命令没有获准运行"))
        return
    goal.update(status=PURSUING, reason="")
    _save(agent)
    if not goal.get("baseline"):
        yield _notice(f"🎯 目标模式：先运行一次检查 `{_command_text(goal)}`，确认它现在没通过")
        receipt = yield from _run_checks(agent, goal, "")
        if agent.abort_requested or receipt.get("aborted"):
            agent.abort_requested = False  # 没跑完的基线不保存，resume 时重跑
            yield _notice(_finish(agent, goal, PAUSED, "已停止"))
            return
        refusal = _baseline_refusal(receipt)
        if refusal:  # 不保存被拒的基线：/goal resume 会重新检查，而不是直接开始花钱
            yield _notice(_finish(agent, goal, UNMET, refusal))
            return
        goal["baseline"] = _brief(receipt)
    while True:
        if agent.abort_requested:
            agent.abort_requested = False
            yield _notice(_finish(agent, goal, PAUSED, "已停止（Esc / Stop）"))
            return
        if goal.get("max_cost") and _spent(agent, goal) >= goal["max_cost"]:
            yield _notice(_finish(agent, goal, BUDGET_LIMITED, f"目标花费已到上限 {format_usd(goal['max_cost'])}"))
            return
        done = len(goal["rounds"]) - int(goal.get("round_base", 0))
        number = len(goal["rounds"]) + 1
        yield _notice(f"🎯 目标 第 {done + 1}/{goal['max_rounds']} 轮")
        if agent.abort_requested:  # 提示显示时按的 Esc：run_turn 一开始会清掉这个标记，必须在这里接住
            agent.abort_requested = False
            yield _notice(_finish(agent, goal, PAUSED, "已停止（Esc / Stop）"))
            return
        state = yield from _run_round(agent, goal, _round_message(goal, number, done + 1), mention_text if number == 1 else None)
        verdict = yield from _after_round(agent, goal, state, number)
        if verdict:
            yield _notice(verdict)
            return


def _after_round(agent, goal, state, number):
    """生成器：检查本轮结果并记账；返回非空字符串表示目标停在这里（给用户的说明）。"""
    if state is None or state.status == STATUS_RUNNING:
        return _finish(agent, goal, PAUSED, "这一轮没有正常结束")
    _add_paths(goal, state.changed_paths)
    entry = {"round": number, "run_id": state.run_id, "tool_steps": state.tool_steps, "stop_reason": state.stop_reason}
    if state.stop_reason == "aborted" or agent.abort_requested:
        agent.abort_requested = False
        goal["rounds"].append(entry)
        return _finish(agent, goal, PAUSED, "已停止（Esc / Stop）")
    failed = state.status == STATUS_FAILED
    receipt = _gate_receipt(agent, state, goal["checks"])
    if receipt is None:
        receipt = yield from _run_checks(agent, goal, state.run_id)
    else:
        yield _notice(f"目标检查沿用本轮验证闸门的结果：{receipt['status']}（exit {receipt['exit_code']}）")
    entry.update(check=_brief(receipt), fingerprint=changes_fingerprint(agent.root, goal["paths"]), cost_usd=round(_spent(agent, goal), 6))
    previous = _last_checked(goal)
    goal["rounds"].append(entry)
    agent.emit_trace(state, "goal_check", {"round": number, "command": receipt["command"], "status": receipt["status"], "exit_code": receipt["exit_code"], "reused": bool(receipt.get("reused"))})
    _save(agent)
    if agent.abort_requested or entry["check"].get("aborted"):
        agent.abort_requested = False
        return _finish(agent, goal, PAUSED, "检查运行时被停止")
    if receipt["status"] == STATUS_PASSED:
        return _finish(agent, goal, ACHIEVED, "")
    if failed:  # 模型 / provider 出错：检查照跑（也许已经改好了），没通过就暂停而不是接着花钱
        return _finish(agent, goal, PAUSED, f"这一轮出错（{state.stop_reason}），处理好后用 /goal resume 继续")
    if receipt["status"] not in (CHECK_FAILED, STATUS_TIMEOUT):
        return _finish(agent, goal, UNMET, f"检查结果是 {receipt['status']}（{clip(receipt['output_tail'], 120)}），不能据此判断")
    if state.stop_reason == STOP_REASON_BUDGET_EXCEEDED:
        return _finish(agent, goal, BUDGET_LIMITED, "花费到了上限")
    if state.tool_steps == 0:
        return _finish(agent, goal, PAUSED, "这一轮模型没有调用任何工具，继续下去只会空转")
    if previous and previous.get("fingerprint") == entry["fingerprint"] and previous["check"].get("tail_key") == entry["check"]["tail_key"]:
        return _finish(agent, goal, UNMET, "连续两轮改动和检查输出都没有变化，没有进展")
    if len(goal["rounds"]) - int(goal.get("round_base", 0)) >= goal["max_rounds"]:
        return _finish(agent, goal, UNMET, f"用完了 {goal['max_rounds']} 轮，检查仍未通过")
    return ""


def _run_round(agent, goal, message, mention_text=None):
    """生成器：跑一轮。期间验证闸门改为自动运行目标检查（回合内的修复轮就针对它），花费上限收紧到目标剩余预算。"""
    config, meter = getattr(agent, "verify_config", None), agent.cost_meter
    limit, source = meter.limit, getattr(agent, "turn_source", "")
    checks = tuple(goal["checks"])
    agent.verify_config = replace(config, mode="auto", commands=checks) if config else VerifyConfig(mode="auto", commands=checks, fix_proof="off")
    if goal.get("max_cost"):
        remaining = max(goal["max_cost"] - _spent(agent, goal), 0.0)
        meter.limit = remaining if limit is None else min(limit, remaining)
    if goal["rounds"]:
        agent.turn_source = GOAL_SOURCE  # 续接消息是运行时写的：压缩摘要、用户消息列表不把它当成用户说的话
    try:
        yield from agent.engine.run_turn(message, mention_text=mention_text)
    finally:
        agent.verify_config, meter.limit, agent.turn_source = config, limit, source
    return getattr(agent, "current_task_state", None)


def _round_message(goal, number, shown):
    if number == 1:
        base = goal.get("baseline") or {}
        status = "failed (exit {})".format(base.get("exit_code")) if base.get("status") == CHECK_FAILED else str(base.get("status", "failed"))
        return goal["objective"] + FIRST_ROUND_NOTE.format(command=_command_text(goal), status=status)
    check = (_last_checked(goal) or {}).get("check") or goal.get("baseline") or {}
    return CONTINUE_MESSAGE.format(
        objective=goal["objective"],
        number=shown,
        limit=goal["max_rounds"],
        command=check.get("command", _command_text(goal)),
        status=check.get("status", ""),
        code=check.get("exit_code"),
        tail=_quoted_tail(check.get("tail")),
    )


def _run_checks(agent, goal, run_id):
    config = getattr(agent, "verify_config", None)
    timeout = int(getattr(config, "timeout", 0) or DEFAULT_TIMEOUT)
    receipt = None
    for command in goal["checks"]:
        yield {"type": "tool_call", "run_id": run_id, "name": "goal_check", "args": {"command": command}}
        receipt = run_command(agent, command, timeout)
        receipt["aborted"] = receipt["status"] == STATUS_SKIPPED and bool(agent.abort_requested)
        head = f"{receipt['status']} (exit {receipt['exit_code']}, {receipt['duration_ms'] / 1000:.1f}s)"
        yield {
            "type": "tool_result",
            "run_id": run_id,
            "name": "goal_check",
            "content": head if receipt["status"] == STATUS_PASSED else f"{head}\n{clip(receipt['output_tail'], 800)}",
            "metadata": {"tool_status": "ok" if receipt["status"] == STATUS_PASSED else "error"},
            "duration_ms": receipt["duration_ms"],
        }
        if receipt["status"] != STATUS_PASSED or agent.abort_requested:
            break
    return receipt


def _gate_receipt(agent, state, checks):
    """本轮验证闸门已经跑过目标检查、且之后文件没再变：直接用它的结果，同一套测试不跑两遍。"""
    verification = getattr(state, "verification", None) or {}
    receipts = list(verification.get("receipts") or [])
    if not receipts or not state.changed_paths:
        return None
    if verification.get("fingerprint") != changes_fingerprint(agent.root, state.changed_paths):
        return None
    current = [item for item in receipts if item.get("round") == receipts[-1].get("round")]
    commands = [item.get("command") for item in current]
    if commands != list(checks)[: len(commands)]:
        return None
    last = current[-1]
    if last.get("status") not in (STATUS_PASSED, CHECK_FAILED, STATUS_TIMEOUT):
        return None
    if last.get("status") == STATUS_PASSED and len(current) < len(checks):
        return None
    return dict(last, reused=True)


def _baseline_refusal(receipt):
    status = receipt["status"]
    if status == STATUS_PASSED:
        return "开始前检查就已经通过：它分不出做没做完。请换一个现在会失败的检查（--check）"
    if status == STATUS_SKIPPED:
        return "检查没有跑到任何测试，不能用来判断目标"
    if status not in (CHECK_FAILED, STATUS_TIMEOUT):
        return f"检查命令无法运行（{clip(receipt['output_tail'], 160)}），请确认命令可用"
    return ""


def _quoted_tail(text):
    """检查输出来自仓库代码：转义冒充对话结构的行，并且不让它提前闭合代码块。"""
    return fence(str(text or "").replace("```", "ˋˋˋ")) or "(no output)"


def normalized_tail(text):
    """比较“检查输出有没有变”时去掉耗时、内存地址和临时路径，否则每轮都不一样。"""
    text = str(text or "")
    for pattern, replacement in NOISE:
        text = pattern.sub(replacement, text)
    return " ".join(text.split())


def _brief(receipt):
    return {
        "command": receipt["command"],
        "status": receipt["status"],
        "exit_code": receipt["exit_code"],
        "duration_ms": receipt["duration_ms"],
        "tail": receipt["output_tail"],
        "tail_key": normalized_tail(receipt["output_tail"]),
        "reused": bool(receipt.get("reused")),
        "aborted": bool(receipt.get("aborted")),
    }


def _finish(agent, goal, status, reason):
    goal.update(status=status, reason=reason, finished_at=now())
    _save(agent)
    spent = format_usd(_spent(agent, goal))
    used = len(goal["rounds"]) - int(goal.get("round_base", 0))
    if status == ACHIEVED:
        text = f"🎯 目标已达成：`{_command_text(goal)}` 通过，用了 {used} 轮 · 估算 {spent}"
        touched = check_files_changed(goal)
        if touched:
            text += f"\n⚠ 目标期间改过检查涉及的文件：{'、'.join(touched[:5])}——请确认没有为了通过而改检查"
        return text
    return f"🎯 目标{STATUS_LABELS[status]}：{reason}（{used} 轮 · 估算 {spent}）。用 /goal resume 继续，/goal clear 放弃。"


def check_files_changed(goal):
    commands = " ".join(goal.get("checks") or [])
    return [path for path in goal.get("paths") or [] if is_test_path(path) or path in commands]


def _last_checked(goal):
    return next((item for item in reversed(goal["rounds"]) if item.get("check")), None)


def _approved(agent, goal):
    checks = tuple(goal["checks"])
    if getattr(agent, "_goal_approved", None) == checks or agent.approval_policy == "auto":
        return True
    if agent.approval_policy == "never":
        return False
    if agent.approve("verify", {"commands": list(checks), "note": "目标模式：开始前和每轮结束后运行这些检查"}):
        agent._goal_approved = checks
        return True
    return False


def _add_paths(goal, paths):
    for path in paths or []:
        if path not in goal["paths"] and len(goal["paths"]) < MAX_PATHS:
            goal["paths"].append(path)


def _session_usd(agent):
    return float(agent.cost_meter.session_totals.get("usd") or 0.0)


def _spent(agent, goal):
    return max(_session_usd(agent) - float(goal.get("cost_at_start") or 0.0), 0.0)


def _command_text(goal):
    return " && ".join(goal.get("checks") or [])


def _notice(text):
    return {"type": "runtime_notice", "run_id": "", "content": text}


def _save(agent):
    agent.session_path = agent.session_store.save(agent.session)


def goal_summary(agent):
    """`bunny -p` JSON 里的 goal 块。"""
    goal = agent.session.get("goal")
    if not isinstance(goal, dict):
        return {}
    return {
        "objective": goal["objective"],
        "status": goal["status"],
        "reason": goal.get("reason", ""),
        "checks": list(goal["checks"]),
        "rounds": len(goal["rounds"]),
        "max_rounds": goal["max_rounds"],
        "cost_usd": round(_spent(agent, goal), 6),
        "baseline": {key: goal.get("baseline", {}).get(key) for key in ("status", "exit_code")},
        "receipts": [
            {"round": item["round"], "run_id": item.get("run_id", ""), "tool_steps": item.get("tool_steps", 0), "stop_reason": item.get("stop_reason", ""), **{key: (item.get("check") or {}).get(key) for key in ("status", "exit_code", "reused")}}
            for item in goal["rounds"]
        ],
        "check_files_changed": check_files_changed(goal),
    }


def render_goal_status(agent):
    goal = goal_state(agent)
    if goal is None:
        return "## 目标\n\n当前会话没有目标。`/goal --check \"pytest -q\" <目标>` 设定一个：每轮结束后由运行时跑检查，通过才算完成。"
    lines = [
        "## 目标",
        "",
        f"- 目标：{goal['objective']}",
        f"- 检查：`{_command_text(goal)}`",
        f"- 状态：{STATUS_LABELS.get(goal['status'], goal['status'])}" + (f"（{goal['reason']}）" if goal.get("reason") else ""),
        f"- 轮数：{len(goal['rounds'])}（每次最多 {goal['max_rounds']} 轮）· 估算花费 {format_usd(_spent(agent, goal))}" + (f" / 上限 {format_usd(goal['max_cost'])}" if goal.get("max_cost") else ""),
    ]
    for item in goal["rounds"][-8:]:
        check = item.get("check") or {}
        lines.append(f"  - 第 {item['round']} 轮：{item.get('tool_steps', 0)} 步 · {item.get('stop_reason', '')} · 检查 {check.get('status', '未运行')}")
    lines += ["", "`/goal resume` 继续 · `/goal clear` 放弃"]
    return "\n".join(lines)
