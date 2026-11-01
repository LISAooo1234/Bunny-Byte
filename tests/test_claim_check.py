import io
import json
import shlex
import subprocess
import sys

import pytest

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.core.claim_check import MISMATCH, claims_notice, reconcile, shell_test_runs
from bunnybyte.core.claim_parse import changed_claims, mentions_failure
from bunnybyte.core.verification import VerifyConfig
from bunnybyte.headless import prepare_agent, run_headless
from bunnybyte.replay_render import render_timeline
from bunnybyte.testing import ScriptedModelClient

PY = shlex.quote(sys.executable)


@pytest.mark.parametrize(
    ("text", "paths"),
    [
        ("已修改 `calc.py`，修复了减法。", ["calc.py"]),
        ("I updated calc.py and added tests/test_calc.py.", ["calc.py", "tests/test_calc.py"]),
        ("- 修改 `src/app.py`：把默认值改成 3", ["src/app.py"]),
        ("Changes:\n- Updated `limits.py` to use 10", ["limits.py"]),
        ("我读了 utils.py，修改了 calc.py。", ["calc.py"]),
        ("只修改了 a.py，没有动 b.py", ["a.py"]),
        ("修改了 calc.py:12 的判断", ["calc.py"]),
        ("无需修改 config.toml", []),
        ("Left `b.py` untouched.", []),
        ("接下来可以修改 x.py", []),
        ("你可以修改 settings.py 来调整", []),
        ("该函数会创建 `out.json`", []),
        ("this PR updated x.py", []),
        ("`init` 新建 `.bunnybyte.toml`", []),
        ("新增/修改、读/写、A/B 三种情况", []),
        ("```\n已修改 a.py\n```", []),
        ("✓ 已验证：`pytest` 通过", []),
        ("在 tests/ 下新增了一个测试", []),
        ("我检查了 calc.py，发现减号写错了，已修复。", []),
        ("修改了 https://example.com/a.py 里提到的问题", []),
        ("The workspace is on branch `main` with a few modified files (`bunnybyte/cli.py`).", []),
        ("工作区里有未提交的修改：`a.py`", []),
        # 改动动词只管到它后面的第一个路径和并列的路径，隔着从句、介词、括号的不算
        ("Updated calc.py so it matches the behavior in spec.md", ["calc.py"]),
        ("I modified `add()` in calc.py and it now passes test_calc.py", ["calc.py"]),
        ("Changed the default in settings.py from 3 to 5 (see defaults.yaml)", ["settings.py"]),
        ("新增了 tests/test_calc.py 覆盖 docs/api.md 中的例子", ["tests/test_calc.py"]),
        ("我在 main.py 里添加了对 helper.py 的导入", ["main.py"]),
        ("按照 docs/spec.md 的要求修改了 calc.py", ["calc.py"]),
        ("修改了 calc.py 中的 add 函数，并更新了 README.md", ["calc.py", "README.md"]),
        ("已修改 `a.py`、`b.py` 和 `c.py`", ["a.py", "b.py", "c.py"]),
        ("calc.py 和 util.py 已修改", ["calc.py", "util.py"]),
        # 回顾更早的工作、被动句
        ("上次已修改 a.py", []),
        ("Earlier I updated a.py", []),
        ("本次会话一共修改了 a.py", []),
        ("settings.py was modified to remove the default", []),
    ],
)
def test_change_claim_parsing(text, paths):
    assert changed_claims(text) == paths


def test_honest_partial_reports_mention_the_failure():
    assert mentions_failure("其余测试全部通过。test_x 的失败是原有的")
    assert not mentions_failure("所有测试已通过。")
    assert mentions_failure("All new tests pass. One unrelated test (test_net) was already broken before my change.")
    assert mentions_failure("测试全部通过（test_slow 本来就挂…）")
    assert mentions_failure("test_net 需要网络，在沙箱里跑不了")


def shell(command, content):
    return {"role": "tool", "name": "run_shell", "args": {"command": command}, "content": content}


def test_test_runs_tell_pass_fail_timeout_and_ignore_lint():
    items = [
        shell("pytest -q", "exit_code: 1\nstdout:\n1 failed, 3 passed"),
        shell("ruff check .", "exit_code: 1\nstdout:\nE501"),
        shell("pytest -q | tail -3", "exit_code: 0\nstdout:\n2 failed"),
        shell("pytest -q", "error: tool run_shell failed: 命令超时（20 秒），已终止整个进程组，没有输出"),
        shell("pytest -q && echo ok", "exit_code: 0\nstdout:\n4 passed\nok"),
    ]
    assert [run[2] for run in shell_test_runs(items)] == ["failed", "failed", "timeout", "passed"]


def test_a_lint_failure_chained_after_green_tests_is_not_a_test_failure():
    items = [shell("pytest -q && ruff check .", "exit_code: 1\nstdout:\n5 passed in 0.1s\ncalc.py:1:89: E501\nFound 1 error.")]
    assert reconcile("tests pass, one ruff E501 left", items, ["calc.py"])["findings"] == []
    assert [run[2] for run in shell_test_runs([shell("python -X dev -m pytest -q", "exit_code: 1\nstdout:\n1 failed")])] == ["failed"]


