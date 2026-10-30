"""回合收尾前的独立审查闸门：验证闸门之后、on_final 钩子之前，让审查模型对照需求和本轮 diff 挑错。

审查是一次不带工具的模型调用（`[critic].profile` 指定的 provider，默认就是主模型）。有证据的反对意见
写回会话让模型修复，最多 `max_rounds` 轮；修复轮不占模型调用预算，但修复时的工具调用照常计入 max_steps；审查出错、回复不是 JSON 都按通过处理并记下原因。
"""

import difflib
import re
import time

from ..providers.base import complete_model
from .cost_meter import budget_stop_needed, record_call
from .critic import VERDICT_ACCEPT, VERDICT_OBJECT, build_prompt, parse_review, render_review, repair_message
from .project_rules import render_rules
from .shadow_snapshots import _LOCK, ShadowError, ShadowStore
from .tool_loop_guard import current_turn_history
from .verification import changes_fingerprint
from .workspace import clip, now

MIN_STEPS_LEFT = 3  # 修复要用工具步数；预算快用完时审查只会把一次成功收尾变成撞上限
MAX_REVIEWS_KEPT = 6
BINARY_SNIFF = 8000
MAX_CHECK_LINES = 10
EXIT_RE = re.compile(r"exit_code:\s*(-?\d+)")


def _gate_allowed(agent, config):
    return not (
        config is None
        or not config.enabled
        or agent.depth != 0
        or agent.read_only
        or agent.runtime_mode == "plan"
    )


def _text(data):
    if data is None:
        return None
    if b"\0" in data[:BINARY_SNIFF]:
        return None
    return data.decode("utf-8", "replace")


def _unified(path, old, new):
    old_text, new_text = _text(old), _text(new)
    if (old is not None and old_text is None) or (new is not None and new_text is None):
        return f"--- {path}\n(binary file changed)\n"
    lines = difflib.unified_diff(
        (old_text or "").splitlines(keepends=True),
        (new_text or "").splitlines(keepends=True),
        fromfile="/dev/null" if old is None else f"a/{path}",
        tofile="/dev/null" if new is None else f"b/{path}",
        n=3,
    )
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)


def _turn_entry(agent):
    turn_id = str(getattr(agent, "current_turn_id", "") or "")
    for entry in reversed(agent.session.get("undo_log") or []):
        if entry.get("turn_id") == turn_id:
            return entry
    return None


def _shadow_diff(agent):
    """本轮改动的 unified diff：本轮第一个快照（回合开始前）对比此刻磁盘上的内容。

    返回 (diff 文本, 仓库文件列表, 此刻的快照 tree)；快照不可用时返回 None，由调用方退回直接读文件。
    """
    entry = _turn_entry(agent)
    store = ShadowStore.for_root(agent.root)
    if entry is None or not store.available:
        return None
    tree = store.snapshot()
    if not tree:
        return None
    try:
        changes = store.diff(entry["before_tree"], tree)
        files = sorted(store.tree_blobs(tree))
        wanted = [item[key] for item in changes for key in ("old_oid", "oid") if set(item[key]) != {"0"}]
        with _LOCK:
            blobs = store._read_blobs(wanted) if wanted else {}
    except (ShadowError, OSError, KeyError, ValueError):
        return None
    parts = []
    for item in sorted(changes, key=lambda change: change["path"]):
        old = None if item["status"] == "A" else blobs.get(item["old_oid"])
        new = None if item["status"] == "D" else blobs.get(item["oid"])
        parts.append(_unified(item["path"], old, new))
    return "".join(parts), files, tree


def _fallback_diff(agent, paths):
    """没有 git 快照时只能给出改动文件的当前内容。"""
    parts = []
    for path in paths:
        target = agent.root / path
        try:
            data = target.read_bytes() if target.is_file() else None
        except OSError:
            data = None
        text = _text(data) if data is not None else None
        body = "(deleted)" if data is None else ("(binary)" if text is None else text)
        parts.append(f"=== {path} (current content; no diff available) ===\n{body}\n")
    return "".join(parts)


def _checks(agent, task_state):
    """运行时的验证回执，加上模型本回合自己跑过的命令和退出码：没有这些，审查模型会把“看不到测试输出”当成问题。"""
    receipts = (task_state.verification or {}).get("receipts") or []
    lines = [f"- runtime check `{item.get('command', '')}` -> {item.get('status', '')}" for item in receipts[-4:]]
    for item in current_turn_history(agent.session.get("history", [])):
        if item.get("role") == "tool" and item.get("name") == "run_shell":
            code = EXIT_RE.search(str(item.get("content", "")))
            command = clip(str((item.get("args") or {}).get("command", "")), 160)
            lines.append(f"- agent ran `{command}` -> exit {code.group(1) if code else '?'}")
    return "\n".join(lines[-MAX_CHECK_LINES:])


