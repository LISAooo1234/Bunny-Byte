import io
import json
import subprocess

import pytest

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.cli import handle_repl_command, main
from bunnybyte.commands.diff_commands import parse_diff_args
from bunnybyte.core.turn_diff import render_changes, unified
from bunnybyte.replay import list_runs, run_replay_command
from bunnybyte.testing import ScriptedModelClient

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0, reason="git is required"
)

GITHUB_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def tool(name, **args):
    return "<tool>" + json.dumps({"name": name, "args": args}) + "</tool>"


def build_agent(tmp_path, outputs, **kwargs):
    kwargs.setdefault("approval_policy", "auto")
    return BunnyByte(
        model_client=ScriptedModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        **kwargs,
    )


def command(agent, text):
    handled, _, output = handle_repl_command(agent, text)
    assert handled
    return output


def replay(tmp_path, *args):
    out = io.StringIO()
    code = run_replay_command([*args, "--cwd", str(tmp_path)], stdout=out)
    return code, out.getvalue()


def two_turns(tmp_path):
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a - b\n")
    (tmp_path / "old.txt").write_text("bye\n")
    agent = build_agent(
        tmp_path,
        [
            tool("write_file", path="notes.md", content="# notes\n"),
            "<final>first</final>",
            tool("read_file", path="app.py"),
            tool("patch_file", path="app.py", old_text="return a - b", new_text="return a + b"),
            tool("write_file", path="pkg/new.py", content="X = 1\n"),
            tool("run_shell", command="rm old.txt"),
            "<final>fixed add</final>",
        ],
    )
    agent.ask("write notes")
    agent.ask("fix add")
    return agent


def test_diff_shows_the_newest_turn_with_stats_and_older_turns_by_number(tmp_path):
    agent = two_turns(tmp_path)

    latest = command(agent, "/diff")

    assert "fix add" in latest
    assert "-    return a - b" in latest and "+    return a + b" in latest
    assert "+++ b/pkg/new.py" in latest and "--- a/old.txt" in latest and "+++ /dev/null" in latest
    assert "| `app.py` | 修改 | +1 −1 |" in latest
    assert "| `old.txt` | 删除 | +0 −1 |" in latest
    assert "共 3 个文件，+2 −2" in latest
    assert "notes.md" not in latest

    older = command(agent, "/diff 2")
    assert "write notes" in older and "+# notes" in older and "app.py" not in older

    assert "没有第 3 轮" in command(agent, "/diff 3")


def test_diff_stat_and_path_filter(tmp_path):
    agent = two_turns(tmp_path)

    stat = command(agent, "/diff --stat")
    assert "| `app.py` |" in stat and "```diff" not in stat

    only_pkg = command(agent, "/diff 1 pkg")
    assert "pkg/new.py" in only_pkg and "app.py" not in only_pkg
    assert "这些路径在这一轮没有改动" in command(agent, "/diff 1 docs/")
    assert "Usage" in command(agent, "/diff --bogus")
    assert parse_diff_args("2 --stat src") == (2, True, ["src"])


def test_diff_labels_an_undone_turn_and_says_when_nothing_changed(tmp_path):
    agent = build_agent(tmp_path, ["<final>just talking</final>"])
    agent.ask("hello")
    assert "还没有哪一轮改过文件" in command(agent, "/diff")

    repo = tmp_path / "repo"
    repo.mkdir()
    agent = two_turns(repo)
    command(agent, "/undo")
    after_undo = command(agent, "/diff")
    assert "已经用 `/undo` 撤销" in after_undo and "+    return a + b" in after_undo


def test_diff_masks_secrets_written_to_a_dotenv_file(tmp_path):
    (tmp_path / ".gitignore").write_text(".env\n")
    agent = build_agent(
        tmp_path,
        [tool("write_file", path=".env", content=f"DEBUG=1\nGITHUB_TOKEN={GITHUB_TOKEN}\n"), "<final>ok</final>"],
    )
    agent.ask("add the token")
    assert GITHUB_TOKEN in (tmp_path / ".env").read_text()

    output = command(agent, "/diff")

    assert ".env" in output and "+DEBUG=1" in output
    assert GITHUB_TOKEN not in output and "<secret:" in output


def test_binary_mode_only_and_long_diffs_are_summarised():
    assert "(binary file changed)" in unified("logo.png", b"\x89PNG\0old", b"\x89PNG\0new")
    assert "(only file mode changed)" in unified("run.sh", b"echo\n", b"echo\n")
    big = "".join(f"line {n}\n" for n in range(500))
    text = render_changes([("A", "big.txt", None, big.encode()), ("A", "z.txt", None, b"z\n")], max_file_lines=50, max_total_lines=50)
    assert "big.txt 的 diff 还有" in text
    assert "另有 1 个文件的 diff 未显示：z.txt" in text


