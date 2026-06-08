"""命令行入口。

这个模块负责把“用户怎么启动 bunnybyte”翻译成 runtime 能理解的对象：
解析参数、挑模型后端、构建工作区快照、恢复或新建 session，
最后进入 one-shot 或交互式循环。
"""

import argparse
import json
import os
import re
import time
import shutil
import sys
import textwrap

from .branding import (
    DISPLAY_HANDLE,
    SUBTITLE,
    WELCOME_STATUS,
    mascot_visible_width,
    render_mascot_ansi_rows,
)
from .commands.slash import command_help_text, parse_subagent_args, resolve_command
from .config import (
    DEFAULT_PROVIDER,
    PROVIDER_DEFAULTS,
    default_max_tokens_for_provider,
    list_provider_profiles,
    load_project_env,
    resolve_project_sandbox_config,
    resolve_provider_config,
)
from .features import skills as skillslib
from .features.skills_runtime import invoke_skill
from .providers import AnthropicCompatibleModelClient, OpenAICompatibleModelClient
from .providers.errors import sanitize_url
from .core.runtime import BunnyByte, SessionStore
from .core.workspace import WorkspaceContext, clip, middle

DEFAULT_SECRET_ENV_NAMES = (
    "BUNNYBYTE_API_KEY",
    "BUNNYBYTE_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "BUNNYBYTE_ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "BUNNYBYTE_DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY",
    "GITHUB_PAT",
    "GH_PAT",
)

WELCOME_NAME = DISPLAY_HANDLE
WELCOME_SUBTITLE = SUBTITLE
HELP_DETAILS = (
    command_help_text()
    + "\n\n"
    + textwrap.dedent(
        """\
    技能工作流：
    /skill <name> [args] 运行一个 BunnyByte 技能。
    """
    ).strip()
)


DEFAULT_OPENAI_MODEL = PROVIDER_DEFAULTS["openai"]["model"]
DEFAULT_OPENAI_BASE_URL = PROVIDER_DEFAULTS["openai"]["base_url"]
SECRET_ENV_NAMES_VAR = "BUNNYBYTE_SECRET_ENV_NAMES"


def _configured_secret_names(args):
    configured_secret_names = set(DEFAULT_SECRET_ENV_NAMES)
    configured_secret_names.update(str(name).upper() for name in args.secret_env_names)
    extra_names = os.environ.get(SECRET_ENV_NAMES_VAR, "")
    if extra_names.strip():
        configured_secret_names.update(
            item.strip().upper() for item in extra_names.split(",") if item.strip()
        )
    return sorted(configured_secret_names)


def _resolve_cli_provider_config(args, provider=None, include_cli_overrides=True):
    config_model = getattr(args, "model", None) if include_cli_overrides else None
    config_base_url = getattr(args, "base_url", None) if include_cli_overrides else None
    config_api_key = getattr(args, "api_key", None) if include_cli_overrides else None
    return resolve_provider_config(
        provider if provider is not None else getattr(args, "provider", None),
        start=getattr(args, "cwd", "."),
        config_path=getattr(args, "config", None),
        model=config_model,
        base_url=config_base_url,
        api_key=config_api_key,
    )


def _build_model_client(args, provider=None, include_cli_overrides=True):
    client, _ = _build_model_client_with_config(
        args, provider=provider, include_cli_overrides=include_cli_overrides
    )
    return client


def _build_model_client_with_config(args, provider=None, include_cli_overrides=True):
    config = _resolve_cli_provider_config(
        args, provider=provider, include_cli_overrides=include_cli_overrides
    )
    return _model_client_from_config(args, config), config


def _model_client_from_config(args, config):
    # CLI 只负责把 provider profile 翻译成具体协议 client。
    # 例如 deepseek 是 profile，protocol=anthropic 才决定走 Messages API。
    if config.protocol == "openai":
        client = OpenAICompatibleModelClient(
            model=config.model,
            base_url=config.base_url,
            api_key=config.api_key,
            temperature=getattr(args, "temperature", 0.2),
            timeout=getattr(args, "openai_timeout", 300),
        )
        return _annotate_model_client(client, config)
    if config.protocol == "anthropic":
        client = AnthropicCompatibleModelClient(
            model=config.model,
            base_url=config.base_url,
            api_key=config.api_key,
            temperature=getattr(args, "temperature", 0.2),
            timeout=getattr(args, "openai_timeout", 300),
        )
        return _annotate_model_client(client, config)

    raise ValueError(f"unknown provider protocol: {config.protocol}")


