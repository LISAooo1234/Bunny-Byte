import json
import subprocess

import pytest

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.cli import handle_repl_command
from bunnybyte.core.workspace_snapshots import legacy_snapshot
from bunnybyte.testing import ScriptedModelClient

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0, reason="git is required"
)


def tool(name, **args):
    return "<tool>" + json.dumps({"name": name, "args": args}) + "</tool>"


def build_agent(tmp_path, outputs):
    return BunnyByte(
        model_client=ScriptedModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        approval_policy="auto",
    )


def undo(agent, args=""):
    handled, _, output = handle_repl_command(agent, ("/undo " + args).strip())
    assert handled
    return output


def test_undo_walks_back_one_turn_at_a_time_and_leaves_unrelated_files_alone(tmp_path):
    (tmp_path / "a.py").write_text("old\n")
    (tmp_path / "keep.txt").write_text("keep\n")
    agent = build_agent(
        tmp_path,
        [
            tool("write_file", path="b.py", content="print(2)\n"),
            "<final>first done</final>",
            tool("read_file", path="a.py"),
            tool("write_file", path="a.py", content="new\n"),
            tool("write_file", path="pkg/c.py", content="c\n"),
            tool("run_shell", command="rm keep.txt"),
            "<final>second done</final>",
        ],
    )
    agent.ask("first")
    agent.ask("second")
    assert (tmp_path / "a.py").read_text() == "new\n" and not (tmp_path / "keep.txt").exists()
    (tmp_path / "mine.txt").write_text("written by me after the turns\n")

    output = undo(agent)

    assert "已撤销" in output and "second" in output
    assert (tmp_path / "a.py").read_text() == "old\n"
    assert (tmp_path / "keep.txt").read_text() == "keep\n"
    assert not (tmp_path / "pkg").exists()
    assert (tmp_path / "b.py").exists()
    assert (tmp_path / "mine.txt").exists()

    undo(agent)
    assert not (tmp_path / "b.py").exists()
    assert (tmp_path / "mine.txt").exists()
    assert "没有可撤销" in undo(agent)
    note = agent.session["history"][-1]["content"]
    assert "不要假设" in note


def test_undo_list_shows_every_turn_with_its_files_and_state(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            tool("write_file", path="one.txt", content="1\n"),
            "<final>ok</final>",
            tool("write_file", path="two.txt", content="2\n"),
            "<final>ok</final>",
            "<final>just talking</final>",
        ],
    )
    agent.ask("make one")
    agent.ask("make two")
    agent.ask("explain something")
    undo(agent)

    listing = undo(agent, "list")

    assert listing.index("make two") < listing.index("make one")
    assert "explain something" not in listing
    assert "已撤销" in listing and "生效中" in listing
    assert "`one.txt`" in listing and "`two.txt`" in listing


def test_undo_refuses_atomically_when_the_user_edited_a_file_since_and_force_overrides(tmp_path):
    (tmp_path / "a.txt").write_text("original\n")
    agent = build_agent(
        tmp_path,
        [
            tool("read_file", path="a.txt"),
            tool("write_file", path="a.txt", content="agent version\n"),
            tool("write_file", path="b.txt", content="agent b\n"),
            "<final>done</final>",
        ],
    )
    agent.ask("edit things")
    assert (tmp_path / "a.txt").read_text() == "agent version\n"
    (tmp_path / "a.txt").write_text("my own edit\n")

    refused = undo(agent)

    assert "没有撤销" in refused and "`a.txt`" in refused and "/undo --force" in refused
    assert (tmp_path / "a.txt").read_text() == "my own edit\n"
    assert (tmp_path / "b.txt").exists()

    forced = undo(agent, "--force")

    assert "已撤销" in forced
    assert (tmp_path / "a.txt").read_text() == "original\n"
    assert not (tmp_path / "b.txt").exists()


def test_gitignored_write_targets_are_still_reported_and_undone(tmp_path):
    (tmp_path / ".gitignore").write_text(".env\ndist/\n")
    (tmp_path / ".env").write_text("KEY=1\n")
    (tmp_path / "other.txt").write_text("x\n")
    agent = build_agent(
        tmp_path,
        [
            tool("read_file", path=".env"),
            tool("write_file", path=".env", content="KEY=2\n"),
            tool("write_file", path="dist/app.js", content="js\n"),
            "<final>done</final>",
        ],
    )
    _, metadata = agent.run_tool_with_metadata("write_file", {"path": "dist/first.js", "content": "1\n"})
    assert metadata["affected_paths"] == ["dist/first.js"]
    agent.ask("update env")
    assert (tmp_path / ".env").read_text() == "KEY=2\n"

    assert "已撤销" in undo(agent)

    assert (tmp_path / ".env").read_text() == "KEY=1\n"
    assert not (tmp_path / "dist" / "app.js").exists()
    assert (tmp_path / "dist" / "first.js").exists()


