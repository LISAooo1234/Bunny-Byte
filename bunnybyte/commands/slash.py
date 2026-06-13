"""Slash command registry and parsers."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SlashCommand:
    name: str
    usage: str
    description: str
    category: str = "General"
    requires_arguments: bool = False
    aliases: tuple[str, ...] = field(default_factory=tuple)


SLASH_COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("help", "/help", "显示可用命令。", "通用", False, ("h",)),
    SlashCommand("clear", "/clear", "创建一个新的空会话。", "会话"),
    SlashCommand("fork", "/fork [event|turn|#]", "从历史节点创建分支会话。", "会话"),
    SlashCommand("rollback", "/rollback [event|turn|#]", "回滚到历史节点并创建分支会话。", "会话"),
    SlashCommand("compact", "/compact", "压缩较早的会话历史。", "会话"),
    SlashCommand("context", "/context", "查看当前提示词上下文用量。", "运行时"),
    SlashCommand("dream", "/dream", "整理并固化长期记忆。", "记忆"),
    SlashCommand("history", "/history", "列出已保存的会话。", "会话"),
    SlashCommand("memory", "/memory", "查看长期记忆索引。", "记忆"),
    SlashCommand("mode", "/mode", "查看当前运行模式。", "运行时"),
    SlashCommand(
        "model", "/model [name]", "查看或切换当前模型。", "运行时"
    ),
    SlashCommand(
        "provider",
        "/provider [name]",
        "查看或切换当前 provider 配置。",
        "运行时",
    ),
    SlashCommand("plan", "/plan <topic>", "进入计划模式。", "计划", True),
    SlashCommand("plan-exit", "/plan-exit", "退出计划模式。", "计划"),
    SlashCommand("remember", "/remember <text>", "保存一条长期记忆。", "记忆", True),
    SlashCommand("reset", "/reset", "重置当前会话的记忆和历史。", "会话"),
    SlashCommand("resume", "/resume <id|index|latest>", "按 id、序号、主题或 latest 恢复会话。", "会话", True),
    SlashCommand("session", "/session", "查看当前会话状态。", "会话"),
    SlashCommand("topic", "/topic [name]", "查看或重命名当前会话主题。", "会话"),
    SlashCommand("tool", "/tool [name]", "展示模型可用工具。", "运行时", False, ("tools",)),
    SlashCommand("skills", "/skills", "列出可用的 BunnyByte 技能。", "技能", False, ("sk",)),
    SlashCommand("skill", "/skill <name> [args]", "加载并运行一个 BunnyByte 技能。", "技能", True),
    SlashCommand("agents", "/agents", "查看子 agent/worker 状态。", "计划", False, ("agent",)),
    SlashCommand(
        "subagent",
        "/subagent explore <task>",
        "启动一个有边界的本地子任务：Explore 或限定写入范围的 worker。",
        "计划",
        True,
        ("sub",),
    ),
    SlashCommand("usage", "/usage", "查看模型/provider 使用元数据。", "运行时"),
    SlashCommand("working-memory", "/working-memory", "查看当前工作记忆。", "记忆"),
    SlashCommand("exit", "/exit", "退出 BunnyByte。", "通用", False, ("quit",)),
)


def command_help_text() -> str:
    lines = [
        "## 命令",
        "",
        "输入 `/` 打开命令建议；按 `Tab` 接受建议；无参数命令可直接按 `Enter` 运行，需要参数的命令请补全后再执行。",
    ]
    categories = ("通用", "会话", "运行时", "记忆", "计划", "技能")
    for category in categories:
        commands = [command for command in SLASH_COMMANDS if command.category == category]
        if not commands:
            continue
        lines.extend(["", f"### {category}", "", "| 命令 | 说明 |", "| --- | --- |"])
        for command in commands:
            alias_text = (
                f" 别名：{', '.join(f'`/{alias}`' for alias in command.aliases)}。"
                if command.aliases
                else ""
            )
            lines.append(f"| `{command.usage}` | {command.description}{alias_text} |")
    lines.extend(
        [
            "",
            "### 快速提示",
            "",
            "- 使用 `/resume latest` 继续最近保存的会话。",
            "- 使用 `/history` 查看会话列表，然后用 `/resume 1` 切换到其中一个会话。",
        ]
    )
    return "\n".join(lines)


def resolve_command(name: str) -> SlashCommand | None:
    normalized = str(name or "").strip().lstrip("/").lower()
    if not normalized:
        return None
    for command in SLASH_COMMANDS:
        if normalized == command.name or normalized in command.aliases:
            return command
    return None


def suggest_commands(text: str, limit: int = 8) -> list[SlashCommand]:
    raw = str(text or "")
    if not raw.startswith("/"):
        return []
    body = raw[1:]
    if " " in body:
        return []
    token = body.lower()
    matches = []
    for command in SLASH_COMMANDS:
        names = (command.name, *command.aliases)
        if not token or any(name.startswith(token) for name in names):
            matches.append(command)
    return matches[:limit]


def parse_subagent_args(args: str) -> tuple[dict | None, str]:
    usage = "用法：/subagent explore <任务> 或 /subagent worker --scope <路径[,路径]> <任务>"
    try:
        tokens = shlex.split(str(args or ""))
    except ValueError as exc:
        return None, f"{usage}. {exc}"
    if not tokens:
        return None, usage

    subagent_type = "Explore"
    if tokens[0].lower() in {"explore", "worker"}:
        subagent_type = "worker" if tokens.pop(0).lower() == "worker" else "Explore"

    write_scope: list[str] = []
    task_parts: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--scope":
            index += 1
            if index >= len(tokens):
                return None, usage
            write_scope.extend(_split_scope(tokens[index]))
        elif token.startswith("--scope="):
            write_scope.extend(_split_scope(token.split("=", 1)[1]))
        else:
            task_parts.append(token)
        index += 1

    prompt = " ".join(task_parts).strip()
    if not prompt:
        return None, usage
    if subagent_type == "worker" and not write_scope:
        return None, usage
    return {
        "description": prompt[:80],
        "prompt": prompt,
        "subagent_type": subagent_type,
        "write_scope": write_scope,
    }, ""


def _split_scope(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]
