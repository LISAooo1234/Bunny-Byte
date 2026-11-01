"""控制面守卫：会改变 agent 自己行为的文件，改之前要有人点头，`--approval auto` 也一样。

克隆来的仓库可以在 AGENTS.md 里写“先把这段加进 .bunnybyte.toml 的 [mcp]”“装一个 pre-commit 钩子”，
模型照做之后，下一次启动就是攻击者的配置。这里管两件事：
- write_file / patch_file 写到这些路径（规则文件、`.bunnybyte.toml`、`.bunnybyte/{rules,skills}`、
  `.git/hooks`、`.git/config`、其他 agent 的配置）时：用户这一轮的请求里点名了这个文件就照常执行；
  否则要有人确认（TUI 弹窗；`ask` 模式下就是那一次普通审批），没人能确认（`bunny -p`、子 agent、
  `--approval never`）就拒绝并告诉模型原因。`.bunnybyte/memory` 只记录不拦：记忆本来就靠模型写。
- run_shell / MCP 工具：见 control_watch.py，命令前后比对一份固定的文件清单。

`[control] mode = guard|warn|off`（默认 guard），仓库里的 `.bunnybyte.toml` 只能调严。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

CONTROL_MODES = ("off", "warn", "guard")
WRITE_TOOLS = frozenset({"write_file", "patch_file"})
RULE_NAMES = frozenset({"agents.md", "claude.md", "gemini.md", ".cursorrules", ".clinerules", ".windsurfrules"})
# 只认仓库根目录下的这些文件（其他 agent 也只在根目录找它们）
ROOT_CONFIG_FILES = frozenset(
    {".mcp.json", ".claude/settings.json", ".claude/settings.local.json", ".cursor/mcp.json", ".gemini/settings.json"}
    | {".codex/config.toml", ".vscode/settings.json", ".vscode/tasks.json", ".github/copilot-instructions.md"}
)
CONFIG_DIRS = (
    (".bunnybyte/rules/", "rules"), (".bunnybyte/skills/", "skills"), (".cursor/rules/", "rules"), (".clinerules/", "rules"),
    (".claude/commands/", "config"), (".claude/agents/", "config"), (".claude/skills/", "skills"), (".claude/hooks/", "config"),
)
LABELS = {
    "rules": "项目规则文件", "config": "agent 配置文件", "skills": "技能文件", "git_hook": "git 钩子",
    "git_config": "git 配置", "memory": "长期记忆", "shell_rc": "shell 启动脚本", "trust": "Bunny Byte 的信任记录",
    "ssh": "SSH 配置", "launch": "开机自启任务",
}
# 这些结果要在回合末尾提醒用户
ALERT_DECISIONS = frozenset({"blocked", "denied", "restored", "reported", "changed_again", "noted"})
EXEMPT_SESSION_KINDS = frozenset({"internal_dream", "internal_skill_evolution"})
PURSUING = "pursuing"


@dataclass(frozen=True)
class ControlConfig:
    mode: str = "guard"
    problems: list = field(default_factory=list, compare=False)


def resolve_control_config(values, *, mode=None, trusted=None):
    """`trusted` 是只来自用户自己（全局配置 / --config）的那份；仓库配置只能把守卫调严。"""
    merged, problems = _parse((values or {}).get("control"))
    if trusted is not None:
        base, base_problems = _parse((trusted or {}).get("control"))
        problems = base_problems + problems
        if CONTROL_MODES.index(merged.mode) < CONTROL_MODES.index(base.mode):
            problems.append(f"项目配置不能把 [control].mode 从 {base.mode} 调成 {merged.mode}，已忽略")
            merged = replace(merged, mode=base.mode)
    if mode:
        merged = replace(merged, mode=mode)
    return replace(merged, problems=problems)


def _parse(section):
    if section is None:
        return ControlConfig(), []
    if not isinstance(section, dict):
        return ControlConfig(), ["[control] 应该是一个表，已忽略"]
    mode = str(section.get("mode", "guard"))
    if mode not in CONTROL_MODES:
        return ControlConfig(), [f"[control].mode 只能是 {' / '.join(CONTROL_MODES)}，已按 guard 处理"]
    return ControlConfig(mode=mode), []


def control_mode(agent):
    config = getattr(agent, "control_config", None) or ControlConfig()
    return config.mode


def exempt(agent):
    """dream / 技能进化这类内部会话：它们本来就是来写记忆和技能的。"""
    session = getattr(agent, "session", None) or {}
    return str(session.get("kind", "") or "") in EXEMPT_SESSION_KINDS


def classify(relative):
    """工作区里的相对路径（posix）→ 类别；不是控制面文件返回空串。大小写不敏感（macOS 的文件系统也是）。"""
    lower = str(relative).replace("\\", "/").lower().strip("/")
    parts = lower.split("/")
    name = parts[-1]
    if lower.startswith(".bunnybyte/memory/"):
        return "memory"
    if lower.startswith(".git/hooks/"):
        return "" if name.endswith(".sample") or len(parts) != 3 else "git_hook"
    if lower == ".git/config":
        return "git_config"
    if name in RULE_NAMES:
        return "rules"
    if name == ".bunnybyte.toml" or lower in ROOT_CONFIG_FILES:
        return "config"
    for prefix, category in CONFIG_DIRS:
        if lower.startswith(prefix):
            return category
    if parts[0] == "skills" and (name == "skill.md" or (len(parts) == 2 and name.endswith(".md"))):
        return "skills"  # 仓库的 skills/ 也会被当成项目技能加载
    return ""


def workspace_relatives(agent, raw):
    """模型给的路径和解析符号链接之后的路径，都换成工作区相对路径；跳出工作区的不算。"""
    root = str(agent.root)
    path = raw if os.path.isabs(str(raw)) else os.path.join(root, str(raw))
    candidates = [os.path.normpath(path)]
    try:
        candidates.append(str(Path(path).resolve()))
    except (OSError, RuntimeError):
        pass
    relatives = []
    for candidate in candidates:
        try:
            if os.path.commonpath([root, candidate]) == root and candidate != root:
                relatives.append(Path(os.path.relpath(candidate, root)).as_posix())
        except ValueError:
            continue
    return list(dict.fromkeys(relatives))


def request_texts(agent):
    """用户自己这一轮说的话：当前回合的用户消息、运行中的插话、目标模式的目标；子 agent 用父 agent 的。"""
    texts = []
    parent = getattr(agent, "control_parent", None)
    if parent is not None:
        texts.extend(request_texts(parent))
    history = list((getattr(agent, "session", None) or {}).get("history") or [])
    start = 0
    for index in range(len(history) - 1, -1, -1):
        item = history[index]
        if item.get("role") == "user" and str(item.get("source", "") or "user") not in {"steer", "worker_notification"}:
            start = index
            if str(item.get("source", "") or "user") == "user":
                texts.append(str(item.get("content", "")))
            break
    texts.extend(
        str(item.get("content", "")) for item in history[start + 1:] if item.get("role") == "user" and item.get("source") == "steer"
    )
    goal = (getattr(agent, "session", None) or {}).get("goal") or {}
    if goal.get("status") == PURSUING and goal.get("objective"):
        texts.append(str(goal["objective"]))
    return texts


def _named(word, blob):
    """`word` 作为完整的名字出现（前后不是字母数字、`.`、`-`、`_`）；中文紧挨着也算。"""
    return re.search(r"(?<![A-Za-z0-9_.\-])" + re.escape(word) + r"(?![A-Za-z0-9_\-]|\.[A-Za-z0-9])", blob) is not None


def mentioned(display, texts):
    """用户有没有点名这个文件。AGENTS.md、.zshrc 这类名字本身就够明确；config、SKILL.md 要带上目录；
    技能要同时说到“技能 / skill”和技能名。`.bunnybyte.toml.example` 不算点名 `.bunnybyte.toml`。"""
    blob = "\n".join(texts).lower()
    if not blob:
        return False
    parts = str(display).lower().removeprefix("~/").split("/")
    name = parts[-1]
    if (name in RULE_NAMES or (name.startswith(".") and len(name) > 4)) and _named(name, blob):
        return True
    if len(parts) >= 2 and _named("/".join(parts[-2:]), blob):
        return True
    if name == "skill.md" and len(parts) >= 2 and len(parts[-2]) >= 3:
        return ("技能" in blob or "skill" in blob) and _named(parts[-2], blob)
    return False


def guard_write(agent, tool, args, allow, deny):
    """PermissionChecker 调用：返回 (权限结果或 None, 给模型的提示)。None 表示照常走后面的审批。"""
    if tool.name not in WRITE_TOOLS or control_mode(agent) == "off" or exempt(agent):
        return None, ()
    relatives = workspace_relatives(agent, (args or {}).get("path", ""))
    display, category = next(((path, found) for path in relatives if (found := classify(path))), ("", ""))
    if not category:
        return None, ()  # 符号链接指向 AGENTS.md 时 display 是 AGENTS.md：说“改 notes.md”不算点名
    if category == "memory":
        record(agent, tool.name, "write", [_change(display, category, "write", "noted")])
        return None, ()
    allowed = getattr(agent, "_control_allowed", None)
    if allowed is None:
        allowed = agent._control_allowed = set()
    run_id = getattr(getattr(agent, "current_task_state", None), "run_id", "")
    if run_id and (run_id, display) in allowed:
        return None, ()
    if mentioned(display, request_texts(agent)):
        record(agent, tool.name, "write", [_change(display, category, "write", "mentioned")])
        return None, ()
    note = f"控制面守卫：{display} 是{LABELS[category]}，会改变 agent 之后的行为，用户这一轮没有提到它"
    if control_mode(agent) == "warn":
        record(agent, tool.name, "write", [_change(display, category, "write", "noted")])
        return None, (f"[control-plane guard] {note}（warn 模式，已照常执行）。",)
    from .dependency_guard import human_confirmer

    confirm = human_confirmer(agent)
    if confirm is None:
        record(agent, tool.name, "write", [_change(display, category, "write", "blocked")])
        return deny("control_plane_blocked", "control_plane_guard", blocked_message(tool.name, display, category)), ()
    if confirm(tool.name, dict(args, guard_note=note + "。允许 = 照常写入")):  # 这一次确认同时算普通审批
        allowed.add((run_id, display))
        record(agent, tool.name, "write", [_change(display, category, "write", "confirmed")])
        return allow("control_plane_confirmed"), ()
    record(agent, tool.name, "write", [_change(display, category, "write", "denied")])
    return deny("control_plane_denied", "control_plane_guard", f"error: the user rejected {tool.name} on {display} ({LABELS[category]})"), ()


def blocked_message(tool_name, display, category):
    return (
        f"error: control-plane guard blocked {tool_name} on {display}: 这是{LABELS[category]}，会改变 agent 自己之后的行为；"
        "用户这一轮没有提到它，也没有人能确认，所以没有写入。不要换个办法（shell 等）再改它；"
        "如果确实需要，在最终回答里说明要改什么，让用户自己改，或在请求里点名这个文件。"
    )


def _change(path, category, status, decision):
    return {"path": path, "category": category, "status": status, "decision": decision}


def record(agent, tool_name, kind, changes):
    """写进 trace（`bunny replay`）、会话事件和本轮的回执（report.json / `bunny -p` JSON 的 control_plane）。"""
    entry = {"tool_name": tool_name, "kind": kind, "changes": [dict(item) for item in changes]}
    agent.session_event_bus.emit("control_plane", entry)
    state = getattr(agent, "current_task_state", None)
    if state is None:
        return
    receipts = getattr(agent, "control_receipts", None)
    if receipts is None:
        receipts = agent.control_receipts = {}
    if state.run_id not in receipts and len(receipts) >= 20:
        receipts.pop(next(iter(receipts)))
    receipts.setdefault(state.run_id, []).append(entry)
    agent.emit_trace(state, "control_plane", dict(entry))
    parent = getattr(agent, "control_parent", None)
    if parent is not None:  # 子 agent 被拦下的写入也要出现在父 agent 的 report / -p JSON / 回合末尾提醒里
        record(parent, f"{tool_name}（子 agent）", kind, changes)


def control_receipt(agent, state):
    if state is None:
        return []
    return list((getattr(agent, "control_receipts", None) or {}).get(state.run_id, []))


DECISION_TEXT = {
    "blocked": "已拦下", "denied": "你拒绝了", "restored": "已恢复原内容", "reported": "被改了、没有自动恢复，请检查",
    "changed_again": "等待确认时又被改过，没有恢复，请检查", "noted": "已放行（warn 模式）", "confirmed": "你确认了改动",
}


def describe_change(change, tool_name=""):
    via = f"（{tool_name}）" if tool_name else ""
    return f"{change['path']} {DECISION_TEXT.get(change['decision'], change['decision'])}{via}"


def control_line(agent, task_state):
    """回合末尾的一行提醒；用户点名过的文件和记忆写入不提。"""
    if agent.depth != 0 or task_state is None:
        return ""
    alerts, confirmed = [], []
    for entry in control_receipt(agent, task_state):
        for change in entry["changes"]:
            if change["category"] == "memory" or change["decision"] == "mentioned":
                continue
            (alerts if change["decision"] in ALERT_DECISIONS else confirmed).append(describe_change(change, entry["tool_name"]))
    if alerts:
        return "⚠ 控制面守卫：" + "；".join(dict.fromkeys(alerts + confirmed))
    if confirmed:
        return "· 控制面守卫：" + "；".join(dict.fromkeys(confirmed))
    return ""
