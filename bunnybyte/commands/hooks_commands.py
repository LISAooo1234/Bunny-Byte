"""/hooks：查看当前钩子，/hooks trust：确认并启用仓库里 .bunnybyte.toml 自带的钩子。"""

from __future__ import annotations

from ..core.hooks import EVENTS, trust_project_hooks

EMPTY_HELP = (
    "还没有配置钩子。在 `.bunnybyte.toml`（项目）或 `~/.config/bunnybyte/config.toml`（全局）里添加，例如：\n\n"
    "```toml\n[[hooks.pre_tool]]\nmatch = \"run_shell\"\ncommand = \"python3 .bunnybyte/hooks/guard.py\"\n```\n\n"
    "详见 docs/hooks.md。"
)


def handle_hooks_command(agent, args):
    tokens = str(args or "").split()
    if tokens == ["trust"]:
        return _trust(agent)
    if tokens:
        return "## Usage\n\n- `/hooks` — 列出钩子和它们的状态\n- `/hooks trust` — 信任并启用这个仓库 `.bunnybyte.toml` 里的钩子"
    return render_hooks(agent)


def render_hooks(agent):
    hookset = getattr(getattr(agent, "hooks", None), "hookset", None)
    if hookset is None or not hookset:
        return "## Hooks\n\n" + EMPTY_HELP
    lines = ["## Hooks", "", "| 事件 | 匹配 | 命令 | 来源 | 状态 |", "| --- | --- | --- | --- | --- |"]
    for event in EVENTS:
        for spec in hookset.specs:
            if spec.event == event:
                lines.append(_row(spec, "运行中"))
        for spec in hookset.pending:
            if spec.event == event:
                lines.append(_row(spec, "未信任，不会运行"))
    if hookset.pending:
        lines += ["", "项目里的钩子会在你的机器上执行命令，确认没问题后运行 `/hooks trust` 启用（钩子内容一改，信任就失效）。"]
    for problem in hookset.problems:
        lines.append(f"- ⚠ {problem}")
    return "\n".join(lines)


def _row(spec, state):
    match = "、".join(spec.match) if spec.event != "on_final" else "-"
    command = spec.command.replace("|", "\\|")
    source = "项目" if spec.source == "project" else "全局"
    return f"| {spec.event} | {match} | `{command}` | {source} | {state} |"


def _trust(agent):
    runner = getattr(agent, "hooks", None)
    hookset = runner.hookset if runner else None
    if hookset is None or not hookset.pending:
        return "## Hooks\n\n没有等待确认的项目钩子。"
    trust_project_hooks(hookset.project_config, hookset.project_fingerprint)
    runner.activate(hookset.with_pending_trusted())
    return "## 已信任项目钩子\n\n" + render_hooks(agent).split("\n\n", 1)[1]
