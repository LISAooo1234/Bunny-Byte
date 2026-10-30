"""/mcp：查看 MCP 服务器和它们提供的工具，/mcp trust：信任并启动仓库 .bunnybyte.toml 里声明的服务器。"""

from __future__ import annotations

from ..core.mcp_config import trust_project_servers
from ..core.mcp_tools import register_mcp_tools

EMPTY_HELP = (
    "还没有配置 MCP 服务器。在 `.bunnybyte.toml`（项目）或 `~/.config/bunnybyte/config.toml`（全局）里添加，例如：\n\n"
    "```toml\n[mcp.servers.fs]\ncommand = \"npx\"\nargs = [\"-y\", \"@modelcontextprotocol/server-filesystem\", \".\"]\n```\n\n"
    "重启 Bunny Byte 后生效。"
)
STATUS_TEXT = {"running": "运行中", "failed": "启动失败", "stopped": "已停止"}


def handle_mcp_command(agent, args):
    tokens = str(args or "").split()
    if tokens == ["trust"]:
        return _trust(agent)
    if tokens:
        return "## Usage\n\n- `/mcp` — 列出 MCP 服务器、状态和工具\n- `/mcp trust` — 信任并启动这个仓库 `.bunnybyte.toml` 里的 MCP 服务器"
    return render_mcp(agent)


def render_mcp(agent):
    manager = getattr(agent, "mcp", None)
    config = getattr(manager, "config", None)
    if manager is None or not config:
        return "## MCP\n\n" + EMPTY_HELP
    lines = ["## MCP", "", "| 服务器 | 来源 | 状态 | 工具 |", "| --- | --- | --- | --- |"]
    for state in sorted(manager.states.values(), key=lambda item: item.spec.name):
        status = STATUS_TEXT.get(state.status, state.status)
        if state.status == "failed":
            status += f"：{_cell(state.error)}"
        tools = "、".join(f"`{name}`" for name in sorted(state.exposed.values())) or "-"
        lines.append(f"| {state.spec.name} | {_source(state.spec)} | {status} | {tools} |")
    for spec in config.pending:
        lines.append(f"| {spec.name} | 项目 | 未信任，不会启动 | - |")
    if config.pending:
        lines += ["", "### 等待确认的项目服务器（`/mcp trust` 会信任下面列出的全部内容）", ""]
        for spec in config.pending:
            lines += _pending_details(spec)
        lines += ["项目里的 MCP 服务器会在你的机器上运行命令，确认没问题后运行 `/mcp trust` 启动（配置一改，信任就失效）。"]
    notes = [state.error for state in manager.states.values() if state.status == "running" and state.error]
    for problem in [*config.problems, *notes]:
        lines.append(f"- ⚠ {problem}")
    return "\n".join(lines)


def _trust(agent):
    manager = getattr(agent, "mcp", None)
    config = getattr(manager, "config", None)
    if manager is None or not config or not config.pending:
        return "## MCP\n\n没有等待确认的项目 MCP 服务器。"
    pending = config.pending
    trust_project_servers(config)
    manager.config = config.with_pending_trusted()
    manager.start(pending)
    added = register_mcp_tools(agent, manager)
    return f"## 已信任项目 MCP 服务器\n\n新增 {len(added)} 个工具。\n\n" + render_mcp(agent).split("\n\n", 1)[1]


def _pending_details(spec):
    rows = [f"- **{spec.name}**", f"  - 命令：`{_cell(_command(spec))}`"]
    if spec.cwd:
        rows.append(f"  - cwd：`{_cell(spec.cwd)}`")
    for key, value in spec.env:
        rows.append(f"  - env：`{key} = {_cell(value)}`")
    if spec.read_only_tools:
        note = "（全部工具免审批）" if "*" in spec.read_only_tools else ""
        rows.append(f"  - 免审批工具：{_cell('、'.join(spec.read_only_tools))}{note}")
    return rows + [""]


def _source(spec):
    return "项目" if spec.source == "project" else "全局"


def _command(spec):
    return " ".join([spec.command, *spec.args])


def _cell(text):
    return " ".join(str(text or "").split()).replace("|", "\\|")[:160]
