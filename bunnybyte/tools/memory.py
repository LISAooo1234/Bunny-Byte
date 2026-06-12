"""长期记忆 tool 定义。"""

MEMORY_TOOL_SPECS = {
    "remember": {
        "schema": {"text": "str", "kind": "str='project'"},
        "risky": False,
        "description": (
            "在用户透露稳定偏好、长期项目事实、决策、反馈或参考信息时，保存一条长期记忆。"
            "不要保存密钥、原始命令输出、临时任务状态，或可从文件重新推导出的事实。"
        ),
    },
}

MEMORY_TOOL_EXAMPLES = {
    "remember": '<tool>{"name":"remember","args":{"kind":"preference","text":"用户希望代码改动后使用简洁中文汇报。"}}</tool>',
}

_ALLOWED_KINDS = {"user", "feedback", "project", "reference", "preference", "decision"}


def validate_memory_tool(agent, name, args):
    if name != "remember":
        return
    note_text = str(args.get("text", "")).strip()
    if not note_text:
        raise ValueError("text must not be empty")
    kind = str(args.get("kind", "project") or "project").strip().lower()
    if kind not in _ALLOWED_KINDS:
        raise ValueError("kind must be one of user, feedback, project, reference, preference, decision")
    reason = agent.reject_durable_reason(note_text)
    if reason:
        raise ValueError(f"memory rejected: {reason}")


def tool_remember(agent, args):
    note_text = str(args.get("text", "")).strip()
    kind = str(args.get("kind", "project") or "project").strip().lower()
    if kind == "preference":
        topic = "user"
    elif kind == "decision":
        topic = "project"
    else:
        topic = kind
    path = agent.remember_durable_note(f"{topic}: {note_text}", source="tool")
    relative = path.relative_to(agent.root).as_posix() if path else ".bunnybyte/memory"
    return f"已保存 {topic} 长期记忆：{relative}"