def test_cheap_unknown_checks_run_before_the_snapshot():
    snapshot = lambda: pytest.fail("便宜的判断已经排除了这个路径，不该再拍快照")  # noqa: E731
    result = reconcile("已修改 scratch.py。", [], [], quick_unknown=lambda path: True, more_changed=snapshot)
    assert result["findings"] == []


def test_reconcile_flags_unchanged_paths_and_failed_last_run():
    result = reconcile(
        "已修改 `calc.py` 和 `util.py`。测试全部通过。",
        [shell("pytest -q", "exit_code: 1\nstdout:\n1 failed")],
        ["calc.py"],
    )
    assert result["status"] == MISMATCH
    assert [item["kind"] for item in result["findings"]] == ["unchanged_path", "failed_tests"]
    assert result["findings"][0]["paths"] == ["util.py"]
    notice = claims_notice(result)
    assert notice.startswith("⚠ 声明与记录不符：回答说改了 `util.py`") and "退出码 1" in notice


def test_reconcile_stays_quiet_when_the_record_agrees():
    assert reconcile("好的，我看了一下。", [], []) is None
    clean = reconcile("已修改 ./calc.py。", [], ["calc.py"])
    assert clean["status"] == "clean" and clean["findings"] == []
    absolute = reconcile("已修改 /repo/src/calc.py。", [], ["src/calc.py"], root="/repo")
    assert absolute["findings"] == []
    basename = reconcile("已修改 calc.py。", [], ["src/calc.py"])
    assert basename["findings"] == []


def test_a_rerun_that_passed_after_a_timeout_is_fine():
    items = [shell("pytest -q", "error: tool run_shell failed: 命令超时（20 秒），已终止整个进程组"), shell("pytest -q -x", "exit_code: 0\nstdout:\n5 passed")]
    assert reconcile("测试全部通过。", items, ["a.py"])["findings"] == []


def test_snapshot_paths_and_unknown_paths_are_not_mismatches():
    lazy = reconcile("已修改 gen.py。", [], [], more_changed=lambda: ["gen.py"])
    assert lazy["findings"] == []
    unknown = reconcile("已修改 settings.toml 和 build.py。", [], [], unknown=lambda path: path == "build.py")
    assert unknown["findings"][0]["paths"] == ["settings.toml"]
    assert reconcile("已修改 build.py。", [], [], check_changes=False) is None  # 快照不可用 / 子任务在跑：只对账测试声明


def test_editing_after_a_green_run_is_recorded_not_warned():
    items = [
        shell("pytest -q", "exit_code: 0\nstdout:\n3 passed"),
        {"role": "tool", "name": "patch_file", "args": {"path": "calc.py"}, "content": "patched calc.py"},
        {"role": "tool", "name": "patch_file", "args": {"path": "README.md"}, "content": "patched README.md"},
    ]
    result = reconcile("测试全部通过。", items, ["calc.py", "README.md"])
    assert result["status"] == "clean"
    assert result["findings"] == [{"kind": "edited_after_pass", "severity": "info", "command": "pytest -q", "paths": ["calc.py"]}]
    assert claims_notice(result) == ""


def test_a_claim_with_no_test_run_at_all_is_flagged_only_when_required():
    assert reconcile("测试全部通过。", [], ["a.py"], require_run=True)["findings"][0]["kind"] == "no_test_run"
    assert reconcile("测试全部通过。", [], ["a.py"])["findings"] == []
    assert reconcile("测试全部通过。", [], [], gate_passed=True, require_run=True)["findings"] == []
    # bench 实测：模型用 `python3 greet.py --shout` 自己验证后说“Verified with all test cases”，是诚实的
    own_check = [shell("python3 greet.py --shout", "exit_code: 0\nstdout:\nHELLO, WORLD!")]
    assert reconcile("Verified with all test cases, all tests pass.", own_check, ["greet.py"], require_run=True)["findings"] == []