def test_checkpoints_store_a_tree_instead_of_copying_the_workspace(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n")
    (tmp_path / "node_modules" / "dep").mkdir(parents=True)
    (tmp_path / "node_modules" / "dep" / "index.js").write_text("module.exports = 1\n")
    agent = build_agent(
        tmp_path,
        [tool("read_file", path="src/app.py"), tool("write_file", path="src/app.py", content="x = 2\n"), "<final>ok</final>"],
    )

    agent.ask("change x")

    checkpoint = agent.current_checkpoint()
    assert checkpoint["shadow_tree"]
    assert checkpoint["rollback_snapshot_available"] is True
    assert checkpoint["rollback_file_count"] == 1
    assert not list(tmp_path.glob(".bunnybyte/checkpoints/*/workspace"))
    assert (tmp_path / checkpoint["rollback_snapshot_path"]).is_file()
    assert not (tmp_path / ".bunnybyte" / "shadow.git" / "index.lock").exists()


def test_rollback_restores_the_files_from_a_tree_checkpoint(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            tool("write_file", path="notes.txt", content="one\n"),
            "<final>one</final>",
            tool("write_file", path="notes.txt", content="two\n"),
            tool("write_file", path="extra.txt", content="later\n"),
            "<final>two</final>",
        ],
    )
    agent.ask("write one")
    first_turn = agent.session["history"][-1]["turn_id"]
    agent.ask("write two")

    result = agent.rollback_session(first_turn)

    assert result["workspace_restored"] is True
    assert (tmp_path / "notes.txt").read_text() == "one\n"
    assert not (tmp_path / "extra.txt").exists()
    assert agent.session["undo_log"] == []


def test_fork_starts_with_an_empty_undo_log(tmp_path):
    agent = build_agent(tmp_path, [tool("write_file", path="a.txt", content="a\n"), "<final>ok</final>"])
    agent.ask("make a")
    assert agent.session["undo_log"]

    agent.fork_session("latest")

    assert agent.session["undo_log"] == []
    assert "没有可撤销" in undo(agent)


def test_undo_log_is_saved_with_the_session(tmp_path):
    agent = build_agent(tmp_path, [tool("write_file", path="a.txt", content="a\n"), "<final>ok</final>"])
    agent.ask("make a")

    saved = agent.session_store.load(agent.session["id"])

    assert [item["status"] for item in saved["undo_log"]] == ["applied"]
    assert saved["undo_log"][0]["request"] == "make a"


def test_undo_is_refused_while_a_worker_is_running(tmp_path):
    agent = build_agent(tmp_path, [tool("write_file", path="a.txt", content="a\n"), "<final>ok</final>"])
    agent.ask("make a")
    agent.worker_manager.state["items"].append({"id": "worker_1", "status": "running"})

    output = undo(agent)

    assert "无法撤销" in output and "子任务" in output
    assert (tmp_path / "a.txt").exists()
    agent.worker_manager.state["items"].clear()
    assert "已撤销" in undo(agent)


def test_undo_warns_when_git_head_moved_during_the_turn(tmp_path):
    # 身份写进仓库配置而不是 GIT_AUTHOR_* 环境变量：run_shell 只传白名单环境变量，
    # 没有全局 git 身份的 CI 机器上，模型那条 `git commit` 会因此失败。
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("v1\n")
    subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "v1"], cwd=tmp_path, check=True)
    agent = build_agent(
        tmp_path,
        [
            tool("read_file", path="a.txt"),
            tool("write_file", path="a.txt", content="v2\n"),
            tool("run_shell", command="git add a.txt && git commit -q -m v2"),
            "<final>committed</final>",
        ],
    )
    agent.ask("change and commit")
    commits = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=tmp_path, capture_output=True, text=True)
    assert commits.stdout.strip() == "2"

    output = undo(agent)

    assert "HEAD" in output
    assert (tmp_path / "a.txt").read_text() == "v1\n"


def test_without_git_everything_still_works_and_undo_explains_why(tmp_path, monkeypatch):
    monkeypatch.setenv("BUNNYBYTE_SHADOW", "off")
    agent = build_agent(
        tmp_path,
        [
            tool("write_file", path="notes.txt", content="one\n"),
            "<final>one</final>",
            tool("write_file", path="notes.txt", content="two\n"),
            "<final>two</final>",
        ],
    )
    agent.ask("write one")
    first_turn = agent.session["history"][-1]["turn_id"]
    agent.ask("write two")

    checkpoint = agent.current_checkpoint()
    assert "shadow_tree" not in checkpoint
    assert list(tmp_path.glob(".bunnybyte/checkpoints/*/workspace"))
    assert "无法撤销" in undo(agent) and "BUNNYBYTE_SHADOW" in undo(agent)

    agent.rollback_session(first_turn)

    assert (tmp_path / "notes.txt").read_text() == "one\n"


def test_trees_and_hash_dicts_can_be_compared_when_git_fails_midway(tmp_path):
    (tmp_path / "a.txt").write_text("a\n")
    (tmp_path / "b.txt").write_text("b\n")
    agent = build_agent(tmp_path, ["<final>ok</final>"])
    before_tree = agent.capture_workspace_snapshot()
    assert isinstance(before_tree, str)
    (tmp_path / "a.txt").write_text("changed\n")
    (tmp_path / "c.txt").write_text("c\n")
    (tmp_path / "b.txt").unlink()

    after_dict = legacy_snapshot(tmp_path)
    paths, summaries = agent.diff_workspace_snapshots(before_tree, after_dict)

    assert paths == ["a.txt", "b.txt", "c.txt"]
    assert summaries == ["modified:a.txt", "deleted:b.txt", "created:c.txt"]


