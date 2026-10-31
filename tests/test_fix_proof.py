import io
import json
import os
import shlex
import sys

import pytest

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.core.fix_proof import plan_fix_proof, prove_fix
from bunnybyte.core.shadow_snapshots import ShadowStore
from bunnybyte.core.verification import VerifyConfig, resolve_verify_config
from bunnybyte.features.sandbox import SandboxConfig
from bunnybyte.headless import run_headless
from bunnybyte.replay_render import render_timeline
from bunnybyte.testing import ScriptedModelClient

PY = shlex.quote(sys.executable)
BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
OLD_TEST = "import unittest\n\nfrom calc import add\n\n\nclass T(unittest.TestCase):\n    def test_zero(self):\n        self.assertEqual(add(0, 0), 0)\n"
REAL_TEST = OLD_TEST + "\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
THEATER_TEST = OLD_TEST + "\n    def test_add_is_callable(self):\n        self.assertTrue(callable(add))\n"
UNRELATED_TEST = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_math(self):\n        self.assertEqual(1 + 1, 2)\n"


def write(path, content):
    # write_file 要求先读过已存在的文件；新文件读一下也只是得到一个错误结果，不影响后面的写入
    return (
        f'<tool name="read_file" path="{path}"></tool>'
        f'<tool name="write_file" path="{path}"><content>{content}</content></tool>'
    )