def test_masking_runs_on_whole_files_before_the_diff_is_cut():
    from bunnybyte.core.secret_vault import SecretVault

    vault = SecretVault()
    config_key = "Zq9fW3kLm8Rt2Vb7Xy4N"
    pem = "-----BEGIN RSA PRIVATE KEY-----\n" + "".join(f"MIIEow{n}AbCdEfGh\n" for n in range(6)) + "-----END RSA PRIVATE KEY-----\n"
    old = ("x = 1\n" * 3 + pem).encode()
    new = ("x = 2\n" + "x = 1\n" * 2 + pem.replace("MIIEow3", "MIIEowZ") + f'api_key = "{config_key}"\n').encode()
    for cap in (400, 6):
        text = render_changes([("M", "id.toml", old, new)], mask=vault.mask, max_file_lines=cap)
        assert config_key not in text and "MIIEow" not in text
    assert "(file too large to diff" in unified("lock.json", None, ("a\n" * 30_000).encode())
    assert "该行还有" in unified("app.min.js", None, ("x" * 5000).encode())


def test_total_cap_is_a_hard_limit():
    files = [("A", f"f{n}.txt", None, "".join(f"{n}-{i}\n" for i in range(150)).encode()) for n in range(5)]
    text = render_changes(files, max_file_lines=200, max_total_lines=400)
    assert len(text.splitlines()) <= 402


def test_diff_dot_means_the_whole_repo(tmp_path):
    agent = two_turns(tmp_path)
    assert "pkg/new.py" in command(agent, "/diff 1 .") and "app.py" in command(agent, "/diff 1 ./")


def test_report_records_the_turn_trees_and_which_session_produced_it(tmp_path):
    agent = two_turns(tmp_path)
    report = list_runs(tmp_path / ".bunnybyte" / "runs")[0]["report"]

    assert report["changes"]["paths"] == ["app.py", "old.txt", "pkg/new.py"]
    assert report["changes"]["before_tree"] != report["changes"]["after_tree"]
    assert report["session_kind"] == "main" and report["depth"] == 0
    assert report["session_id"] == agent.session["id"]

    agent2 = build_agent(tmp_path, ["<final>nothing to do</final>"])
    agent2.ask("talk only")
    assert list_runs(tmp_path / ".bunnybyte" / "runs")[0]["report"]["changes"] == {}


def test_replay_renders_the_timeline_of_a_real_run(tmp_path):
    two_turns(tmp_path)

    code, text = replay(tmp_path)

    assert code == 0
    lines = text.splitlines()
    assert "已完成（final_answer_returned）" in lines[0] and "4 步" in lines[0]
    assert lines[1] == "请求：fix add"
    assert any("◆ 模型 #1" in line and "1 个工具调用" in line for line in lines)
    assert any("✓ 读取 app.py" in line for line in lines)
    assert any("✓ 修改 app.py" in line and "+1 −1" in line and "改动 app.py" in line for line in lines)
    assert any("✓ 写入 pkg/new.py" in line for line in lines)
    assert any("命令 rm old.txt" in line and "改动 old.txt" in line for line in lines)
    assert any("给出回答" in line for line in lines)
    assert "改动的文件：app.py、old.txt、pkg/new.py" in text
    assert text.rstrip().endswith("fixed add")
    assert "checkpoint_created" in text  # 白名单外的事件只汇总成一行
    order = [text.index(marker) for marker in ("读取 app.py", "修改 app.py", "写入 pkg/new.py", "rm old.txt")]
    assert order == sorted(order)


def test_replay_by_index_and_prefix_list_and_diff(tmp_path, capsys):
    two_turns(tmp_path)
    runs = list_runs(tmp_path / ".bunnybyte" / "runs")

    code, older = replay(tmp_path, "2")
    assert code == 0 and "请求：write notes" in older

    # 两次运行常在同一秒：取能区分开的最短开头（固定切 22 位时随机后缀前两位撞上的概率是 1/256）
    prefix = next(runs[1]["id"][:n] for n in range(22, 27) if not runs[0]["id"].startswith(runs[1]["id"][:n]))
    code, by_prefix = replay(tmp_path, prefix)
    assert "请求：write notes" in by_prefix

    code, listing = replay(tmp_path, "--list")
    assert listing.index("fix add") < listing.index("write notes")

    code, with_diff = replay(tmp_path, "--diff")
    assert "+    return a + b" in with_diff and "--- a/old.txt" in with_diff

    assert replay(tmp_path, "run_19990101")[0] == 2
    assert replay(tmp_path, "9")[0] == 2
    capsys.readouterr()
    assert replay(tmp_path, runs[0]["id"][4:12])[0] == 2  # 只写日期是 run id 开头，不是序号
    assert "匹配到 2 次运行" in capsys.readouterr().err


