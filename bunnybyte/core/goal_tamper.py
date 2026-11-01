"""目标模式的测试篡改守卫：检查通过时，对比的是目标开始时的快照，而不是只看最后一轮。

一轮删掉失败的测试、下一轮拿到“通过”，单轮扫描两次都只看到一半；这里在宣布达成之前拿
`start_goal` 记下的 tree 和当前工作区比一次（目标期间先加后删的测试互相抵消），再加一条只有目标模式才有的检查：
检查命令里点名的非测试文件（`python check.py` 的 check.py、Makefile、tox.ini）被改过或删掉。

发现可疑改动时先追加一轮（整个目标一次）：把目标开始时的原始代码交给模型，让它恢复测试、改代码去满足；
测试真的和目标矛盾时保持原样并说明理由。这一轮之后仍然可疑：warn 照常达成但带 ⚠，block 判为未达成
（`bunny -p --goal` 退出码 4）。预算不够再跑一轮、或已按 Esc 时不追加，直接按模式收尾。
"""

import ast
import shlex
from pathlib import PurePosixPath

from .goal_review import BUDGET_MARGIN
from .shadow_snapshots import _LOCK, ShadowError, ShadowStore
from .tamper_guard import INFO, SKIPPED, SUSPICIOUS, WARN, _assertions, _finding, _is_test_file, _neutral, _tree, describe, scan_trees, tamper_mode, tamper_summary
from .task_state import STATUS_FAILED
from .transcript_fence import fence
from .turn_diff import text_or_none

ORIGINAL_CHARS = 4000
ORIGINAL_FILE_CHARS = 2000

ESCALATION_MESSAGE = (
    "{objective}\n\n[Goal mode, extra round: this message was written by the runtime, not typed by the user.] "
    "After your previous turn the check `{command}` passed, but compared with the start of the goal these changes may "
    "have weakened the tests or the check:\n```\n{items}\n```\n"
    "A pass obtained this way does not count. Restore these tests and the check to what they were (the original code "
    "is below) and change the code so that it satisfies them. If a test really contradicts the goal, leave it exactly "
    "as it was originally, do not delete, skip or loosen it, and say in your final answer which test and why. When "
    "this turn ends the runtime re-runs the check and compares the tests with the start of the goal again.\n\n"
    "Original code at the start of the goal:\n```\n{originals}\n```"
)


def start_tree(agent):
    """`start_goal` 时的工作区 tree；影子快照不可用时为空串（之后的扫描记为跳过）。"""
    store = ShadowStore.for_root(agent.root)
    return (store.snapshot() or "") if store.available else ""


def guard_pass(agent, goal, entry, state):
    """生成器：检查通过后调用。返回 None = 照常评审 / 达成；否则 (状态, 说明)：状态 None = 已安排追加一轮，
    "paused" = 恢复测试的要求已记下、等 /goal resume（provider 出错或按了 Esc，不在这时接着花钱），"unmet" = 停在这里。"""
    mode = tamper_mode(getattr(agent, "verify_config", None))
    if mode == "off":
        return None
    result = scan_goal(agent, goal)
    result.update(mode=mode, round=entry["round"], escalated=bool(goal.get("tamper_escalated")))
    goal["tamper"] = result
    agent.emit_trace(state, "goal_tamper", {key: value for key, value in result.items() if key != "originals"})
    if result["status"] != SUSPICIOUS:
        return None
    text = "⚠ 检查通过了，但和目标开始时相比测试或检查可能被削弱（" + _warn_text(result) + "）"
    if state.status == STATUS_FAILED or agent.abort_requested:
        # provider 出错或按了 Esc：模型还没机会回应恢复要求，先暂停（不接着花钱，也不下结论），resume 时再发
        agent.abort_requested = False
        if not goal.get("tamper_escalated"):
            result["originals"] = _originals(agent, goal, result)
        goal["tamper_escalated"] = entry["tamper_escalated"] = True
        yield _notice(text + "：恢复测试的要求已记下")
        why = "已停止（Esc / Stop）" if state.status != STATUS_FAILED else f"这一轮出错（{state.stop_reason}）"
        return "paused", f"{why}；检查通过但测试可能被削弱，/goal resume 会先请模型恢复测试"
    skip = None if goal.get("tamper_escalated") else _escalation_skip(goal)
    if not goal.get("tamper_escalated") and not skip:
        result["originals"] = _originals(agent, goal, result)
        goal["tamper_escalated"] = True
        entry["tamper_escalated"] = True  # 续接消息（包括 Esc / --resume 之后）按这个重发“恢复测试”的要求
        yield _notice(text + "：追加一轮，请模型恢复后再让检查通过")
        return None, ""
    if skip:
        result["escalation_skipped"] = skip
    if mode == "block":
        result["blocked"] = True
        return "unmet", "检查靠削弱测试才通过：" + _warn_text(result) + ("（追加一轮后仍未恢复，模型的说明见最终回答）" if goal.get("tamper_escalated") else f"（{skip}，没有追加恢复测试的一轮）")
    return None


