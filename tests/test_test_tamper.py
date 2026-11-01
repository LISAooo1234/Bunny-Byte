import io
import json
import shlex
import sys

import pytest

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.config import resolve_project_verify_config
from bunnybyte.core.tamper_guard import CLEAN, SUSPICIOUS, relevant_paths, scan_changes, tamper_notice
from bunnybyte.core.verification import VerifyConfig, resolve_verify_config
from bunnybyte.headless import EXIT_OK, EXIT_UNVERIFIED, prepare_agent, run_headless
from bunnybyte.replay_render import render_timeline
from bunnybyte.testing import ScriptedModelClient

PY = shlex.quote(sys.executable)

TESTS = """import unittest

from calc import add, sub


class T(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

    def test_sub(self):
        self.assertEqual(sub(5, 3), 2)
"""
PYTEST = """from calc import add


def test_add():
    assert add(2, 3) == 5


def test_add_zero():
    assert add(0, 0) == 0
"""


def b(text):
    return None if text is None else text.encode()


def scan(*changes):
    return scan_changes([(status, path, b(old), b(new)) for status, path, old, new in changes])


def kinds(result, severity=None):
    return [item["kind"] for item in result["findings"] if severity is None or item["severity"] == severity]


# ---- 会被标出来的削弱 ----------------------------------------------------------


def test_deleting_a_failing_test_function_is_flagged():
    result = scan(("M", "tests/test_calc.py", TESTS, TESTS.split("    def test_sub")[0]))

    assert result["status"] == SUSPICIOUS
    removed = next(item for item in result["findings"] if item["kind"] == "removed_tests")
    assert removed["names"] == ["test_sub"] and removed["paths"] == ["tests/test_calc.py"]


def test_deleting_the_whole_test_file_is_flagged():
    result = scan(("D", "test_calc.py", PYTEST, None))

    assert "removed_tests" in kinds(result, "warn")


def test_moving_a_test_file_out_of_collection_counts_as_deleting_it():
    result = scan(("D", "tests/test_calc.py", PYTEST, None), ("A", "tests/calc_checks.py", None, PYTEST))

    assert "removed_tests" in kinds(result, "warn")


def test_adding_a_skip_marker_to_an_existing_test_is_flagged():
    skipped = PYTEST.replace("def test_add_zero", "@pytest.mark.skip(reason='flaky')\ndef test_add_zero").replace("from calc", "import pytest\nfrom calc")
    result = scan(("M", "test_calc.py", PYTEST, skipped))

    skip = next(item for item in result["findings"] if item["kind"] == "skip_added")
    assert skip["severity"] == "warn" and skip["names"] == ["test_add_zero"]


def test_unittest_skip_and_skiptest_are_flagged():
    skipped = TESTS.replace("    def test_sub(self):\n", "    @unittest.skip('later')\n    def test_sub(self):\n")
    assert "skip_added" in kinds(scan(("M", "tests/test_calc.py", TESTS, skipped)), "warn")
    inline = TESTS.replace("        self.assertEqual(sub(5, 3), 2)", "        self.skipTest('broken')\n        self.assertEqual(sub(5, 3), 2)")
    assert "skip_added" in kinds(scan(("M", "tests/test_calc.py", TESTS, inline)), "warn")


def test_removing_assertions_is_flagged():
    weaker = PYTEST.replace("    assert add(0, 0) == 0\n", "    add(0, 0)\n")
    result = scan(("M", "test_calc.py", PYTEST, weaker))

    assert kinds(result, "warn") == ["assertions_removed"]


def test_assert_true_and_swallowed_assertion_errors_are_flagged():
    neutral = PYTEST.replace("assert add(0, 0) == 0", "assert True")
    assert "assertions_neutralised" in kinds(scan(("M", "test_calc.py", PYTEST, neutral)), "warn")
    swallowed = PYTEST.replace("    assert add(0, 0) == 0\n", "    try:\n        assert add(0, 0) == 0\n    except Exception:\n        pass\n")
    assert "assertions_neutralised" in kinds(scan(("M", "test_calc.py", PYTEST, swallowed)), "warn")


