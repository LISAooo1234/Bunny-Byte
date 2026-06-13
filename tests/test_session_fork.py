from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.testing import ScriptedModelClient


def build_agent(tmp_path, outputs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return BunnyByte(
        model_client=ScriptedModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        approval_policy="auto",
    )


def test_fork_latest_creates_child_session_and_keeps_parent_unchanged(tmp_path):
    agent = build_agent(tmp_path, ["<final>First answer.</final>"])
    assert agent.ask("first request") == "First answer."
    parent_id = agent.session["id"]
    parent_history = list(agent.session["history"])

    fork = agent.fork_session("latest")

    assert agent.session["id"] != parent_id
    assert agent.session["parent_session_id"] == parent_id
    assert agent.session["fork"]["forked_from_event_id"] == parent_history[-1]["event_id"]
    assert agent.session["history"] == parent_history
    assert agent.session_store.load(parent_id)["history"] == parent_history
    assert fork["session_id"] == agent.session["id"]
    assert fork["workspace_restored"] is False


def test_fork_from_turn_truncates_history_but_leaves_workspace_unchanged(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"write_file","args":{"path":"notes.txt","content":"one\\n"}}</tool>',
            "<final>Wrote one.</final>",
            '<tool>{"name":"write_file","args":{"path":"notes.txt","content":"two\\n"}}</tool>',
            "<final>Wrote two.</final>",
        ],
    )
    assert agent.ask("write one") == "Wrote one."
    first_turn = agent.session["history"][-1]["turn_id"]
    assert agent.ask("write two") == "Wrote two."
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "two\n"

    fork = agent.fork_session(first_turn)

    assert [item["content"] for item in agent.session["history"] if item["role"] == "user"] == ["write one"]
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "two\n"
    assert agent.session["fork"]["forked_from_turn_id"] == first_turn
    assert fork["workspace_restored"] is False
    assert "session-only" in fork["restore_warning"]


def test_session_checkpoint_does_not_store_workspace_snapshot(tmp_path):
    agent = build_agent(tmp_path, ["<final>Ready.</final>"])

    agent.ask("start")
    checkpoint = agent.current_checkpoint()

    assert checkpoint["checkpoint_backend"] == "session"
    assert "workspace_snapshot" not in checkpoint
    assert "checkpoint_path" not in checkpoint


def test_fork_command_switches_to_child_session(tmp_path):
    from bunnybyte.cli import handle_repl_command

    agent = build_agent(tmp_path, ["<final>Ready.</final>"])
    agent.ask("start")
    parent_id = agent.session["id"]

    handled, should_exit, output = handle_repl_command(agent, "/fork latest")

    assert handled is True
    assert should_exit is False
    assert "Session Forked" in output
    assert agent.session["id"] != parent_id
    assert f"`{parent_id}`" in output
    assert "Workspace restored" in output
