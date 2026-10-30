"""Child runtime construction for worker tasks."""

from .mcp_tools import register_mcp_tools
from .workspace import WorkspaceContext


def build_child_runtime(parent, subagent_type, write_scope):
    from .runtime import BunnyByte

    child = BunnyByte(
        model_client=new_model_client(parent),
        workspace=WorkspaceContext.build(parent.workspace.cwd, repo_root_override=parent.root),
        session_store=parent.session_store,
        run_store=parent.run_store,
        approval_policy="never" if subagent_type == "Explore" else "auto",
        max_steps=parent.max_steps,
        max_new_tokens=parent.max_new_tokens,
        depth=parent.depth + 1,
        max_depth=parent.max_depth,
        read_only=subagent_type == "Explore"
        or (subagent_type == "worker" and not write_scope),
        auto_dream=parent.auto_dream,
        secret_env_names=parent.secret_env_names,
        shell_env_allowlist=parent.shell_env_allowlist,
        feature_flags=parent.feature_flags,
        write_scope=write_scope,
        model_client_factory=getattr(parent, "model_client_factory", None),
        sandbox_config=getattr(parent, "sandbox_config", None),
        ask_user_callback=getattr(parent, "ask_user_callback", None),
        session={
            "id": f"{parent.session['id']}-{subagent_type.lower()}-{len(parent.worker_manager.state.get('items', [])) + 1}",
            "created_at": parent.session.get("created_at"),
            "topic": f"# Worker: {subagent_type}",
            "workspace_root": parent.workspace.repo_root,
            "history": [],
            "kind": "worker",
        },
    )
    child.cost_meter = parent.cost_meter  # worker 花的钱算进父 agent 的本轮和会话
    child.secret_vault = parent.secret_vault  # 同一个密钥在父子 agent 里是同一个占位符
    child.hooks = parent.hooks  # pre_tool / post_tool 对子 agent 同样生效；on_final 只在主 agent 上跑
    if getattr(parent, "mcp", None) is not None:
        # 共用父 agent 的连接；需要审批的 MCP 工具不在 worker / readonly 档位里，后台 worker 只拿到免审批的那些
        register_mcp_tools(child, parent.mcp)
    child.set_tool_profile("readonly" if subagent_type == "Explore" else "worker")
    child.refresh_prefix(force=True)
    return child


def new_model_client(parent):
    factory = getattr(parent, "model_client_factory", None)
    if factory is not None:
        return factory()
    return parent.model_client