def test_legacy_snapshot_sees_a_rewrite_with_the_same_size_right_after_the_first_hash(tmp_path):
    (tmp_path / "a.txt").write_text("aaaa\n")
    first = legacy_snapshot(tmp_path)

    (tmp_path / "a.txt").write_text("bbbb\n")

    assert legacy_snapshot(tmp_path) != first


def test_legacy_snapshot_skips_dependency_directories(tmp_path):
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "i.js").write_text("1\n")
    (tmp_path / "src.py").write_text("1\n")

    assert sorted(legacy_snapshot(tmp_path)) == ["src.py"]


def test_undo_list_mentions_large_files_that_were_left_out_of_the_snapshot(tmp_path, monkeypatch):
    from bunnybyte.core import shadow_snapshots

    monkeypatch.setattr(shadow_snapshots, "MAX_UNTRACKED_BYTES", 100)
    agent = build_agent(
        tmp_path,
        [tool("run_shell", command="python3 -c \"open('big.bin','wb').write(b'x' * 500); open('small.txt','w').write('hi')\""), "<final>ok</final>"],
    )

    agent.ask("make files")
    listing = undo(agent, "list")

    assert "`small.txt`" in listing
    assert "没有纳入快照" in listing and "`big.bin`" in listing
    undo(agent)
    assert not (tmp_path / "small.txt").exists()
    assert (tmp_path / "big.bin").exists()


def test_undo_keeps_a_gitignored_file_that_existed_before_the_agent_overwrote_it(tmp_path):
    (tmp_path / ".gitignore").write_text(".env\n")
    (tmp_path / ".env").write_text("SECRET=original\n")
    agent = build_agent(
        tmp_path,
        [
            tool("run_shell", command="python3 -c \"print('warming up')\""),
            tool("read_file", path=".env"),
            tool("write_file", path=".env", content="SECRET=changed\n"),
            "<final>done</final>",
        ],
    )
    agent.ask("rotate the secret")
    assert (tmp_path / ".env").read_text() == "SECRET=changed\n"

    output = undo(agent)

    assert "已撤销" in output
    assert (tmp_path / ".env").read_text() == "SECRET=original\n"


def test_rollback_does_not_delete_gitignored_files_the_agent_started_tracking_later(tmp_path):
    (tmp_path / ".gitignore").write_text(".env\n")
    (tmp_path / ".env").write_text("SECRET=original\n")
    agent = build_agent(
        tmp_path,
        [
            tool("write_file", path="notes.txt", content="one\n"),
            "<final>one</final>",
            tool("read_file", path=".env"),
            tool("write_file", path=".env", content="SECRET=changed\n"),
            "<final>two</final>",
        ],
    )
    agent.ask("write notes")
    first_turn = agent.session["history"][-1]["turn_id"]
    agent.ask("rotate the secret")

    agent.rollback_session(first_turn)

    assert (tmp_path / ".env").exists()


def test_a_rejected_tool_call_does_not_reuse_the_previous_turns_tree_for_its_checkpoint(tmp_path):
    from bunnybyte.core.shadow_snapshots import ShadowStore

    agent = build_agent(
        tmp_path,
        [
            tool("write_file", path="first.txt", content="1\n"),
            "<final>one</final>",
            tool("write_file", path="../outside.txt", content="nope\n"),
            "<final>two</final>",
        ],
    )
    agent.ask("write first")
    agent._tool_tree_hint = agent.capture_workspace_snapshot()  # a leftover from an earlier tool call
    (tmp_path / "user.txt").write_text("created by me between the turns\n")

    agent.ask("try to write outside the workspace")

    trees = [
        item["shadow_tree"]
        for item in agent.checkpoint_state()["items"].values()
        if item.get("shadow_tree") and item["summary"].startswith("tool_executed")
    ]
    store = ShadowStore.for_root(tmp_path)
    assert trees and "user.txt" in store.tree_blobs(trees[-1])


def test_a_partly_failed_rollback_says_which_files_could_not_be_restored(tmp_path, monkeypatch):
    from bunnybyte.core.shadow_snapshots import ShadowStore

    agent = build_agent(
        tmp_path,
        [
            tool("write_file", path="notes.txt", content="one\n"),
            "<final>one</final>",
            tool("write_file", path="notes.txt", content="two\n"),
            "<final>two</final>",
        ],
    )
    agent.ask("write one")
    first_turn = agent.session["history"][-1]["turn_id"]
    agent.ask("write two")

    def broken(target, mode, data):
        raise OSError("disk says no")

    monkeypatch.setattr(ShadowStore, "_write_entry", staticmethod(broken))

    with pytest.raises(ValueError) as caught:
        agent.rollback_session(first_turn)

    assert "notes.txt" in str(caught.value) and "没能还原" in str(caught.value)