def test_replay_hides_internal_runs_and_copes_with_killed_or_old_runs(tmp_path):
    two_turns(tmp_path)
    runs_dir = tmp_path / ".bunnybyte" / "runs"
    dream = runs_dir / "run_29991231-235959-aaaaaa"
    dream.mkdir()
    (dream / "task_state.json").write_text(json.dumps({"status": "completed", "user_request": "# Dream: Memory Consolidation\n..."}))
    killed = runs_dir / "run_29991231-235958-bbbbbb"
    killed.mkdir()
    (killed / "task_state.json").write_text(json.dumps({"status": "running", "user_request": "long task", "changed_paths": ["a.py"]}))
    (killed / "trace.jsonl").write_text(
        json.dumps({"event": "run_started", "created_at": "2026-10-31T00:00:00+00:00", "user_request": "long task"})
        + '\n{"event": "tool_exec'
    )

    code, text = replay(tmp_path)
    assert "请求：long task" in text and "未正常结束" in text

    _, listing = replay(tmp_path, "--list")
    assert "Dream" not in listing and "未结束" in listing
    _, everything = replay(tmp_path, "--list", "--all")
    assert "[内部] # Dream" in everything

    _, old_diff = replay(tmp_path, "run_29991231-235958-bbbbbb", "--diff")
    assert "没有记录快照 tree" in old_diff and "a.py" in old_diff


def test_replay_is_reachable_from_the_cli_and_reports_an_empty_workspace(tmp_path, capsys):
    assert main(["replay", "--cwd", str(tmp_path)]) == 2
    assert "还没有运行记录" in capsys.readouterr().err


def test_replay_hides_worker_runs_by_session_kind_and_old_worker_prompts(tmp_path):
    runs_dir = tmp_path / ".bunnybyte" / "runs"
    for name, report in (
        ("run_20261031-000001-aaaaaa", {"session_kind": "worker", "depth": 1, "task_state": {"user_request": "fix"}}),
        ("run_20261031-000002-bbbbbb", {"task_state": {"user_request": "你是 BunnyByte 的 worker 子代理。你的任务：改 a.py"}}),
        ("run_20261031-000003-cccccc", {"session_kind": "main", "depth": 0, "task_state": {"user_request": "real request"}}),
    ):
        (runs_dir / name).mkdir(parents=True)
        (runs_dir / name / "report.json").write_text(json.dumps(report))
    assert [run["id"] for run in list_runs(runs_dir)] == ["run_20261031-000003-cccccc"]
    assert len(list_runs(runs_dir, include_internal=True)) == 3


def test_replay_does_not_climb_out_of_the_project(tmp_path):
    (tmp_path / ".bunnybyte" / "runs" / "run_20261031-000001-aaaaaa").mkdir(parents=True)
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    from bunnybyte.replay import workspace_root

    assert workspace_root(project) == project.resolve()
    plain = tmp_path / "plain"
    plain.mkdir()
    assert workspace_root(plain) == tmp_path.resolve()


def test_replay_diff_masks_known_env_secrets(tmp_path, monkeypatch):
    secret = "plainvalue-without-a-known-format-123"
    monkeypatch.setenv("BB_TEST_SERVICE_TOKEN", secret)
    agent = build_agent(tmp_path, [tool("write_file", path="cfg.txt", content=f"value {secret}\n"), "<final>ok</final>"])
    agent.ask("write cfg")

    _, text = replay(tmp_path, "--diff")

    assert "cfg.txt" in text and secret not in text


def test_hook_lines_say_which_hook_ran():
    from bunnybyte.replay_render import render_timeline

    events = [
        {"event": "run_started", "created_at": "2026-10-31T00:00:00+00:00"},
        {"event": "hook_run", "hook_event": "on_final", "command": "check.sh", "exit_code": 2, "blocked": True, "created_at": "2026-10-31T00:00:01+00:00"},
    ]
    assert "钩子 on_final · check.sh · exit 2 · 拦下" in render_timeline(events)