def test_pytest_config_that_deselects_tests_is_flagged():
    before = "[tool.pytest.ini_options]\naddopts = \"-q\"\n"
    after = "[tool.pytest.ini_options]\naddopts = \"-q --deselect tests/test_calc.py::test_sub\"\n"
    result = scan(("M", "pyproject.toml", before, after))

    narrowed = next(item for item in result["findings"] if item["kind"] == "collection_narrowed")
    assert "--deselect tests/test_calc.py::test_sub" in narrowed["detail"]
    ini = scan(("A", "pytest.ini", None, "[pytest]\naddopts = --ignore=tests/test_calc.py\n"))
    assert "collection_narrowed" in kinds(ini, "warn")


def test_a_new_conftest_that_ignores_files_is_flagged():
    result = scan(("A", "tests/conftest.py", None, "collect_ignore = ['test_calc.py']\n"))

    assert "collection_narrowed" in kinds(result, "warn")


# ---- 不该被标出来的正当改动 ----------------------------------------------------


def test_renaming_a_test_is_not_a_deletion():
    renamed = PYTEST.replace("def test_add_zero", "def test_add_handles_zero")
    assert scan(("M", "test_calc.py", PYTEST, renamed))["status"] == CLEAN


def test_moving_tests_between_files_is_not_a_deletion():
    first, second = PYTEST.split("\n\ndef test_add_zero")
    result = scan(
        ("M", "tests/test_calc.py", PYTEST, first + "\n"),
        ("A", "tests/test_calc_zero.py", None, "from calc import add\n\n\ndef test_add_zero" + second),
    )
    assert result["status"] == CLEAN


def test_fixing_an_expected_value_is_only_noted():
    fixed = PYTEST.replace("== 5", "== 6")
    result = scan(("M", "test_calc.py", PYTEST, fixed))

    assert result["status"] == CLEAN
    assert kinds(result) == ["expectations_changed"]


def test_rewriting_assert_style_and_moving_asserts_into_a_helper_is_clean():
    rewritten = TESTS.replace("self.assertEqual(add(2, 3), 5)", "assert add(2, 3) == 5")
    assert scan(("M", "tests/test_calc.py", TESTS, rewritten))["status"] == CLEAN
    helper = "from calc import add\n\n\ndef check(a, b, want):\n    assert add(a, b) == want\n\n\ndef test_add():\n    check(2, 3, 5)\n\n\ndef test_add_zero():\n    check(0, 0, 0)\n"
    assert scan(("M", "test_calc.py", PYTEST, helper))["status"] == CLEAN


def test_collapsing_cases_into_parametrize_is_clean():
    param = "import pytest\nfrom calc import add\n\n\n@pytest.mark.parametrize('a,b,want', [(2, 3, 5), (0, 0, 0)])\ndef test_add(a, b, want):\n    assert add(a, b) == want\n"
    result = scan(("M", "test_calc.py", PYTEST, param))

    assert "assertions_removed" not in kinds(result)


def test_new_test_files_with_skips_and_platform_skips_are_not_warnings():
    new = "import sys\nimport pytest\n\n\n@pytest.mark.skip(reason='todo')\ndef test_later():\n    assert False\n"
    assert scan(("A", "tests/test_new.py", None, new))["status"] == CLEAN
    platform = PYTEST.replace("def test_add_zero", "@pytest.mark.skipif(sys.platform == 'win32', reason='posix')\ndef test_add_zero").replace("from calc", "import sys\nimport pytest\nfrom calc")
    result = scan(("M", "test_calc.py", PYTEST, platform))
    assert result["status"] == CLEAN and kinds(result) == ["env_skip_added"]


def test_tests_deleted_together_with_their_source_are_obsolete_not_tampering():
    whole = scan(("D", "calc.py", "def add(a, b):\n    return a + b\n", None), ("D", "tests/test_calc.py", PYTEST, None))
    assert whole["status"] == CLEAN and kinds(whole) == ["obsolete_tests"]
    source_old = "def add(a, b):\n    return a + b\n\n\ndef old_api():\n    return 1\n"
    with_api = PYTEST + "\n\ndef test_old_api():\n    assert old_api() == 1\n"
    one = scan(("M", "calc.py", source_old, "def add(a, b):\n    return a + b\n"), ("M", "test_calc.py", with_api, PYTEST))
    assert one["status"] == CLEAN and kinds(one) == ["obsolete_tests"]