def _annotate_model_client(client, config):
    setattr(client, "provider", config.name)
    setattr(client, "protocol", config.protocol)
    if not isinstance(getattr(client, "model", None), str) or not getattr(
        client, "model", ""
    ):
        setattr(client, "model", config.model)
    if not isinstance(getattr(client, "base_url", None), str) or not getattr(
        client, "base_url", ""
    ):
        setattr(client, "base_url", config.base_url)
    if not isinstance(getattr(client, "api_key", None), str):
        setattr(client, "api_key", config.api_key)
    return client


def build_welcome(agent, model, host):
    width = max(68, min(shutil.get_terminal_size((80, 20)).columns, 84))
    inner = width - 4
    gap = 3
    left_width = (inner - gap) // 2
    right_width = inner - gap - left_width

    def row(text):
        body = middle(text, width - 4)
        return f"| {body.ljust(width - 4)} |"

    def divider(char="-"):
        return "+" + char * (width - 2) + "+"

    def center(text):
        body = middle(text, inner)
        return f"| {body.center(inner)} |"

    def center_ansi(text, visible_width):
        visible_width = min(visible_width, inner)
        left = max(0, (inner - visible_width) // 2)
        right = max(0, inner - visible_width - left)
        return f"| {' ' * left}{text}{' ' * right} |"

    def cell(label, value, size):
        body = middle(f"{label:<9} {value}", size)
        return body.ljust(size)

    def pair(left_label, left_value, right_label, right_value):
        left = cell(left_label, left_value, left_width)
        right = cell(right_label, right_value, right_width)
        return f"| {left}{' ' * gap}{right} |"

    line = divider("=")
    rows = [center_ansi(text, mascot_visible_width()) for text in render_mascot_ansi_rows()]
    session_label = "pending" if agent.is_pending_session else agent.session["id"]
    rows.extend(
        [
            center(WELCOME_NAME),
            center(WELCOME_SUBTITLE),
            center(WELCOME_STATUS),
            divider("-"),
            row(""),
            row("WORKSPACE  " + middle(agent.workspace.cwd, inner - 11)),
            pair("MODEL", model, "BRANCH", agent.workspace.branch),
            pair("APPROVAL", agent.approval_policy, "SESSION", session_label),
            row("TOPIC      " + middle(agent.session_topic, inner - 11)),
            row(""),
        ]
    )
    return "\n".join([line, *rows, line])


def build_agent(args):
    """根据 CLI 参数装配出一个可运行的 BunnyByte 实例。

    为什么存在：
    命令行参数只是字符串和开关，runtime 需要的是已经装配好的对象图：
    model client、workspace snapshot、session store、secret 配置等。
    这个函数负责把“启动参数”翻译成“agent 运行现场”。

    输入 / 输出：
    - 输入：`argparse` 解析后的 `args`
    - 输出：一个新的 `BunnyByte`，或一个从旧 session 恢复出来的 `BunnyByte`

    在 agent 链路里的位置：
    它是整个程序启动链路里最靠近 runtime 的装配点。`main()` 先调它，
    得到 agent 后，后面无论是 one-shot 还是 REPL 模式，都会落到 `ask()`。
    """
    # 这里是 CLI 到 runtime 的装配点：
    # 先采集工作区快照，再整理 secret 名单、模型后端和 session。
    workspace = WorkspaceContext.build(args.cwd)
    store = SessionStore(workspace.repo_root + "/.bunnybyte/sessions")
    model, provider_config = _build_model_client_with_config(args)

    def model_client_factory():
        return _build_model_client(args)

    # 默认不设置输出 token 上限；只有用户显式传 --max-new-tokens 时才下发限制。
    max_new_tokens_defaulted = args.max_new_tokens is None
    setattr(args, "_bunnybyte_max_new_tokens_defaulted", max_new_tokens_defaulted)

    sandbox_config = resolve_project_sandbox_config(
        start=workspace.repo_root,
        config_path=getattr(args, "config", None),
        mode=getattr(args, "sandbox", None),
        backend=getattr(args, "sandbox_backend", None),
    )
    load_project_env(workspace.repo_root, override=False)
    configured_secret_names = _configured_secret_names(args)
    session_id = args.resume
    if session_id == "latest":
        session_id = store.latest(include_empty=False)
    memory_dir = getattr(args, "memory_dir", None)
    auto_dream = not getattr(args, "no_auto_dream", False)
    dream_interval = getattr(args, "dream_interval", 24.0)
    dream_min_sessions = getattr(args, "dream_min_sessions", 5)
    ask_user_callback = None if getattr(args, "prompt", None) else _cli_ask_user
    if session_id:
        agent = BunnyByte.from_session(
            model_client=model,
            workspace=workspace,
            session_store=store,
            session_id=session_id,
            approval_policy=args.approval,
            max_steps=args.max_steps,
            max_new_tokens=args.max_new_tokens,
            secret_env_names=configured_secret_names,
            memory_dir=memory_dir,
            auto_dream=auto_dream,
            dream_interval_hours=dream_interval,
            dream_min_sessions=dream_min_sessions,
            model_client_factory=model_client_factory,
            sandbox_config=sandbox_config,
            ask_user_callback=ask_user_callback,
        )
        _attach_provider_switching(agent, args)
        return agent
    lazy_session = True
    agent = BunnyByte(
        model_client=model,
        workspace=workspace,
        session_store=store,
        approval_policy=args.approval,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
        secret_env_names=configured_secret_names,
        memory_dir=memory_dir,
        auto_dream=auto_dream,
        dream_interval_hours=dream_interval,
        dream_min_sessions=dream_min_sessions,
        model_client_factory=model_client_factory,
        sandbox_config=sandbox_config,
        ask_user_callback=ask_user_callback,
        lazy_session=lazy_session,
    )
    _attach_provider_switching(agent, args)
    return agent


def _attach_provider_switching(agent, args):
    def current_model_client_factory():
        current = getattr(agent, "model_client", None)
        config = resolve_provider_config(
            _client_string_attr(current, "provider")
            or getattr(args, "provider", None),
            start=getattr(args, "cwd", "."),
            config_path=getattr(args, "config", None),
            model=_client_string_attr(current, "model"),
            base_url=_client_string_attr(current, "base_url"),
            api_key=_client_string_attr(current, "api_key"),
        )
        return _model_client_from_config(args, config)

    def provider_switch_factory(provider):
        client, config = _build_model_client_with_config(
            args, provider=provider, include_cli_overrides=True
        )
        if getattr(args, "_bunnybyte_max_new_tokens_defaulted", False):
            agent.max_new_tokens = default_max_tokens_for_provider(config.name)
        return client, config

    def provider_profiles_factory():
        return list_provider_profiles(
            start=getattr(args, "cwd", "."),
            config_path=getattr(args, "config", None),
        )

    agent.model_client_factory = current_model_client_factory
    agent.provider_switch_factory = provider_switch_factory
    agent.provider_profiles_factory = provider_profiles_factory


def _client_string_attr(client, name):
    value = getattr(client, name, "")
    return value if isinstance(value, str) and value else None


def build_arg_parser():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Minimal coding agent for provider profiles backed by OpenAI-compatible or Anthropic-compatible APIs.",
    )
    parser.add_argument("prompt", nargs="*", help="可选的一次性提示词。")
    parser.add_argument("--cwd", default=".", help="工作区目录。")
    parser.add_argument(
        "--config", default=None, help="BunnyByte TOML 配置文件路径。"
    )
    parser.add_argument(
        "--provider",
        default=None,
        help=f"要使用的 provider 配置；默认读取配置文件，未配置时使用 {DEFAULT_PROVIDER}。",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="所选 provider 配置的 API key 覆盖值。",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="所选 provider 配置的模型名覆盖值。",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="所选 provider 配置的 API base URL 覆盖值。",
    )
    parser.add_argument(
        "--openai-timeout",
        type=int,
        default=300,
        help="Provider 请求超时时间（秒）。",
    )
    parser.add_argument(
        "--resume", default=None, help="要恢复的会话 id，或使用 'latest'。"
    )
    parser.add_argument(
        "--memory-dir",
        default=None,
        help="记忆目录；默认使用工作区内的 .bunnybyte/memory。",
    )
    parser.add_argument(
        "--no-auto-dream",
        action="store_true",
        help="关闭自动记忆整理。",
    )
    parser.add_argument(
        "--dream-interval",
        type=float,
        default=24.0,
        help="自动 dream/记忆整理的最小间隔小时数。",
    )
    parser.add_argument(
        "--dream-min-sessions",
        type=int,
        default=5,
        help="触发自动 dream/记忆整理前需要的新会话数量。",
    )
    parser.add_argument(
        "--approval",
        choices=("ask", "auto", "never"),
        default="ask",
        help="高风险工具的审批策略。",
    )
    parser.add_argument(
        "--sandbox",
        choices=("off", "best_effort", "required"),
        default=None,
        help="run_shell 的沙箱模式。",
    )
    parser.add_argument(
        "--sandbox-backend",
        choices=("auto", "bubblewrap", "none"),
        default=None,
        help="run_shell 的沙箱后端。",
    )
    parser.add_argument(
        "--secret-env-name",
        dest="secret_env_names",
        action="append",
        default=[],
        help="额外按密钥处理的环境变量名，用于 trace/report 脱敏。",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=50,
        help="每个请求最多允许的工具/模型迭代次数。",
    )
    parser.add_argument(
        "--max-new-tokens",
        type=int,
        default=None,
        help="每步模型输出 token 上限；默认不设置上限，只有显式传入时才限制。",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.2,
        help="发送给 provider 的采样温度。",
    )
    parser.add_argument(
        "--tui", action="store_true", help="启动 Textual 终端 UI。"
    )
    parser.add_argument(
        "--repl",
        action="store_true",
        help="使用普通行式 REPL，而不是 TUI。",
    )
    return parser