def build_agent(tmp_path, outputs, fix_proof="report", verify_mode="auto", **kwargs):
    (tmp_path / "calc.py").write_text(BUGGY, encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(OLD_TEST, encoding="utf-8")
    kwargs.setdefault("approval_policy", "auto")
    verify = VerifyConfig(mode=verify_mode, commands=(f"{PY} -B -m unittest -q",), timeout=60, fix_proof=fix_proof)
    return BunnyByte(
        model_client=ScriptedModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        verify_config=verify,
        **kwargs,
    )


def proof_events(events, kind):
    return [event for event in events if event.get("name") == "fix_proof" and event["type"] == kind]


def notices(events):
    return [event["content"] for event in events if event["type"] == "runtime_notice"]


def test_a_regression_test_that_fails_on_the_old_code_is_proven(tmp_path):
    agent = build_agent(tmp_path, [write("calc.py", FIXED), write("test_calc.py", REAL_TEST), "<final>修好了，补了测试。</final>"])

    events = list(agent.engine.run_turn("修 add"))

    result = agent.current_task_state.verification["fix_proof"]
    assert result["status"] == "proven"
    assert result["tests"] == ["test_calc.py"] and result["reverted"] == ["calc.py"]
    assert result["imported"] == ["calc.py"]
    assert "已证明" in proof_events(events, "tool_result")[0]["content"]
    assert any("✓ 修复已证明" in text for text in notices(events))
    assert (tmp_path / "calc.py").read_text() == FIXED  # 只在临时副本里撤回，工作区不动
    report = json.loads((agent.current_run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["verification"]["fix_proof"]["status"] == "proven"


def test_a_test_that_passes_on_the_old_code_too_is_flagged_but_report_mode_does_not_repair(tmp_path):
    agent = build_agent(tmp_path, [write("calc.py", FIXED), write("test_calc.py", THEATER_TEST), "<final>修好了。</final>"])

    events = list(agent.engine.run_turn("修 add"))

    assert agent.current_task_state.verification["fix_proof"]["status"] == "theater"
    assert proof_events(events, "tool_result")[0]["metadata"]["tool_status"] == "error"
    assert any("⚠ 修复未证明" in text for text in notices(events))
    assert not [item for item in agent.session["history"] if item.get("name") == "fix_proof"]
    assert len(agent.model_client.prompts) == 3


def test_repair_mode_sends_a_theater_result_back_once_and_proves_the_better_test(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            write("calc.py", FIXED),
            write("test_calc.py", THEATER_TEST),
            "<final>修好了。</final>",
            write("test_calc.py", REAL_TEST),
            "<final>换成了能抓住 bug 的测试。</final>",
        ],
        fix_proof="repair",
    )

    events = list(agent.engine.run_turn("修 add"))

    statuses = [event["content"].split("：")[0] for event in proof_events(events, "tool_result")]
    assert len(statuses) == 2 and statuses[1] == "已证明"
    assert "still PASS on the old code" in agent.model_client.prompts[3]
    state = agent.current_task_state.verification
    assert state["fix_proof"]["status"] == "proven" and state["fix_proof_repairs"] == 1
    assert [event["content"] for event in events if event["type"] == "final"] == ["换成了能抓住 bug 的测试。"]
    assert agent.current_task_state.tool_steps == 6  # 3 次读 + 3 次写；修复证明不占工具步数


def test_repair_mode_accepts_an_explanation_without_another_round(tmp_path):
    agent = build_agent(
        tmp_path,
        [write("calc.py", FIXED), write("test_calc.py", THEATER_TEST), "<final>a</final>", "<final>这是重构，行为没变。</final>"],
        fix_proof="repair",
    )

    events = list(agent.engine.run_turn("修 add"))

    assert len(proof_events(events, "tool_result")) == 1  # 没有新改动 = 指纹不变，验证和证明都不再跑
    assert [event["content"] for event in events if event["type"] == "final"] == ["这是重构，行为没变。"]


def test_a_test_for_a_brand_new_function_is_only_weak_and_never_repaired(tmp_path):
    source = FIXED.replace("+", "-") + "\n\ndef mul(a, b):\n    return a * b\n"
    test = OLD_TEST.replace("from calc import add", "from calc import add, mul") + "\n    def test_mul(self):\n        self.assertEqual(mul(2, 3), 6)\n"
    agent = build_agent(tmp_path, [write("calc.py", source), write("test_calc.py", test), "<final>加了 mul。</final>"], fix_proof="repair")

    events = list(agent.engine.run_turn("加一个 mul"))

    assert agent.current_task_state.verification["fix_proof"]["status"] == "weak"
    assert len(agent.model_client.prompts) == 3
    assert any("修复证明较弱" in text for text in notices(events))


def test_a_changed_test_that_never_imports_the_changed_source_is_called_out(tmp_path):
    agent = build_agent(tmp_path, [write("calc.py", FIXED), write("test_other.py", UNRELATED_TEST), "<final>好了</final>"])

    events = list(agent.engine.run_turn("修 add"))

    result = agent.current_task_state.verification["fix_proof"]
    assert result["status"] == "theater" and result["imported"] == []
    assert "根本没有导入改过的源码" in proof_events(events, "tool_result")[0]["content"]


@pytest.mark.parametrize(
    ("outputs", "reason"),
    [
        ([write("calc.py", FIXED), "<final>a</final>"], "no_tests"),
        ([write("test_calc.py", REAL_TEST.replace("(2, 3), 5", "(2, 3), -1")), "<final>a</final>"], "no_sources"),
        ([write("calc.py", FIXED), write("test_calc.py", "# 只加了一行注释\n" + OLD_TEST), "<final>a</final>"], "tests_unchanged"),
    ],
)
def test_ordinary_turns_are_skipped_silently(tmp_path, outputs, reason):
    agent = build_agent(tmp_path, outputs)

    events = list(agent.engine.run_turn("改"))

    assert agent.current_task_state.verification["fix_proof"]["reason"] == reason
    assert not proof_events(events, "tool_call")
    assert not any("修复" in text for text in notices(events))


def test_sandboxed_runs_are_not_copied_out_of_the_sandbox(tmp_path):
    agent = build_agent(
        tmp_path,
        [write("calc.py", FIXED), write("test_calc.py", REAL_TEST), "<final>a</final>"],
        sandbox_config=SandboxConfig(mode="best_effort", backend="bubblewrap"),
    )
    agent.sandbox_runner.which = lambda _name: None

    events = list(agent.engine.run_turn("改"))

    assert agent.current_task_state.verification["fix_proof"]["reason"] == "sandbox"
    assert not proof_events(events, "tool_call")


def test_off_mode_and_failed_verification_never_run_the_proof(tmp_path):
    agent = build_agent(tmp_path, [write("calc.py", FIXED), write("test_calc.py", REAL_TEST), "<final>a</final>"], fix_proof="off")
    list(agent.engine.run_turn("改"))
    assert "fix_proof" not in agent.current_task_state.verification

    other = tmp_path / "other"
    other.mkdir()
    agent = build_agent(other, [write("test_calc.py", REAL_TEST), "<final>a</final>"])
    events = list(agent.engine.run_turn("改"))
    assert agent.current_task_state.verification["status"] == "failed"
    assert not proof_events(events, "tool_call")


def snapshot_pair(root, before, after):
    for name, text in before.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    store = ShadowStore.for_root(root)
    first = store.snapshot()
    for name, text in after.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return store, first, store.snapshot()


def prove(root, before, after, timeout=60):
    store, first, second = snapshot_pair(root, before, after)
    plan = plan_fix_proof(store, first, second)
    if plan.get("status"):
        return plan
    return prove_fix(store, root, plan, env={"PATH": os.environ.get("PATH", "")}, timeout=timeout)


def test_pytest_projects_name_the_failing_test_and_keep_config_files_at_the_new_version(tmp_path):
    result = prove(
        tmp_path,
        {"calc.py": BUGGY, "test_calc.py": OLD_TEST, "pytest.ini": "[pytest]\n"},
        {"calc.py": FIXED, "test_calc.py": REAL_TEST, "pytest.ini": "[pytest]\nmarkers =\n    slow\naddopts = --strict-markers\n"},
    )

    assert result["status"] == "proven" and result["failed_tests"] == ["test_calc.T::test_add"]
    assert result["reverted"] == ["calc.py"]  # pytest.ini 不撤回，否则 --strict-markers 会让收集失败、冒充“已证明”


def test_a_src_layout_package_is_imported_from_the_scratch_copy(tmp_path):
    test = OLD_TEST.replace("from calc", "from pkg.calc") + "\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
    result = prove(
        tmp_path,
        {"src/pkg/__init__.py": "", "src/pkg/calc.py": BUGGY, "tests/test_calc.py": test.replace("(2, 3), 5", "(0, 0), 0")},
        {"src/pkg/calc.py": FIXED, "tests/test_calc.py": test},
    )

    assert result["status"] == "proven" and result["imported"] == ["src/pkg/calc.py"]


# setuptools 的可编辑安装就是这样一个 meta path finder：它排在 sys.path 前面，副本里的 calc.py 根本不会被导入
EDITABLE_FINDER = """import importlib.util
import sys


class Finder:
    def find_spec(self, name, path=None, target=None):
        if name == "calc":
            return importlib.util.spec_from_file_location("calc", {path!r})
        return None


sys.meta_path.insert(0, Finder())
"""


def test_code_imported_from_outside_the_copy_is_skipped(tmp_path, monkeypatch):
    installed = tmp_path / "installed"
    installed.mkdir()
    (installed / "calc.py").write_text(FIXED, encoding="utf-8")  # 像 pip install -e 那样指向别处的一份
    repo = tmp_path / "repo"
    repo.mkdir()
    store, first, second = snapshot_pair(
        repo,
        {"calc.py": BUGGY, "test_calc.py": OLD_TEST, "pytest.ini": "[pytest]\n"},
        {"calc.py": FIXED, "test_calc.py": REAL_TEST, "conftest.py": EDITABLE_FINDER.format(path=str(installed / "calc.py"))},
    )
    plan = plan_fix_proof(store, first, second)

    result = prove_fix(store, repo, plan, env={"PATH": os.environ.get("PATH", "")}, timeout=60)

    assert result["status"] == "skipped" and result["reason"] == "imported_outside" and "calc" in result["modules"]


def test_a_test_that_fails_on_the_new_code_in_the_copy_is_reported_with_its_output(tmp_path):
    result = prove(
        tmp_path,
        {"calc.py": BUGGY, "test_calc.py": OLD_TEST},
        {"calc.py": FIXED, "test_calc.py": REAL_TEST + "\n    def test_fixture(self):\n        self.assertTrue(open('data.json').read())\n"},
    )

    assert result["reason"] == "control_failed" and "data.json" in result["output_tail"]


def test_a_test_that_hangs_on_the_old_code_counts_as_proven(tmp_path):
    hanging = "def add(a, b):\n    while True:\n        pass\n"
    result = prove(tmp_path, {"calc.py": hanging, "test_calc.py": OLD_TEST.replace("add(0, 0), 0", "1, 1")}, {"calc.py": FIXED, "test_calc.py": REAL_TEST}, timeout=12)

    assert result["status"] == "proven" and result["failed_tests"] == ["(超时)"]


def test_deleted_assertions_are_reported_as_the_only_test_change(tmp_path):
    result = prove(tmp_path, {"calc.py": BUGGY, "test_calc.py": REAL_TEST}, {"calc.py": FIXED + "# tweak\n", "test_calc.py": OLD_TEST})

    assert result["status"] == "theater" and result["assertions_removed_only"] is True


def test_headless_json_replay_and_config(tmp_path):
    agent = build_agent(tmp_path, [write("calc.py", FIXED), write("test_calc.py", REAL_TEST), "<final>好了</final>"])
    out = io.StringIO()

    run_headless(agent, "修 add", output_format="json", stdout=out, stderr=io.StringIO())

    proof = json.loads(out.getvalue())["verification"]["fix_proof"]
    assert proof["status"] == "proven" and proof["tests"] == ["test_calc.py"] and proof["repairs"] == 0
    trace = [json.loads(line) for line in agent.run_store.trace_path(agent.current_task_state).read_text().splitlines()]
    timeline = render_timeline(trace)
    assert "修复证明 test_calc.py" in timeline and "已证明" in timeline

    assert resolve_verify_config({}).fix_proof == "report"
    assert resolve_verify_config({"verify": {"fix_proof": "repair"}}).fix_proof == "repair"
    with pytest.raises(ValueError):
        resolve_verify_config({"verify": {"fix_proof": "always"}})


def test_approval_prompt_mentions_the_fix_proof_only_when_tests_changed(tmp_path):
    from bunnybyte.tui.widgets import format_tool_args

    agent = build_agent(tmp_path, [write("calc.py", FIXED), write("test_calc.py", REAL_TEST), "<final>a</final>"], verify_mode="ask", approval_policy="ask")
    asked = []
    agent.approve = lambda name, args: asked.append((name, args)) or True

    list(agent.engine.run_turn("改"))

    verify_args = [args for name, args in asked if name == "verify"][0]
    assert "修复证明" in verify_args["note"]
    assert "修复证明" in format_tool_args("verify", verify_args)


def test_tui_card_title_and_summary():
    from bunnybyte.tui.tool_cards import card_args_summary, tool_title

    assert tool_title("fix_proof") == "修复证明"
    assert card_args_summary("fix_proof", {"tests": ["test_calc.py"]}) == "撤回源码重跑 test_calc.py"


def test_bare_pytest_functions_are_run_with_pytest_even_without_pytest_config(tmp_path):
    old = "from calc import add\n\n\ndef test_zero():\n    assert add(0, 0) == 0\n"
    new = old + "\n\ndef test_add():\n    assert add(2, 3) == 5\n"

    result = prove(tmp_path, {"calc.py": BUGGY, "tests/test_calc.py": old}, {"calc.py": FIXED, "tests/test_calc.py": new})

    assert result["status"] == "proven" and "pytest" in result["command"]
    assert result["failed_tests"] == ["tests.test_calc::test_add"]


def test_a_test_using_a_new_method_or_keyword_is_weak_not_proven(tmp_path):
    old_src = "class C:\n    def a(self):\n        return 1\n\n\ndef f(x):\n    return x\n"
    new_src = old_src.replace("        return 1\n", "        return 1\n\n    def b(self):\n        return 2\n") + "\n\ndef g(x, scale=1):\n    return x * scale\n"
    new_src = new_src.replace("def f(x):\n    return x", "def f(x, scale=1):\n    return x * scale")
    old_test = "from calc import C, f\n\n\ndef test_a():\n    assert C().a() == 1\n"
    new_test = old_test + "\n\ndef test_b():\n    assert C().b() == 2\n\n\ndef test_scale():\n    assert f(2, scale=3) == 6\n"

    result = prove(tmp_path, {"calc.py": old_src, "test_calc.py": old_test}, {"calc.py": new_src, "test_calc.py": new_test})

    assert result["status"] == "weak" and len(result["failed_tests"]) == 2


def test_a_pytest_collection_error_on_the_old_code_is_weak(tmp_path):
    test = "from calc import add, mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n"
    result = prove(
        tmp_path,
        {"calc.py": FIXED, "pytest.ini": "[pytest]\n"},
        {"calc.py": FIXED + "\n\ndef mul(a, b):\n    return a * b\n", "test_calc.py": test},
    )

    assert result["status"] == "weak" and result["failed_tests"] == []


def test_only_non_python_changes_besides_tests_are_skipped(tmp_path):
    store, first, second = snapshot_pair(
        tmp_path, {"calc.py": FIXED, "test_calc.py": OLD_TEST}, {"test_calc.py": REAL_TEST, "coverage.xml": "<coverage/>\n"}
    )

    assert plan_fix_proof(store, first, second)["reason"] == "no_python_sources"


def test_a_new_parametrize_row_counts_as_a_test_change(tmp_path):
    old = "import pytest\n\nfrom calc import add\n\n\n@pytest.mark.parametrize(\"a,b,c\", [\n    (0, 0, 0),\n])\ndef test_add(a, b, c):\n    assert add(a, b) == c\n"
    new = old.replace("    (0, 0, 0),\n", "    (0, 0, 0),\n    (2, 3, 5),\n")

    result = prove(tmp_path, {"calc.py": BUGGY, "test_calc.py": old}, {"calc.py": FIXED, "test_calc.py": new})

    assert result["status"] == "proven"


def test_a_projects_own_sitecustomize_cannot_hide_the_import_probe(tmp_path):
    installed = tmp_path / "installed"
    installed.mkdir()
    (installed / "calc.py").write_text(FIXED, encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    store, first, second = snapshot_pair(
        repo,
        {"calc.py": BUGGY, "test_calc.py": OLD_TEST},
        {"calc.py": FIXED, "test_calc.py": REAL_TEST, "sitecustomize.py": EDITABLE_FINDER.format(path=str(installed / "calc.py"))},
    )

    result = prove_fix(store, repo, plan_fix_proof(store, first, second), env={"PATH": os.environ.get("PATH", "")}, timeout=60)

    assert result["reason"] == "imported_outside"  # 项目自己的 sitecustomize 照样执行了（所以导入了外面的 calc）


def test_a_failing_addopts_plugin_option_is_retried_without_addopts(tmp_path):
    ini = "[pytest]\naddopts = -n auto\n"  # 没装 xdist：只跑几个文件时直接用法错误
    result = prove(tmp_path, {"calc.py": BUGGY, "test_calc.py": OLD_TEST, "pytest.ini": ini}, {"calc.py": FIXED, "test_calc.py": REAL_TEST})

    assert result["status"] == "proven" and "addopts=" in result["command"]


def test_symlinks_and_oversized_repos_are_skipped(tmp_path, monkeypatch):
    from bunnybyte.core import fix_proof

    (tmp_path / "data").write_text("x", encoding="utf-8")
    store, first, second = snapshot_pair(tmp_path, {"calc.py": BUGGY, "test_calc.py": OLD_TEST}, {"calc.py": FIXED, "test_calc.py": REAL_TEST})
    monkeypatch.setattr(fix_proof, "MAX_FILES", 2)
    assert plan_fix_proof(store, first, second)["reason"] == "too_large"

    os.symlink("data", tmp_path / "link")
    assert plan_fix_proof(store, second, store.snapshot())["reason"] == "symlink"


def test_a_second_theater_result_is_not_sent_back_again(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            write("calc.py", FIXED),
            write("test_calc.py", THEATER_TEST),
            "<final>a</final>",
            write("test_calc.py", THEATER_TEST + "\n    def test_also_callable(self):\n        self.assertTrue(add)\n"),
            "<final>b</final>",
        ],
        fix_proof="repair",
    )

    events = list(agent.engine.run_turn("修 add"))

    assert len(proof_events(events, "tool_result")) == 2
    assert agent.current_task_state.verification["fix_proof_repairs"] == 1
    assert [event["content"] for event in events if event["type"] == "final"] == ["b"]


def test_output_tail_is_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXPROOF_API_KEY", "fp-secret-value-123456789")
    test = REAL_TEST.replace("self.assertEqual(add(2, 3), 5)", "self.assertEqual(add(2, 3), 5, 'fp-secret-value-123456789')")
    agent = build_agent(tmp_path, [write("calc.py", FIXED), write("test_calc.py", test), "<final>a</final>"])

    list(agent.engine.run_turn("修 add"))

    result = agent.current_task_state.verification["fix_proof"]
    assert result["status"] == "proven" and "fp-secret-value" not in result["output_tail"]


def test_an_unexpected_error_skips_the_proof_instead_of_failing_the_turn(tmp_path, monkeypatch):
    from bunnybyte.core import fix_proof_gate

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(fix_proof_gate, "prove_fix", boom)
    agent = build_agent(tmp_path, [write("calc.py", FIXED), write("test_calc.py", REAL_TEST), "<final>好了</final>"])

    events = list(agent.engine.run_turn("修 add"))

    assert agent.current_task_state.verification["fix_proof"]["reason"] == "error"
    assert [event["content"] for event in events if event["type"] == "final"] == ["好了"]


def test_a_probe_that_records_nothing_skips_instead_of_trusting_the_copy(tmp_path, monkeypatch):
    from bunnybyte.core import fix_proof

    monkeypatch.setattr(fix_proof, "PROBE_SOURCE", "")

    result = prove(tmp_path, {"calc.py": BUGGY, "test_calc.py": OLD_TEST}, {"calc.py": FIXED, "test_calc.py": REAL_TEST})

    assert result["reason"] == "probe_failed"