def test_fixture_data_and_ordinary_config_edits_are_ignored():
    assert relevant_paths(["tests/data/input.json", "README.md", "calc.py"]) == []
    assert relevant_paths(["tests/test_calc.py", "pyproject.toml", "tests/conftest.py"]) == ["tests/test_calc.py", "pyproject.toml", "tests/conftest.py"]
    ruff = scan(("M", "pyproject.toml", "[tool.ruff]\nline-length = 100\n", "[tool.ruff]\nline-length = 120\nextend-exclude = ['--ignore']\n"))
    assert ruff == {"status": CLEAN, "findings": []}
    first = scan(("A", "pytest.ini", None, "[pytest]\ntestpaths = tests\n"))
    assert first["status"] == CLEAN


def test_a_brand_new_test_file_is_clean():
    assert scan(("A", "tests/test_calc.py", None, PYTEST)) == {"status": CLEAN, "findings": []}


def test_notice_text_by_mode():
    result = scan(("D", "test_calc.py", PYTEST, None))
    assert tamper_notice(result, "warn").startswith("⚠ 测试可能被削弱：删除了测试")
    assert tamper_notice(result, "block").endswith("不算已验证")
    assert tamper_notice({"status": CLEAN, "findings": []}, "warn") == ""


# ---- 接进回合、无人值守和配置 ----------------------------------------------------

CALC = "def add(a, b):\n    return a + b\n"
BUGGY = "def add(a, b):\n    return a - b\n"


