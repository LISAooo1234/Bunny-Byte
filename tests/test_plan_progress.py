from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.testing import ScriptedModelClient


def build_agent(tmp_path, outputs=None, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    return BunnyByte(
        model_client=ScriptedModelClient(outputs or []),
        workspace=workspace,
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        approval_policy="auto",
        **kwargs,
    )


def rendered_text(widget) -> str:
    rendered = widget.render()
    return getattr(rendered, "plain", str(rendered))


def test_plan_mode_prompt_describes_interactive_progress_workflow(tmp_path):
    agent = build_agent(tmp_path)
    agent.enter_plan_mode("interactive-plan")

    prompt = agent.prompt("plan the refactor")

    assert "Runtime mode: plan" in prompt
    assert "use ask_user before committing to a plan" in prompt
    assert "Use todo_add/todo_update/todo_list as the progress ledger" in prompt
    assert "objective, assumptions, user choices, steps, validation" in prompt
    assert (
        "relative to the current working directory artifact root, not relative to the BunnyByte source directory"
        in prompt
    )


def test_plan_mode_allows_non_bunnybyte_workspace(tmp_path):
    workspace = tmp_path / "client-project"
    workspace.mkdir()
    agent = build_agent(workspace)

    plan_path = agent.enter_plan_mode("client migration")

    assert plan_path == ".bunnybyte/plans/client-migration-plan.md"
    assert agent.root == workspace
    assert agent.run_tool(
        "write_file",
        {"path": plan_path, "content": "# Client migration plan\n"},
    ).startswith("wrote .bunnybyte/plans/client-migration-plan.md")
    assert (workspace / plan_path).read_text(encoding="utf-8") == "# Client migration plan\n"


def test_plan_mode_artifact_lives_under_launch_cwd_inside_repo(tmp_path):
    repo = tmp_path / "repo"
    app = repo / "apps" / "web"
    app.mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(app, repo_root_override=repo)
    agent = BunnyByte(
        model_client=ScriptedModelClient([]),
        workspace=workspace,
        session_store=SessionStore(repo / ".bunnybyte" / "sessions"),
        approval_policy="auto",
    )

    plan_path = agent.enter_plan_mode("frontend migration")

    assert plan_path == "apps/web/.bunnybyte/plans/frontend-migration-plan.md"
    assert agent.run_tool(
        "write_file",
        {"path": plan_path, "content": "# Frontend migration\n"},
    ).startswith("wrote apps/web/.bunnybyte/plans/frontend-migration-plan.md")
    assert (
        app / ".bunnybyte" / "plans" / "frontend-migration-plan.md"
    ).read_text(encoding="utf-8") == "# Frontend migration\n"


def test_plan_mode_rejects_write_capable_worker_agents(tmp_path):
    agent = build_agent(tmp_path)
    agent.enter_plan_mode("interactive-plan")

    result = agent.run_tool(
        "agent",
        {
            "description": "Patch code",
            "prompt": "Modify files",
            "subagent_type": "worker",
        },
    )

    assert "invalid arguments" in result
    assert "plan mode only allows Explore agents" in result


def test_progress_panel_renders_todo_ledger_and_hides_when_empty(tmp_path):
    from bunnybyte.tui.widgets import ProgressPanel

    agent = build_agent(tmp_path)
    panel = ProgressPanel()
    panel.update_agent(agent)
    assert panel.has_class("hidden")

    agent.todo_ledger.add("Clarify preferences", status="done")
    agent.todo_ledger.add("Draft plan", status="in_progress")
    agent.todo_ledger.add("Validate changes", status="pending", note="after implementation")

    panel.update_agent(agent)
    text = rendered_text(panel)

    assert not panel.has_class("hidden")
    assert "Progress" in text
    assert "✓ Clarify preferences" in text
    assert "→ Draft plan" in text
    assert "• Validate changes — after implementation" in text