def handle_repl_command(agent, user_input):
    raw_command = ""
    command_args = ""
    command_name = ""
    if str(user_input).startswith("/"):
        raw_command, _, command_args = str(user_input)[1:].partition(" ")
        resolved = resolve_command(raw_command)
        command_name = resolved.name if resolved else raw_command.strip().lower()
        command_args = command_args.strip()

    if user_input in {"/exit", "/quit"}:
        return True, True, ""
    if user_input == "/help":
        return True, False, HELP_DETAILS
    if user_input == "/memory":
        return True, False, agent.memory_command_text()
    if user_input == "/working-memory":
        return True, False, agent.memory_text()
    if user_input.startswith("/remember"):
        _, _, note = user_input.partition(" ")
        if not note.strip():
            return True, False, _format_usage_message("/remember <text>")
        agent.remember_durable_note(note)
        return True, False, "## Memory\n\nSaved to the daily log."
    if user_input == "/dream":
        return True, False, agent.run_dream()
    if user_input == "/skills":
        return True, False, skillslib.render_skills_list(agent.skills)
    if user_input == "/plan" or user_input.startswith("/plan "):
        _, _, raw_topic = user_input.partition(" ")
        topic = raw_topic.strip()
        if not topic:
            return True, False, _format_mode_status(agent)
        path = None
        if " " in topic:
            topic, _, path = topic.partition(" ")
            path = path.strip() or None
        try:
            plan_path = agent.enter_plan_mode(topic, path=path)
        except ValueError as exc:
            return True, False, _format_error(exc)
        return True, False, _format_key_value_section(
            "Plan Mode",
            [("Runtime mode", "plan"), ("Plan path", plan_path)],
        )
    if user_input == "/plan-exit":
        agent.exit_plan_mode()
        return True, False, _format_key_value_section(
            "Runtime Mode",
            [("Runtime mode", "default")],
        )
    if user_input == "/mode":
        return True, False, _format_mode_status(agent)
    if user_input == "/session":
        return True, False, _format_session_status(agent)
    if user_input == "/topic" or user_input.startswith("/topic "):
        _, _, raw_topic = user_input.partition(" ")
        topic = raw_topic.strip()
        if not topic:
            return True, False, _format_key_value_section(
                "Session Topic",
                [("Session topic", agent.session_topic)],
            )
        try:
            topic = agent.set_session_topic(topic)
        except ValueError as exc:
            return True, False, _format_error(exc)
        return True, False, _format_key_value_section(
            "Session Topic",
            [("Session topic", topic)],
        )
    if command_name == "agents":
        return True, False, _format_subagent_status(agent)
    if command_name == "subagent":
        payload, error = parse_subagent_args(command_args)
        if error:
            return True, False, _format_usage_message(error)
        return True, False, agent.run_tool("agent", payload)
    if user_input == "/context":
        return True, False, _format_json_section(
            "Context Usage",
            agent.prompt_metadata("", "")["context_usage"],
        )
    if user_input == "/usage":
        return True, False, _format_usage(agent)
    if command_name == "provider":
        if not command_args:
            return True, False, _format_provider(agent)
        provider = command_args.strip()
        if provider in {"list", "ls"}:
            return True, False, _format_provider_list(agent)
        if " " in provider:
            return True, False, _format_usage_message("/provider [name]")
        try:
            output = _switch_provider(agent, provider)
        except ValueError as exc:
            return True, False, _format_error(exc)
        return True, False, output
    if user_input == "/model" or user_input.startswith("/model "):
        _, _, model = user_input.partition(" ")
        model = model.strip()
        if not model:
            return True, False, _format_model(agent)
        setattr(agent.model_client, "model", model)
        agent.session_event_bus.emit("model_changed", {"model": model})
        agent.refresh_prefix(force=True)
        return True, False, _format_model(agent)
    if user_input == "/history":
        return True, False, _format_history(agent)
    if user_input.startswith("/resume "):
        _, _, target = user_input.partition(" ")
        session_id = _resolve_session_id(agent, target.strip())
        if not session_id:
            return True, False, _format_error("session not found")
        agent.resume_session(session_id)
        return True, False, _format_key_value_section(
            "Session Resumed",
            [("Session id", session_id)],
        )
    if user_input == "/clear":
        session_id = agent.clear_session()
        return True, False, _format_key_value_section(
            "New Session",
            [("Session id", session_id)],
        )
    if user_input == "/compact":
        return True, False, _format_json_section(
            "Compaction Result",
            agent.compact_history(trigger="manual"),
        )
    if user_input == "/reset":
        agent.reset()
        return True, False, "## Session Reset\n\nThe current session was reset."
    command, arguments = skillslib.parse_slash_command(user_input)
    if command == "skill":
        skill_name, _, skill_arguments = arguments.partition(" ")
        if not skill_name.strip():
            return True, False, _format_usage_message("/skill <name> [args]")
        if skill_name.strip() not in agent.skills:
            return True, False, _format_error(f"skill not found: {skill_name.strip()}")
        return True, False, invoke_skill(agent, skill_name.strip(), skill_arguments.strip())
    if command and command in agent.skills:
        return True, False, invoke_skill(agent, command, arguments)
    return False, False, ""


