import subprocess

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


def test_fork_from_turn_truncates_history_and_restores_workspace(tmp_path):
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

    agent.fork_session(first_turn)

    assert [item["content"] for item in agent.session["history"] if item["role"] == "user"] == ["write one"]
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "one\n"
    assert agent.session["fork"]["forked_from_turn_id"] == first_turn


def test_fork_without_restorable_snapshot_falls_back_to_history_only(tmp_path):
    agent = build_agent(tmp_path, ["<final>Ready.</final>"])
    agent.ask("start")
    checkpoint_id = agent.session["checkpoints"]["current_id"]
    checkpoint = agent.session["checkpoints"]["items"][checkpoint_id]
    checkpoint.pop("workspace_snapshot", None)
    checkpoint.pop("workspace_snapshot_truncated", None)
    parent_id = agent.session["id"]

    fork = agent.fork_session("latest")

    assert agent.session["id"] != parent_id
    assert fork["workspace_restored"] is False
    assert "history only" in fork["restore_warning"]
    assert agent.session["checkpoints"]["current_id"] == ""
def test_git_checkpoint_restores_tracked_and_untracked_files(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=tmp_path, check=True, capture_output=True)
    agent = BunnyByte(
        model_client=ScriptedModelClient(
            [
                '<tool>{"name":"run_shell","args":{"command":"printf \'one\\n\' > README.md && printf \'created\\n\' > new.txt","timeout":20}}</tool>',
                "<final>One.</final>",
                '<tool>{"name":"run_shell","args":{"command":"printf \'two\\n\' > README.md && printf \'changed\\n\' > new.txt","timeout":20}}</tool>',
                "<final>Two.</final>",
            ]
        ),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        approval_policy="auto",
    )

    assert agent.ask("make one") == "One."
    first_turn = agent.session["history"][-1]["turn_id"]
    first_checkpoint = agent.current_checkpoint()
    assert first_checkpoint["checkpoint_backend"] == "git"
    assert (tmp_path / first_checkpoint["checkpoint_path"]).is_file()
    assert agent.ask("make two") == "Two."
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "two\n"
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "changed\n"

    fork = agent.fork_session(first_turn)

    assert fork["workspace_restored"] is True
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "one\n"
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "created\n"


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
