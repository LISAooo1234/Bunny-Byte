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
    SlashCommand("help", "/help", "Show commands.", "General", False, ("h",)),
    SlashCommand("clear", "/clear", "Create a new empty session.", "Session"),
    SlashCommand("compact", "/compact", "Compact older session history.", "Session"),
    SlashCommand("context", "/context", "Show prompt context usage.", "Runtime"),
    SlashCommand("dream", "/dream", "Consolidate durable memory.", "Memory"),
    SlashCommand("history", "/history", "List saved sessions.", "Session"),
    SlashCommand("memory", "/memory", "Show durable memory index.", "Memory"),
    SlashCommand("mode", "/mode", "Show runtime mode.", "Runtime"),
    SlashCommand("model", "/model [name]", "Show or switch the current model.", "Runtime"),
    SlashCommand("plan", "/plan <topic>", "Enter plan mode.", "Planning", True),
    SlashCommand("plan-exit", "/plan-exit", "Exit plan mode.", "Planning"),
    SlashCommand("remember", "/remember <text>", "Save a durable memory note.", "Memory", True),
    SlashCommand("reset", "/reset", "Reset current session memory and history.", "Session"),
    SlashCommand("resume", "/resume <id|index|latest>", "Resume a saved session.", "Session", True),
    SlashCommand("session", "/session", "Show session status.", "Session"),
    SlashCommand("skills", "/skills", "List available BunnyByte skills.", "Skills", False, ("sk",)),
    SlashCommand("skill", "/skill <name> [args]", "Load and run a BunnyByte skill.", "Skills", True),
    SlashCommand("agents", "/agents", "Show subagent worker status.", "Planning", False, ("agent",)),
    SlashCommand(
        "subagent",
        "/subagent explore <task>",
        "Launch a bounded local child run: Explore or scoped worker.",
        "Planning",
        True,
        ("sub",),
    ),
    SlashCommand("usage", "/usage", "Show model/provider usage metadata.", "Runtime"),
    SlashCommand("working-memory", "/working-memory", "Show working memory.", "Memory"),
    SlashCommand("exit", "/exit", "Exit BunnyByte.", "General", False, ("quit",)),
)


def command_help_text() -> str:
    lines = [
        "## Commands",
        "",
        "Type `/` to open command suggestions. Press `Tab` to accept a suggestion. Press `Enter` to run no-arg commands or fill in commands that still need arguments.",
    ]
    categories = ("General", "Session", "Runtime", "Memory", "Planning", "Skills")
    for category in categories:
        commands = [command for command in SLASH_COMMANDS if command.category == category]
        if not commands:
            continue
        lines.extend(["", f"### {category}"])
        for command in commands:
            alias_text = (
                f" Aliases: {', '.join(f'`/{alias}`' for alias in command.aliases)}."
                if command.aliases
                else ""
            )
            lines.append(f"- `{command.usage}`: {command.description}{alias_text}")
    lines.extend(
        [
            "",
            "### Quick Tips",
            "- Use `/resume latest` to continue the most recent saved session.",
            "- Use `/history` to list sessions, then `/resume 1` to switch to one of them.",
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
    usage = "Usage: /subagent explore <task> or /subagent worker --scope <path[,path]> <task>"
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