def build_agent(tmp_path, outputs, tamper="", verify_mode="auto"):
    (tmp_path / "calc.py").write_text(BUGGY, encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(PYTEST.replace("from calc import add", "import unittest\nfrom calc import add") + "\n\nif __name__ == '__main__':\n    unittest.main()\n", encoding="utf-8")
    verify = VerifyConfig(mode=verify_mode, commands=(f"{PY} -B -m pytest -q -p no:cacheprovider",), timeout=60, fix_proof="off", tamper=tamper)
    agent = BunnyByte(
        model_client=ScriptedModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        verify_config=verify,
        approval_policy="auto",
        auto_dream=False,
    )
    return agent


DELETE_TEST = '<tool name="run_shell" command="rm test_calc.py"></tool>'
FIX_SOURCE = f'<tool name="read_file" path="calc.py"></tool><tool name="write_file" path="calc.py"><content>{CALC}</content></tool>'


def notices(events):
    return [event["content"] for event in events if event["type"] == "runtime_notice"]


def test_a_turn_that_deletes_the_failing_test_by_shell_is_flagged_everywhere(tmp_path):
    pytest.importorskip("pytest")
    agent = build_agent(tmp_path, [DELETE_TEST, "<final>测试全部通过。</final>"])

    events = list(agent.engine.run_turn("让测试通过"))

    tamper = agent.current_task_state.verification["tamper"]
    assert tamper["status"] == SUSPICIOUS and tamper["mode"] == "warn"
    assert any("⚠ 测试可能被削弱：删除了测试" in text for text in notices(events))
    report = json.loads((agent.current_run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["task_state"]["verification"]["tamper"]["status"] == SUSPICIOUS
    trace = [json.loads(line) for line in agent.run_store.trace_path(agent.current_task_state).read_text().splitlines()]
    assert "⚠ 测试篡改守卫 删除了测试" in render_timeline(trace)


def test_the_guard_runs_with_the_verification_gate_off(tmp_path):
    agent = build_agent(tmp_path, [DELETE_TEST, "<final>好了。</final>"], verify_mode="off")

    events = list(agent.engine.run_turn("让测试通过"))

    assert agent.current_task_state.verification["tamper"]["status"] == SUSPICIOUS
    assert any("测试可能被削弱" in text for text in notices(events))


def test_off_mode_and_source_only_turns_are_silent(tmp_path):
    agent = build_agent(tmp_path, [DELETE_TEST, "<final>好了。</final>"], tamper="off")
    events = list(agent.engine.run_turn("让测试通过"))
    assert "tamper" not in agent.current_task_state.verification
    assert not any("测试可能被削弱" in text for text in notices(events))



def test_turns_that_touch_no_test_or_config_file_are_not_scanned(tmp_path, monkeypatch):
    monkeypatch.setattr("bunnybyte.core.tamper_guard.current_turn_entry", lambda agent: pytest.fail("快照不该被读"))
    agent = build_agent(tmp_path, [FIX_SOURCE, "<final>修好了。</final>"], verify_mode="off")

    list(agent.engine.run_turn("修 add"))

    assert "tamper" not in agent.current_task_state.verification


def run_json(agent, **kwargs):
    prepare_agent(agent)
    out, err = io.StringIO(), io.StringIO()
    code = run_headless(agent, "让测试通过", output_format="json", stdout=out, stderr=err, **kwargs)
    return code, json.loads(out.getvalue()), err.getvalue()


def test_require_verified_exits_4_when_the_pass_came_from_deleting_the_test(tmp_path):
    pytest.importorskip("pytest")
    agent = build_agent(tmp_path, [DELETE_TEST, "<final>测试全部通过。</final>"])
    (tmp_path / "test_other.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    code, result, err = run_json(agent, require_verified=True)

    assert result["verification"]["status"] == "passed"  # 删掉失败的测试后，剩下的确实通过了
    assert code == EXIT_UNVERIFIED and result["verification"]["verdict"] == "tampered"
    assert result["verification"]["tamper"]["findings"][0]["kind"] == "removed_tests"
    assert "测试可能被削弱" in err and "test_add_zero" in err and "退出码 4" in err


def test_the_same_run_without_tampering_is_verified(tmp_path):
    pytest.importorskip("pytest")
    agent = build_agent(tmp_path, [FIX_SOURCE, "<final>修好了。</final>"])

    code, result, _ = run_json(agent, require_verified=True)

    assert code == EXIT_OK and result["verification"]["verdict"] == "verified"


def test_warn_mode_reports_but_keeps_exit_code_0(tmp_path):
    pytest.importorskip("pytest")
    agent = build_agent(tmp_path, [DELETE_TEST, "<final>好了。</final>"], tamper="warn")
    (tmp_path / "test_other.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    code, result, _ = run_json(agent, require_verified=True)

    assert code == EXIT_OK and result["verification"]["verdict"] == "verified"
    assert result["verification"]["tamper"]["status"] == SUSPICIOUS


def test_config_values_and_repo_config_can_only_make_it_stricter(tmp_path, monkeypatch):
    assert resolve_verify_config({}).tamper == ""
    assert resolve_verify_config({"verify": {"tamper": "block"}}).tamper == "block"
    with pytest.raises(ValueError):
        resolve_verify_config({"verify": {"tamper": "maybe"}})

    home = tmp_path / "home.toml"
    monkeypatch.setattr("bunnybyte.config.DEFAULT_CONFIG_PATH", home)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".bunnybyte.toml").write_text('[verify]\ntamper = "off"\n', encoding="utf-8")
    assert resolve_project_verify_config(start=repo).tamper == ""  # 仓库关不掉
    (repo / ".bunnybyte.toml").write_text('[verify]\ntamper = "block"\n', encoding="utf-8")
    assert resolve_project_verify_config(start=repo).tamper == "block"  # 调严可以
    home.write_text('[verify]\ntamper = "off"\n', encoding="utf-8")
    (repo / ".bunnybyte.toml").write_text("[verify]\n", encoding="utf-8")
    assert resolve_project_verify_config(start=repo).tamper == "off"  # 用户自己关掉的算数


def test_files_changed_outside_the_main_agents_snapshots_are_seen(tmp_path):
    """子 agent 的写入不在主 agent 的工具快照里：守卫重新拍一次快照，不会因为 after_tree 过时而说“干净”。"""
    from types import SimpleNamespace

    from bunnybyte.core.tamper_guard import scan_turn

    touch = '<tool name="read_file" path="calc.py"></tool><tool name="write_file" path="calc.py"><content>' + BUGGY + "# noop\n</content></tool>"
    agent = build_agent(tmp_path, [touch, "<final>好了。</final>"], verify_mode="off")
    list(agent.engine.run_turn("看看"))
    entry = agent.session["undo_log"][-1]
    (tmp_path / "test_calc.py").unlink()  # 像子 agent 一样在主 agent 的快照之外删掉测试
    agent.current_turn_id = entry["turn_id"]

    result = scan_turn(agent, SimpleNamespace(changed_paths=["calc.py", "test_calc.py"]))

    assert result["status"] == SUSPICIOUS and result["findings"][0]["kind"] == "removed_tests"


# ---- 第二道评审找出来的情况 ----------------------------------------------------


def test_adding_an_unrelated_test_does_not_cancel_a_deleted_one():
    swapped = PYTEST.split("\n\ndef test_add_zero")[0] + "\n\n\ndef test_feature():\n    assert add(1, 1) == 2\n"
    result = scan(("M", "test_calc.py", PYTEST, swapped))

    removed = next(item for item in result["findings"] if item["kind"] == "removed_tests")
    assert removed["names"] == ["test_add_zero"]
    elsewhere = scan(("M", "test_calc.py", PYTEST, PYTEST.split("\n\ndef test_add_zero")[0] + "\n"), ("A", "tests/test_new.py", None, "def test_other():\n    assert 1 + 1 == 2\n"))
    assert "removed_tests" in kinds(elsewhere, "warn")


def test_a_skip_whose_reason_mentions_a_platform_is_still_a_warning():
    skipped = PYTEST.replace("def test_add_zero", "@pytest.mark.skip(reason='broken on darwin')\ndef test_add_zero").replace("from calc", "import pytest\nfrom calc")
    assert "skip_added" in kinds(scan(("M", "test_calc.py", PYTEST, skipped)), "warn")


def test_turning_a_platform_skip_into_an_unconditional_skip_is_flagged():
    base = "import sys\nimport pytest\nfrom calc import add\n\n\n@pytest.mark.skipif(sys.platform == 'win32', reason='posix')\ndef test_add():\n    assert add(2, 3) == 5\n"
    hard = base.replace("@pytest.mark.skipif(sys.platform == 'win32', reason='posix')", "@pytest.mark.skip(reason='later')")
    assert "skip_added" in kinds(scan(("M", "test_calc.py", base, hard)), "warn")


def test_consolidating_assertions_and_reraising_handlers_are_clean():
    two = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n    assert add(0, 0) == 0\n"
    one = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5 and add(0, 0) == 0\n"
    assert scan(("M", "test_calc.py", two, one))["status"] == CLEAN
    reraise = PYTEST.replace("    assert add(0, 0) == 0\n", "    try:\n        assert add(0, 0) == 0\n    except Exception:\n        print('debug')\n        raise\n")
    assert scan(("M", "test_calc.py", PYTEST, reraise))["status"] == CLEAN


def test_a_conftest_hook_that_only_adds_markers_is_not_narrowing():
    hook = "import pytest\n\n\ndef pytest_collection_modifyitems(config, items):\n    for item in items:\n        if 'slow' in item.nodeid:\n            item.add_marker(pytest.mark.slow)\n"
    assert scan(("A", "conftest.py", None, hook))["status"] == CLEAN
    dropping = "def pytest_collection_modifyitems(config, items):\n    items[:] = [item for item in items if 'flaky' not in item.nodeid]\n"
    assert "collection_narrowed" in kinds(scan(("A", "conftest.py", None, dropping)), "warn")


def test_obsolete_needs_an_exact_or_called_name():
    source_old = "def run():\n    return 1\n\n\ndef add(a, b):\n    return a + b\n"
    tests_old = PYTEST + "\n\ndef test_running_totals():\n    assert add(1, 2) == 3\n"
    result = scan(("M", "calc.py", source_old, "def add(a, b):\n    return a + b\n"), ("M", "test_calc.py", tests_old, PYTEST))
    assert "removed_tests" in kinds(result, "warn")


def test_rewriting_a_large_test_file_stays_fast():
    import time

    big = "from calc import add\n\n" + "".join(f"\n\ndef test_case_{i}():\n    assert add({i}, 1) == {i + 1}\n" for i in range(1500))
    rewritten = big.replace("add(", "calc_add(")
    started = time.monotonic()
    scan(("M", "test_calc.py", big, rewritten))
    assert time.monotonic() - started < 5


def test_global_block_beats_a_repo_warn_and_require_verified_keeps_an_explicit_warn(tmp_path, monkeypatch):
    from bunnybyte.core.tamper_guard import require_block

    home = tmp_path / "home.toml"
    home.write_text('[verify]\ntamper = "block"\n', encoding="utf-8")
    monkeypatch.setattr("bunnybyte.config.DEFAULT_CONFIG_PATH", home)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".bunnybyte.toml").write_text('[verify]\ntamper = "warn"\n', encoding="utf-8")
    assert resolve_project_verify_config(start=repo).tamper == "block"
    assert resolve_project_verify_config(start=repo, mode="auto").tamper == "block"  # --verify 不跳过这条规则
    assert require_block(VerifyConfig(tamper="warn")).tamper == "warn"
    assert require_block(VerifyConfig()).tamper == "block"


def test_a_step_limited_run_is_scanned_too(tmp_path):
    agent = build_agent(tmp_path, [DELETE_TEST, DELETE_TEST], verify_mode="off")
    agent.max_steps = 1

    events = list(agent.engine.run_turn("让测试通过"))

    assert agent.current_task_state.verification["tamper"]["status"] == SUSPICIOUS
    assert "测试可能被削弱" in agent.current_task_state.final_answer or any("测试可能被削弱" in text for text in notices(events))


def test_deleting_a_non_comparison_or_unittest_assertion_is_not_credited_as_consolidation():
    two = "from calc import add, ok\n\n\ndef test_add():\n    x = add(2, 3)\n    assert x == 5\n    assert ok(x)\n"
    one = "from calc import add, ok\n\n\ndef test_add():\n    x = add(2, 3)\n    assert x == 5\n"
    assert "assertions_removed" in kinds(scan(("M", "test_calc.py", two, one)), "warn")
    ut_old = "import unittest\nfrom calc import f, g\n\n\nclass T(unittest.TestCase):\n    def test_fg(self):\n        self.assertEqual(f(1), 1)\n        assert g() == 2\n"
    ut_new = "import unittest\nfrom calc import f, g\n\n\nclass T(unittest.TestCase):\n    def test_fg(self):\n        assert g() == 2\n"
    assert "assertions_removed" in kinds(scan(("M", "tests/test_calc.py", ut_old, ut_new)), "warn")
    both = "from calc import a, b\n\n\ndef test_ab():\n    assert a == 1\n    assert b == 2\n"
    either = "from calc import a, b\n\n\ndef test_ab():\n    assert a == 1 or b == 2\n"
    assert "assertions_removed" in kinds(scan(("M", "test_calc.py", both, either)), "warn")


def test_a_rename_whose_body_changed_is_listed_as_a_changed_expectation():
    old = "from calc import add\n\n\ndef test_add_negative():\n    assert add(-1, -2) == -3\n"
    new = "from calc import add\n\n\ndef test_add_positive():\n    assert add(1, 2) == 3\n"
    result = scan(("M", "test_calc.py", old, new))

    changed = next(item for item in result["findings"] if item["kind"] == "expectations_changed")
    assert changed["names"] == ["test_add_negative → test_add_positive"]


def test_mass_renames_stay_fast():
    import time

    old = "".join(f"\n\ndef test_add_case_{i}():\n    assert add({i}, 1) == {i + 1}\n" for i in range(600))
    new = old.replace("test_add_case_", "test_add_value_").replace("== ", "==  ")
    started = time.monotonic()
    scan(("M", "test_calc.py", "from calc import add\n" + old, "from calc import add\n" + new))
    assert time.monotonic() - started < 10