def scan_goal(agent, goal):
    """目标开始时的 tree → 当前工作区：返回 tamper_guard 同样格式的结果，外加 check_file_changed。"""
    store = ShadowStore.for_root(agent.root)
    start = str(goal.get("start_tree") or "")
    if not store.available:
        result = {"status": SKIPPED, "reason": f"影子快照不可用（{store.disabled_reason}）", "findings": []}
    elif not start:
        result = {"status": SKIPPED, "reason": "目标开始时没有快照", "findings": []}
    elif not store.has_tree(start):
        result = {"status": SKIPPED, "reason": "目标开始时的快照已不存在", "findings": []}
    else:
        after = store.snapshot()
        if not after:
            result = {"status": SKIPPED, "reason": "拍不了当前快照", "findings": []}
        else:
            result = scan_trees(store, start, after, scope="目标期间")
            try:
                _check_files(result, goal, store, store.diff(start, after))
            except (ShadowError, OSError, ValueError, KeyError) as exc:
                result.setdefault("check_file_error", f"{type(exc).__name__}: {exc}"[:200])
            return result
    # 没有快照：只知道本目标改过哪些路径、不知道内容，点名的文件只提示
    named = check_file_paths(goal, [("M", path) for path in goal.get("paths") or []])
    if named:
        result.setdefault("findings", []).append(_finding("check_file_changed", INFO, named, "无法对比内容"))
    return result


def _check_files(result, goal, store, diff):
    """检查命令点名的非测试文件：删掉了算可疑；.py 改过时只有断言变少 / 被架空才算（`python calc.py` 自测的 calc.py
    正是要修的源码，改它的实现不是削弱）；Makefile、脚本之类别的文件改过就算。"""
    items = {item["path"]: item for item in diff}
    named = check_file_paths(goal, [(item["status"], item["path"]) for item in diff])
    py = [items[path] for path in named if items[path]["status"] == "M" and path.endswith(".py")]
    oids = [item[key] for item in py for key in ("old_oid", "oid")]
    with _LOCK:
        blobs = store._read_blobs(oids) if oids else {}
    warn, info = [], []
    for path in named:
        item = items[path]
        if item in py:
            old, new = _tree(text_or_none(blobs.get(item["old_oid"]))), _tree(text_or_none(blobs.get(item["oid"])))
            weaker = old is not None and (new is None or len(_assertions(new)) < len(_assertions(old)) or _neutral(new) > _neutral(old))
            (warn if weaker else info).append(path)
        else:
            warn.append(path)
    if warn:
        result.setdefault("findings", []).append(_finding("check_file_changed", WARN, warn, "删掉了、断言变少或被架空" if any(p.endswith(".py") for p in warn) else ""))
        result["status"] = SUSPICIOUS
    if info:
        result.setdefault("findings", []).append(_finding("check_file_changed", INFO, info, "断言没有变少"))


def check_file_paths(goal, changed):
    """检查命令里点名的非测试文件中，目标期间被改过或删掉的。测试文件交给按 AST 的扫描（新增测试不算问题）。"""
    tokens = set()
    for command in goal.get("checks") or []:
        try:
            words = shlex.split(command)
        except ValueError:
            words = command.split()
        for word in words:
            tokens.add(word.split("=", 1)[-1].removeprefix("./").split("::", 1)[0])
    found = []
    for status, path in changed:
        if status not in "MD" or _is_test_file(path) or PurePosixPath(path).name == "conftest.py":
            continue
        if path in tokens or any(token.endswith("/" + path) for token in tokens):
            found.append(path)
    return sorted(set(found))


def _escalation_skip(goal):
    if goal.get("max_cost"):
        spent = max(float(goal["rounds"][-1].get("cost_usd") or 0.0), 0.0)
        counted = len(goal["rounds"]) - int(goal.get("cost_round_base", 0))
        if goal["max_cost"] - spent < spent / max(counted, 1) * BUDGET_MARGIN:  # 和目标评审一样：给追加的一轮留 1.5 倍余量
            return "剩余预算不够再跑一轮"
    return None