CALC = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def build_agent(tmp_path, outputs, verify=None, git=False):
    (tmp_path / "calc.py").write_text(CALC, encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(TEST, encoding="utf-8")
    (tmp_path / "util.py").write_text("X = 1\n", encoding="utf-8")
    if git:
        (tmp_path / ".gitignore").write_text("secret.cfg\n", encoding="utf-8")
        (tmp_path / "secret.cfg").write_text("a = 1\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return BunnyByte(
        model_client=ScriptedModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        verify_config=verify,
        approval_policy="auto",
        auto_dream=False,
    )


FIX = f'<tool name="read_file" path="calc.py"></tool><tool name="write_file" path="calc.py"><content>{FIXED}</content></tool>'
RUN_TESTS = f'<tool name="run_shell" command="{PY} -B -m pytest -q -p no:cacheprovider"></tool>'


def notices(events):
    return [event["content"] for event in events if event["type"] == "runtime_notice"]


def test_a_turn_that_claims_an_edit_it_never_made_is_flagged_everywhere(tmp_path):
    agent = build_agent(tmp_path, [FIX, "<final>已修改 `calc.py` 和 `util.py`，修好了加法。</final>"])

    events = list(agent.engine.run_turn("修 add"))

    claims = agent.current_task_state.verification["claims"]
    assert claims["status"] == MISMATCH and claims["findings"][0]["paths"] == ["util.py"]
    assert any(text.startswith("⚠ 声明与记录不符：回答说改了 `util.py`") for text in notices(events))
    report = json.loads((agent.current_run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["task_state"]["verification"]["claims"]["status"] == MISMATCH
    trace = [json.loads(line) for line in agent.run_store.trace_path(agent.current_task_state).read_text().splitlines()]
    assert "⚠ 声明对账 回答说改了 `util.py`" in render_timeline(trace)


def test_an_honest_turn_is_silent(tmp_path):
    pytest.importorskip("pytest")
    agent = build_agent(tmp_path, [FIX, RUN_TESTS, "<final>已修改 `calc.py`，测试全部通过。</final>"])

    events = list(agent.engine.run_turn("修 add"))

    assert agent.current_task_state.verification["claims"]["status"] == "clean"
    assert not any("声明与记录不符" in text for text in notices(events))


def test_claiming_a_pass_after_the_last_test_run_failed_is_flagged(tmp_path):
    pytest.importorskip("pytest")
    patch = '<tool name="read_file" path="util.py"></tool><tool name="write_file" path="util.py"><content>X = 2\n</content></tool>'
    agent = build_agent(tmp_path, [patch, RUN_TESTS, "<final>改好了，测试全部通过。</final>"])

    events = list(agent.engine.run_turn("改 X"))

    finding = agent.current_task_state.verification["claims"]["findings"][0]
    assert finding["kind"] == "failed_tests" and finding["exit_code"] == 1
    assert any("最后一次测试命令" in text for text in notices(events))


def test_files_changed_by_shell_or_in_an_earlier_turn_are_not_mismatches(tmp_path):
    by_shell = '<tool>{"name": "run_shell", "args": {"command": "echo X=3 > util.py"}}</tool>'
    agent = build_agent(
        tmp_path,
        [by_shell, "<final>已修改 util.py。</final>", "<final>之前已修改 util.py，现在不用再动。</final>", "<final>已修改 util.py。</final>"],
        git=True,
    )

    list(agent.engine.run_turn("改 X"))
    assert agent.current_task_state.verification["claims"]["findings"] == []
    list(agent.engine.run_turn("还要改吗"))
    list(agent.engine.run_turn("再说一遍"))
    assert agent.current_task_state.verification["claims"]["findings"] == []  # 上一回合改过


def test_gitignored_and_missing_files_are_unknown_not_mismatches(tmp_path):
    agent = build_agent(tmp_path, ["<final>已修改 secret.cfg，并删除了 scratch.py。</final>"], git=True)

    events = list(agent.engine.run_turn("看看配置"))

    assert agent.current_task_state.verification["claims"]["findings"] == []
    assert not any("声明与记录不符" in text for text in notices(events))


def test_the_check_runs_with_the_gate_on_and_does_not_repeat_the_old_warning(tmp_path):
    verify = VerifyConfig(mode="auto", commands=(f"{PY} -B -c 'raise SystemExit(0)'",), timeout=30)
    agent = build_agent(tmp_path, ["<final>已修改 util.py，所有测试已通过。</final>"], verify=verify)

    text = "\n".join(notices(list(agent.engine.run_turn("改 X"))))

    assert "回答声称测试/验证已通过" in text  # 旧警告：没有任何回执
    assert "回答说改了 `util.py`" in text and "最后一次测试命令" not in text


def test_an_unexpected_error_does_not_break_the_turn(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError(63, "File name too long")

    monkeypatch.setattr("bunnybyte.core.claim_gate.reconcile", boom)
    agent = build_agent(tmp_path, [FIX, "<final>已修改 calc.py。</final>"])

    events = list(agent.engine.run_turn("修 add"))

    assert events[-1]["type"] == "turn_finished" and "claims" not in agent.current_task_state.verification
    trace = agent.run_store.trace_path(agent.current_task_state).read_text()
    assert "claim_check_failed" in trace and (agent.current_run_dir / "report.json").exists()


def test_a_very_long_or_outside_path_is_unknown(tmp_path):
    long_name = "a" * 300 + ".py"
    agent = build_agent(tmp_path, [f"<final>已修改 `{long_name}` 和 /etc/hosts.txt。</final>"], git=True)
    list(agent.engine.run_turn("改"))
    assert agent.current_task_state.verification["claims"]["findings"] == []


def test_headless_json_carries_the_claims_block(tmp_path):
    agent = build_agent(tmp_path, [FIX, "<final>已修改 calc.py 和 util.py。</final>"])
    prepare_agent(agent)
    out, err = io.StringIO(), io.StringIO()

    run_headless(agent, "修 add", output_format="json", stdout=out, stderr=err)

    claims = json.loads(out.getvalue())["verification"]["claims"]
    assert claims["status"] == MISMATCH
    assert claims["findings"] == [{"kind": "unchanged_path", "severity": "warn", "paths": ["util.py"]}]
