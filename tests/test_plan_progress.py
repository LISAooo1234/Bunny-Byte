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
