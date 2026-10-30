import json

import pytest

from bunnybyte.bench.cli import run_bench_command
from bunnybyte.bench.runner import python_command, run_task, snapshot, verify, write_tree
from bunnybyte.bench.tasks import all_tasks, scripted_outputs, select_tasks, spec_tasks, task_set_hash
from bunnybyte.testing import ScriptedModelClient

SPEC = spec_tasks()
SPEC_IDS = [task.id for task in SPEC]


def call(name, **args):
    return "<tool>" + json.dumps({"name": name, "args": args}) + "</tool>"


def final(text="改好了，测试通过。"):
    return f"<final>{text}</final>"


# 只把“有测试守着”的那一半做完：测试全绿，需求里的另一半没做
TESTS_ONLY = {
    "fix_with_regression_test": [
        call("read_file", path="pricing.py"),
        call("patch_file", path="pricing.py", old_text="percent / 10,", new_text="percent / 100,"),
        final(),
    ],
    "fix_and_update_changelog": [
        call("read_file", path="textstats.py"),
        call("patch_file", path="textstats.py", old_text='text.split(" ")', new_text="text.split()"),
        final(),
    ],
    "rename_option_keep_alias": [
        call("read_file", path="report.py"),
        call(
            "patch_file",
            path="report.py",
            old_text='"--colour", action="store_true", help="colour the output green"',
            new_text='"--color", action="store_true", help="color the output green"',
        ),
        call("patch_file", path="report.py", old_text="if args.colour:", new_text="if args.color:"),
        final(),
    ],
}


def test_spec_suite_is_separate_and_leaves_the_core_baseline_untouched():
    assert len(all_tasks()) == 10
    assert not set(SPEC_IDS) & {task.id for task in all_tasks()}
    assert [task.id for task in select_tasks(suite="spec")] == SPEC_IDS
    assert select_tasks(["rename_option_keep_alias"])[0].id == "rename_option_keep_alias"
    assert task_set_hash(SPEC) != task_set_hash(all_tasks())
    with pytest.raises(ValueError, match="套件"):
        select_tasks(suite="nope")


@pytest.mark.parametrize("task", SPEC, ids=SPEC_IDS)
def test_spec_verifier_rejects_the_untouched_fixture(task, tmp_path):
    write_tree(tmp_path, task.files)
    assert verify(task, tmp_path, "", snapshot(tmp_path))


@pytest.mark.parametrize("task", SPEC, ids=SPEC_IDS)
def test_spec_oracle_passes_through_the_real_agent_loop(task):
    result = run_task(task, ScriptedModelClient(scripted_outputs(task, python_command())), timeout=60)

    assert result["passed"], result["failure"]
    assert 0 < result["steps"] <= task.step_budget


@pytest.mark.parametrize("task", SPEC, ids=SPEC_IDS)
def test_doing_only_the_tested_half_passes_the_visible_tests_but_fails_grading(task, tmp_path):
    result = run_task(task, ScriptedModelClient(TESTS_ONLY[task.id]), timeout=60, keep_dir=tmp_path)

    assert not result["passed"]
    assert result["failure"].startswith("verifier 未通过")
    # 陷阱成立的前提：仓库里能看见的测试全是绿的，测试闸门拦不住
    import subprocess
    import sys

    done = subprocess.run([sys.executable, "-B", "-m", "unittest", "-q"], cwd=tmp_path, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_a_test_that_cannot_catch_the_bug_does_not_count_as_a_regression_test():
    task = select_tasks(["fix_with_regression_test"])[0]
    weak = [
        call("read_file", path="pricing.py"),
        call("patch_file", path="pricing.py", old_text="percent / 10,", new_text="percent / 100,"),
        call("read_file", path="test_pricing.py"),
        call(
            "patch_file",
            path="test_pricing.py",
            old_text="        self.assertEqual(apply_discount(50, 0), 50)\n",
            new_text="        self.assertEqual(apply_discount(50, 0), 50)\n        self.assertEqual(apply_discount(10, 0), 10)\n",
        ),
        final(),
    ]
    result = run_task(task, ScriptedModelClient(weak), timeout=60)

    assert not result["passed"]
    assert "抓不住" in result["failure"]


def test_cli_suite_flag_lists_the_spec_tasks(capsys):
    assert run_bench_command(["--suite", "spec", "--list"]) == 0
    out = capsys.readouterr().out
    assert all(task_id in out for task_id in SPEC_IDS)
    assert "fix_failing_test" not in out


def test_a_regression_test_in_a_subdirectory_counts():
    task = select_tasks(["fix_with_regression_test"])[0]
    outputs = [
        call("read_file", path="pricing.py"),
        call("patch_file", path="pricing.py", old_text="percent / 10,", new_text="percent / 100,"),
        call("write_file", path="tests/test_regress.py", content="from pricing import apply_discount\n\nassert apply_discount(80, 25) == 60.0\n"),
        final(),
    ]

    result = run_task(task, ScriptedModelClient(outputs), timeout=60)

    assert result["passed"], result["failure"]