def _format_mode_status(agent):
    rows = [("Runtime mode", agent.runtime_mode)]
    plan_path = getattr(agent.plan_mode, "plan_path", "")
    if plan_path:
        rows.append(("Plan path", plan_path))
    return _format_key_value_section("Runtime Mode", rows)


def _format_session_status(agent):
    task_state = getattr(agent, "current_task_state", None)
    run_id = getattr(task_state, "run_id", "") or ""
    run_dir = str(agent.run_store.run_dir(run_id)) if run_id else "-"
    workers = agent.worker_manager.to_dict()
    items = workers.get("items", [])
    worker_summary = "none"
    if items:
        counts = {}
        for item in items:
            status = str(item.get("status", "unknown") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        worker_summary = ", ".join(
            f"{status}={count}" for status, count in sorted(counts.items())
        )
    status = "pending" if agent.is_pending_session else "active"
    session_path = (
        f"{agent.session_path} (not saved yet)"
        if agent.is_pending_session
        else str(agent.session_path)
    )
    return _format_key_value_section(
        "Session Status",
        [
            ("Session id", agent.session.get("id", "")),
            ("Session status", status),
            ("Session topic", agent.session_topic),
            ("Session path", session_path),
            ("Events path", agent.session_event_bus.path),
            ("Runtime mode", agent.runtime_mode),
            ("Plan path", getattr(agent.plan_mode, "plan_path", "") or "-"),
            ("Last run id", run_id or "-"),
            ("Last run dir", run_dir),
            ("Resume status", agent.resume_state.get("status", "-")),
            ("Worker summary", worker_summary),
        ],
    )


def _format_subagent_status(agent):
    return "\n".join(
        [
            "## Subagents",
            "",
            f"**Worker summary:** {_worker_summary(agent)}",
            "",
            "| Tool | Purpose |",
            "| --- | --- |",
            "| `agent(description, prompt, subagent_type='Explore|worker', write_scope=[])` | Launch a bounded child run. |",
            "| `send_message(to, message)` | Continue an existing worker. |",
            "| `task_stop(task_id)` | Stop a running worker. |",
        ]
    )


def _worker_summary(agent):
    items = agent.worker_manager.to_dict().get("items", [])
    if not items:
        return "none"
    return ", ".join(f"{item.get('id')}:{item.get('status')}" for item in items)


def _format_usage(agent):
    metadata = dict(getattr(agent, "last_completion_metadata", {}) or {})
    context_usage = dict(
        (getattr(agent, "last_prompt_metadata", {}) or {}).get("context_usage", {})
        or {}
    )
    base_url = str(getattr(agent.model_client, "base_url", "") or "")
    return _format_key_value_section(
        "Usage",
        [
            ("Provider profile", getattr(agent.model_client, "provider", "-") or "-"),
            ("Provider protocol", getattr(agent.model_client, "protocol", "-") or "-"),
            ("Model", getattr(agent.model_client, "model", "-") or "-"),
            ("Base URL", sanitize_url(base_url) or "-"),
            ("Prompt cache supported", bool(getattr(agent.model_client, "supports_prompt_cache", False))),
            ("Last input tokens", metadata.get("input_tokens", "unavailable")),
            ("Last output tokens", metadata.get("output_tokens", "unavailable")),
            ("Last cached tokens", metadata.get("cached_tokens", "unavailable")),
            ("Last provider attempts", metadata.get("provider_attempts", "unavailable")),
            ("Last provider retry count", metadata.get("provider_retry_count", "unavailable")),
            ("Last provider error", metadata.get("provider_error", "unavailable")),
            (
                "Context usage",
                f"{context_usage.get('total_estimated_tokens', '-')}/{context_usage.get('context_window', '-')}",
            ),
        ],
    )


def _format_provider(agent):
    base_url = str(getattr(agent.model_client, "base_url", "") or "")
    return _format_key_value_section(
        "Provider",
        [
            ("Provider", getattr(agent.model_client, "provider", "-") or "-"),
            ("Protocol", getattr(agent.model_client, "protocol", "-") or "-"),
            ("Model", getattr(agent.model_client, "model", "-") or "-"),
            ("Base URL", sanitize_url(base_url) or "-"),
            ("Max new tokens", getattr(agent, "max_new_tokens", "-")),
        ],
    )


def provider_profiles_for_agent(agent):
    factory = getattr(agent, "provider_profiles_factory", None)
    if callable(factory):
        return list(factory())
    return list_provider_profiles(start=getattr(agent, "root", "."))


def _format_provider_list(agent):
    current = str(getattr(agent.model_client, "provider", "") or "")
    lines = [
        "## Provider Profiles",
        "",
        "| Active | Profile | Protocol | Default model | Base URL | Switch |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for profile in provider_profiles_for_agent(agent):
        marker = "yes" if profile.name == current else ""
        lines.append(
            f"| {marker} | `{profile.name}` | `{profile.protocol}` | "
            f"`{profile.model}` | `{sanitize_url(profile.base_url) or '-'}` | "
            f"`/provider {profile.name}` |"
        )
    lines.extend(["", "Aliases: `gpt` → `openai`, `claude` → `anthropic`."])
    return "\n".join(lines)


def _switch_provider(agent, provider):
    switcher = getattr(agent, "provider_switch_factory", None)
    if not callable(switcher):
        raise ValueError("provider switching is unavailable for this runtime")
    client, config = switcher(provider)
    previous_provider = getattr(agent.model_client, "provider", "") or ""
    previous_model = getattr(agent.model_client, "model", "") or ""
    agent.model_client = client
    agent.last_completion_metadata = {}
    agent.refresh_prefix(force=True)
    agent.resume_state = agent.evaluate_resume_state()
    agent.session_event_bus.emit(
        "provider_changed",
        {
            "previous_provider": previous_provider,
            "previous_model": previous_model,
            "provider": config.name,
            "protocol": config.protocol,
            "model": config.model,
            "base_url": config.base_url,
        },
    )
    return _format_provider(agent)


def _format_model(agent):
    return _format_key_value_section(
        "Model",
        [("Model", getattr(agent.model_client, "model", "-") or "-")],
    )


def _format_history(agent):
    rows = agent.session_store.list_sessions()
    if not rows:
        return "(no sessions)"
    lines = [
        "## Session History",
        "",
        "Use `/resume <index>` or `/resume latest` to continue a saved session.",
        "",
        "| # | Topic | ID | Mode | Turns | Updated | Last answer |",
        "| ---: | --- | --- | --- | ---: | --- | --- |",
    ]
    for row in rows:
        lines.append(
            "| "
            f"{row['index']} | "
            f"{_markdown_table_cell(row.get('topic', ''))} | "
            f"`{_markdown_table_cell(row.get('id', ''))}` | "
            f"{_markdown_table_cell(row.get('runtime_mode', ''))} | "
            f"{row.get('history_count', 0)} | "
            f"{_markdown_table_cell(row.get('updated_at', ''))} | "
            f"{_markdown_table_cell(_compact_summary(row.get('last_final_answer', '')))} |"
        )
    return "\n".join(lines)


def _compact_summary(value, limit=96):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _markdown_table_cell(value):
    text = str(value or "-").replace("\n", " ")
    return text.replace("|", "\\|")


def _format_key_value_section(title, rows):
    lines = [f"## {title}", "", "| Field | Value |", "| --- | --- |"]
    for label, value in rows:
        lines.append(f"| {label} | {_format_value_cell(value)} |")
    return "\n".join(lines)


def _format_json_section(title, payload):
    return "\n".join(
        [
            f"## {title}",
            "",
            "```json",
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
        ]
    )


def _format_usage_message(usage):
    text = str(usage or "").strip()
    if text.lower().startswith("usage:"):
        text = text.split(":", 1)[1].strip()
    return "\n".join(["## Usage", "", f"`{text}`"])


def _format_error(error):
    message = str(error or "").strip()
    return "\n".join(["## Error", "", message])


def _format_value_cell(value):
    text = _compact_summary(value, limit=160)
    text = text.replace("\n", " ")
    escaped = text.replace("|", "\\|") or "-"
    if text != "-" and re.fullmatch(r"[A-Za-z0-9_.:/@+=-]+", text):
        return f"`{escaped}`"
    return escaped


def _resolve_session_id(agent, target):
    if target == "latest":
        return agent.session_store.latest(include_empty=False)
    rows = agent.session_store.list_sessions()
    if target.isdigit():
        index = int(target)
        for row in rows:
            if row["index"] == index:
                return row["id"]
    for row in rows:
        if row["id"] == target:
            return row["id"]
    lowered = target.lower()
    exact_topic_matches = [
        row["id"] for row in rows if str(row.get("topic", "")).lower() == lowered
    ]
    if len(exact_topic_matches) == 1:
        return exact_topic_matches[0]
    contains_topic_matches = [
        row["id"]
        for row in rows
        if lowered and lowered in str(row.get("topic", "")).lower()
    ]
    if len(contains_topic_matches) == 1:
        return contains_topic_matches[0]
    return ""


def _cli_ask_user(question, choices):
    if choices:
        print(question)
        for index, choice in enumerate(choices, start=1):
            print(f"{index}. {choice}")
        answer = input("> ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]
        return answer
    return input(question + " ").strip()


def _stream_print(text, *, chunk_size=12, delay=0.012):
    text = str(text or "")
    if not text:
        print("")
        return
    for index in range(0, len(text), chunk_size):
        sys.stdout.write(text[index : index + chunk_size])
        sys.stdout.flush()
        time.sleep(delay)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _print_turn_stream(agent, user_message):
    final_answer = ""
    stream_open = False
    stream_buffer = ""
    stream_preview = ""
    for event in agent.engine.run_turn(user_message):
        event_type = str(event.get("type", ""))
        if event_type == "model_requested":
            stream_buffer = ""
            stream_preview = ""
            stream_open = False
            continue
        if event_type == "model_delta":
            stream_buffer += str(event.get("content", ""))
            next_preview = _model_stream_preview(stream_buffer)
            if not next_preview:
                continue
            if not stream_open:
                sys.stdout.write("[model]\n")
                stream_open = True
            if next_preview.startswith(stream_preview):
                sys.stdout.write(next_preview[len(stream_preview) :])
            else:
                sys.stdout.write(next_preview)
            stream_preview = next_preview
            sys.stdout.flush()
            continue
        if event_type == "model_parsed":
            if stream_open:
                sys.stdout.write("\n")
                sys.stdout.flush()
                stream_open = False
            if event.get("kind") not in {"final"}:
                stream_buffer = ""
                stream_preview = ""
            continue
        if event_type == "tool_call":
            sys.stdout.write(
                f"\n[tool] {event.get('name', '')} "
                f"{json.dumps(event.get('args', {}), ensure_ascii=False, sort_keys=True)}\n"
            )
            sys.stdout.flush()
            continue
        if event_type == "tool_result":
            sys.stdout.write(
                f"[tool result] {event.get('name', '')}: "
                f"{clip(event.get('content', ''), 240)}\n"
            )
            sys.stdout.flush()
            continue
        if event_type in {"assistant_preamble", "retry", "runtime_notice", "final", "stop"}:
            content = str(event.get("content", ""))
            if event_type in {"final", "stop"}:
                final_answer = content
            if stream_preview.strip() and content.strip() == stream_preview.strip():
                stream_buffer = ""
                stream_preview = ""
                continue
            _stream_print(content)
            stream_buffer = ""
            stream_preview = ""
    return final_answer


def _model_stream_preview(content):
    text = str(content or "")
    marker = "<final>"
    if marker in text:
        body = text.split(marker, 1)[1]
        if "</final>" in body:
            body = body.split("</final>", 1)[0]
        return body
    return text


def _drain_idle_worker_notifications(agent):
    notifications = agent.engine.drain_worker_notifications()
    for notification in notifications:
        print(f"\n[worker notification]\n{notification}")
    return notifications


def interaction_mode(args):
    if args.prompt:
        return "one_shot"
    if getattr(args, "repl", False):
        return "repl"
    if getattr(args, "tui", False) or sys.stdin.isatty():
        return "tui"
    return "repl"


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    try:
        agent = build_agent(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    mode = interaction_mode(args)
    if mode == "tui":
        from .tui.app import BunnyByteTuiApp

        BunnyByteTuiApp(agent).run()
        return 0

    model = getattr(
        agent.model_client, "model", getattr(args, "model", DEFAULT_OPENAI_MODEL)
    )
    host = getattr(
        agent.model_client,
        "base_url",
        getattr(args, "base_url", DEFAULT_OPENAI_BASE_URL),
    )
    print(build_welcome(agent, model=model, host=host))

    if mode == "one_shot":
        # one-shot 模式：只跑一次 ask，不进入 REPL 循环。
        prompt = " ".join(args.prompt).strip()
        if prompt:
            print()
            try:
                handled, _, output = handle_repl_command(agent, prompt)
                if handled:
                    print(output)
                else:
                    _print_turn_stream(agent, prompt)
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 1
        return 0

    while True:
        # 交互模式：每次读取一条用户输入，交给同一个 agent，
        # 因此 session history 和 working memory 会跨轮延续。
        _drain_idle_worker_notifications(agent)
        try:
            user_input = input("\nbunnybyte> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return 0

        if not user_input:
            continue
        handled, should_exit, output = handle_repl_command(agent, user_input)
        if should_exit:
            return 0
        if handled:
            print(output)
            continue

        print()
        try:
            _print_turn_stream(agent, user_input)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