def _model_name(client):
    return str(getattr(client, "model", "") or getattr(client, "provider", "") or "主模型")


def critic_gate(engine, task_state, final):
    """生成器。返回 True 表示审查提出了有证据的反对且还有修复轮次，调用方应 continue 回到模型循环。"""
    agent = engine.runtime
    config = getattr(agent, "critic_config", None)
    paths = list(task_state.changed_paths)
    if not _gate_allowed(agent, config) or not paths:
        return False
    state = task_state.critic
    fingerprint = changes_fingerprint(agent.root, paths)
    if int(state.get("objections", 0)) >= config.max_rounds or state.get("fingerprint") == fingerprint:
        return False  # 修复轮次用完，或者模型没再改文件、只是在回答里反驳：都不再审
    if agent.abort_requested:
        return False
    if budget_stop_needed(agent):
        state.update(skipped="本轮花费已到上限，跳过审查")
        return False
    if agent.max_steps - task_state.tool_steps < MIN_STEPS_LEFT:
        state.update(skipped="步数预算所剩无几，跳过审查")
        return False
    client = getattr(agent, "critic_client", None) or agent.model_client  # 用时再取：/model 切换后跟着变
    shadow = _shadow_diff(agent)
    diff, files, tree = shadow if shadow else (_fallback_diff(agent, paths), [], "")
    diff = agent.redact_text(diff)
    truncated = len(diff) > config.max_diff_chars
    prompt = build_prompt(
        request=agent.redact_text(task_state.user_request),
        rules=agent.redact_text(render_rules(getattr(agent.workspace, "project_rules", ()))),
        files=files,
        changed=paths,
        diff=diff[: config.max_diff_chars],
        truncated=truncated,
        checks=_checks(agent, task_state),
        final=agent.redact_text(final),
    )
    yield {"type": "tool_call", "run_id": task_state.run_id, "name": "critic", "args": {"model": _model_name(client)}}
    started = time.monotonic()
    metadata, reason = {}, ""
    try:
        result = complete_model(client, prompt, agent.max_new_tokens)
        metadata = dict(getattr(result, "metadata", None) or {})
        record_call(agent, metadata, source="critic", client=client)
        verdict, objections, reason = parse_review(getattr(result, "text", ""))
    except Exception as exc:  # 审查只是锦上添花：provider 出错不能让一个已经做完的回合失败
        verdict, objections, reason = VERDICT_ACCEPT, [], f"审查调用失败：{clip(str(exc), 160)}"
    duration_ms = int((time.monotonic() - started) * 1000)
    review = {
        "round": int(state.get("rounds", 0)) + 1,
        "verdict": verdict,
        "objections": objections,
        "reason": reason,
        "duration_ms": duration_ms,
        "model": _model_name(client),
        "diff_chars": len(diff),
        "diff_truncated": truncated,
        "tree": tree,
        "input_tokens": metadata.get("input_tokens"),
        "output_tokens": metadata.get("output_tokens"),
    }
    state["rounds"] = review["round"]
    state["reviews"] = (list(state.get("reviews", [])) + [review])[-MAX_REVIEWS_KEPT:]
    state.update(verdict=verdict, fingerprint=fingerprint)
    agent.emit_trace(
        task_state,
        "critic_review",
        {**{key: value for key, value in review.items() if key != "tree"}, "completion_metadata": metadata},
    )
    yield {
        "type": "tool_result",
        "run_id": task_state.run_id,
        "name": "critic",
        "content": render_review(verdict, objections, reason, duration_ms),
        "metadata": {"tool_status": "error" if verdict == VERDICT_OBJECT else "ok"},
        "duration_ms": duration_ms,
        "output_tokens": metadata.get("output_tokens"),
    }
    if verdict != VERDICT_OBJECT or agent.abort_requested:
        agent.run_store.write_task_state(task_state)
        return False
    state["objections"] = int(state.get("objections", 0)) + 1
    agent.record(
        {
            "role": "tool",
            "name": "critic",
            "args": {},
            "content": repair_message(objections, config.max_rounds - state["objections"]),
            "created_at": now(),
        }
    )
    agent.session_event_bus.emit(
        "critic_objected",
        {"run_id": task_state.run_id, "round": review["round"], "files": [item["file"] for item in objections]},
    )
    agent.run_store.write_task_state(task_state)
    return True


def critic_summary(task_state):
    """给 report / 无人值守 JSON / bench 用的一小段摘要。"""
    state = task_state.critic or {}
    reviews = state.get("reviews", [])
    return {
        "verdict": state.get("verdict", ""),
        "rounds": int(state.get("rounds", 0)),
        "objections": int(state.get("objections", 0)),
        "issues": [item for review in reviews for item in review.get("objections", [])],
        "skipped": state.get("skipped", ""),
    }
