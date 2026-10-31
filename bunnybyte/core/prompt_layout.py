"""提示词的缓存友好布局：一轮里不变的内容在前，每次调用都会变的运行状态放到历史之后。

provider 的提示词缓存（DeepSeek / OpenAI 自动前缀缓存、Anthropic 的 cache_control）只认“从头开始
逐字节相同”的那一段。以前 git status、工作记忆、待办、已读范围、检查点都夹在历史前面，读一次文件或
改一次代码就让后面的历史全部失去复用；现在它们合成一块“运行状态”放在历史之后、当前请求之前。
"""

from __future__ import annotations

import os

from ..features import memory as memorylib

LIVE_STATE_HEADER = "Runtime state (maintained by the runtime, refreshed every call; not a user request):"


def memory_sections(agent, memory_enabled, durable_enabled):
    """返回 {"memory_guide": 不变的记忆说明, "memory": 历史之后的运行状态}。"""
    memory_text = "Memory:\n- disabled" if not memory_enabled else str(agent.memory_text())
    if memory_enabled and not durable_enabled:
        lines = [line for line in memory_text.splitlines() if not line.startswith("- durable_topics:")]
        memory_text = "\n".join([*lines, "- durable_topics: disabled by memory_scope"])
    parts = [LIVE_STATE_HEADER, memory_text]
    if hasattr(agent, "todo_ledger"):
        parts.append(agent.todo_ledger.render_prompt())
    if hasattr(agent, "read_ledger"):
        parts.append(str(agent.read_ledger.render_prompt() or "").strip())
    if hasattr(agent, "render_checkpoint_text"):
        parts.append(str(agent.render_checkpoint_text() or "").strip())
    status = str(getattr(getattr(agent, "workspace", None), "status", "") or "").strip()
    if status:
        parts.append("Git status:\n" + status)
    guide = ""
    if memory_enabled and durable_enabled and hasattr(agent, "memory_dir"):
        guide = memorylib.build_memory_system_section(agent.memory_dir)
    return {"memory_guide": guide, "memory": "\n\n".join(part for part in parts if part)}


def system_split(agent, prompt):
    """前缀里“说明 + 规则 + 工具”那段的长度（Anthropic 协议作为 system 发送）；对不上就返回 0。

    在密钥遮蔽之后算：遮蔽后的提示词必须仍以遮蔽后的这段开头，否则不拆。
    """
    state = getattr(agent, "prefix_state", None)
    stable_chars = int(getattr(state, "stable_chars", 0) or 0)
    if stable_chars <= 0:
        return 0
    stable = agent.redact_text(str(agent.prefix)[:stable_chars]) if hasattr(agent, "redact_text") else str(agent.prefix)[:stable_chars]
    return len(stable) if stable.strip() and prompt.startswith(stable) and len(prompt) > len(stable) else 0


def note_prompt_reuse(agent, prompt):
    """这次提示词和这个 runtime 上一次发出的提示词有多长的公共前缀（provider 缓存最多能复用这么多）。"""
    previous = getattr(agent, "_previous_prompt", None)
    agent._previous_prompt = prompt
    if previous is None:
        return {"prompt_chars": len(prompt), "reused_chars": None}
    return {"prompt_chars": len(prompt), "reused_chars": len(os.path.commonprefix([previous, prompt]))}