def _warn_text(result):
    warn = [item for item in result.get("findings") or [] if item["severity"] == WARN]
    return "；".join(describe(item) for item in warn[:3]) + (f" 等 {len(warn)} 项" if len(warn) > 3 else "")


def _originals(agent, goal, result):
    """可疑文件在目标开始时的内容：点名的测试只取那几个函数，其余整个文件；总共最多 ORIGINAL_CHARS 字。
    非 git 仓库、没提交的测试被删之后，模型只有靠这段才能恢复。"""
    store = ShadowStore.for_root(agent.root)
    warn = [item for item in result.get("findings") or [] if item["severity"] == WARN]
    paths = list(dict.fromkeys(path for item in warn for path in item.get("paths") or []))
    names = {name for item in warn for name in item.get("names") or []}
    try:
        blobs = store.tree_blobs(goal["start_tree"])
        wanted = [blobs[path] for path in paths if path in blobs]
        with _LOCK:
            contents = store._read_blobs(wanted) if wanted else {}
    except (ShadowError, OSError, ValueError, KeyError):
        return ""
    parts, total = [], 0
    for path in paths:
        text = text_or_none(contents.get(blobs.get(path, "")))
        if text is None:
            continue
        body = _functions(text, names) or text
        if len(body) > ORIGINAL_FILE_CHARS:
            body = body[:ORIGINAL_FILE_CHARS] + f"\n[... {len(body) - ORIGINAL_FILE_CHARS} more chars]"
        block = f"=== {path} ===\n{body}\n"
        if total + len(block) > ORIGINAL_CHARS:
            parts.append(f"[... {len(paths) - len(parts)} more files not shown]\n")
            break
        parts.append(block)
        total += len(block)
    return agent.redact_text("".join(parts))


def _functions(text, names):
    """text 里名字在 names 中的顶层 / 类内函数（带装饰器）的源码；一个都没有时返回空串。"""
    if not names:
        return ""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return ""
    lines, found = text.splitlines(), []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            start = min([node.lineno] + [item.lineno for item in node.decorator_list])
            found.append((start, "\n".join(lines[start - 1 : node.end_lineno])))
    return "\n\n".join(source for _, source in sorted(found))


def escalation_message(goal):
    result = goal.get("tamper") or {}
    warn = [item for item in result.get("findings") or [] if item["severity"] == WARN]
    return ESCALATION_MESSAGE.format(
        objective=goal["objective"],
        command=" && ".join(goal.get("checks") or []),
        items=_fenced("\n".join(f"- {describe(item)}" for item in warn)),
        originals=_fenced(result.get("originals") or "(not available: restore them from version control)"),
    )


ROUND_NOTICE = "🎯 目标 追加一轮（恢复测试）"


def achieved_line(goal):
    """达成时附在结尾的一行：仍然可疑 → ⚠；扫描被跳过 → 说明原因。"""
    result = goal.get("tamper") or {}
    if result.get("status") == SUSPICIOUS:
        why = "追加一轮后仍未恢复" if result.get("escalated") else result.get("escalation_skipped", "")
        return f"⚠ 和目标开始时相比测试或检查可能被削弱：{_warn_text(result)}（{why}）——请确认是你要求的改动，模型的说明见最终回答"
    if result.get("status") == SKIPPED:
        return f"· 目标期间的测试篡改检查跳过：{result.get('reason', '')}"
    return ""


def blocked(goal):
    return bool((goal.get("tamper") or {}).get("blocked"))


def goal_tamper_summary(goal):
    result = goal.get("tamper") or {}
    if not result:
        return {}
    return {**tamper_summary(result), "escalated": bool(goal.get("tamper_escalated")), "blocked": blocked(goal), "reason": result.get("reason", "")}


def check_files_changed(goal):
    """兼容 0.15 的 `-p` JSON 字段，现在只列检查命令点名、被改过的非测试文件（测试文件的改动看 `tamper`）。"""
    findings = (goal.get("tamper") or {}).get("findings") or []
    return [path for item in findings if item.get("kind") == "check_file_changed" for path in item.get("paths") or []]


def _fenced(text):
    return fence(str(text or "").replace("```", "ˋˋˋ")) or "(none)"


def _notice(text):
    return {"type": "runtime_notice", "run_id": "", "content": text}
