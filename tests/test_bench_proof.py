import json
import subprocess
import sys

import pytest

from bunnybyte.bench import report
from bunnybyte.bench.catalog_proof import CART_REAL_TEST, POSTS_REAL_TEST
from bunnybyte.bench.cli import run_bench_command
from bunnybyte.bench.runner import BENCH_VERIFY, python_command, run_task, snapshot, verify, write_tree
from bunnybyte.bench.tasks import all_tasks, proof_tasks, scripted_outputs, select_tasks, task_set_hash
from bunnybyte.testing import ScriptedModelClient

PROOF = proof_tasks()
PROOF_IDS = [task.id for task in PROOF]


def call(name, **args):
    return "<tool>" + json.dumps({"name": name, "args": args}) + "</tool>"


def final(text="修好了，补了回归测试，测试通过。"):
    return f"<final>{text}</final>"


CART_FIX = call(
    "patch_file",
    path="cart.py",
    old_text="    def clear(self):\n        self.items = []\n",
    new_text="    def clear(self):\n        self.items = []\n        self._total = None\n",
)
# 最顺手的回归测试：清空一个从没算过总价的购物车。修没修都能过，抓不住缓存没清的 bug
CART_THEATER_TEST = (
    "import unittest\n\nfrom cart import Cart\n\n\nclass ClearTest(unittest.TestCase):\n"
    "    def test_clear(self):\n        cart = Cart()\n        cart.add(\"tea\", 3.5)\n        cart.clear()\n"
    "        self.assertEqual(cart.total(), 0)\n"
)
CART_THEATER = [
    call("read_file", path="cart.py"),
    CART_FIX,
    call("write_file", path="test_cart_clear.py", content=CART_THEATER_TEST),
    final(),
]


def test_proof_suite_is_separate_and_leaves_the_core_baseline_untouched():
    assert len(all_tasks()) == 10
    assert not set(PROOF_IDS) & {task.id for task in all_tasks()}
    assert [task.id for task in select_tasks(suite="proof")] == PROOF_IDS
    assert task_set_hash(PROOF) != task_set_hash(all_tasks())
    assert BENCH_VERIFY.fix_proof == "off"  # 旧的 --verify 结果不跑修复证明，新结果要和它可比


@pytest.mark.parametrize("task", PROOF, ids=PROOF_IDS)
def test_proof_verifier_rejects_the_untouched_fixture(task, tmp_path):
    write_tree(tmp_path, task.files)
    assert verify(task, tmp_path, "", snapshot(tmp_path))


@pytest.mark.parametrize("task", PROOF, ids=PROOF_IDS)
def test_proof_oracle_passes_and_its_test_is_proven(task):
    result = run_task(task, ScriptedModelClient(scripted_outputs(task, python_command())), timeout=60, fix_proof="report")

    assert result["passed"], result["failure"]
    assert result["verify_status"] == "passed"
    assert result["fix_proof"] == "proven"
    assert 0 < result["steps"] <= task.step_budget


@pytest.mark.parametrize("task", PROOF, ids=PROOF_IDS)
def test_each_fixture_has_a_unittest_test_so_the_verify_gate_can_pass(task, tmp_path):
    # 验证闸门在通过之后才跑修复证明；没有能收集到的测试时回执是 skipped，修复证明就没机会跑
    write_tree(tmp_path, task.files)
    done = subprocess.run([sys.executable, "-B", "-m", "unittest", "-q"], cwd=tmp_path, capture_output=True, text=True)
    assert done.returncode == 0 and "Ran 0 tests" not in done.stderr, done.stderr


def test_a_test_that_misses_the_bug_passes_the_gate_but_is_flagged_and_fails_grading():
    task = select_tasks(["cart_total_after_clear"])[0]

    result = run_task(task, ScriptedModelClient(CART_THEATER), timeout=60, fix_proof="report")

    assert not result["passed"] and "抓不住" in result["failure"]
    assert result["verify_status"] == "passed"  # 测试闸门拦不住：仓库里的测试全绿
    assert result["fix_proof"] == "theater" and result["fix_proof_repairs"] == 0
    assert "[修复证明 theater]" in report.render_row(result)


def test_repair_mode_sends_the_weak_test_back_and_the_better_test_passes_grading():
    task = select_tasks(["cart_total_after_clear"])[0]
    outputs = [*CART_THEATER, call("write_file", path="test_cart_clear.py", content=CART_REAL_TEST), final("换成先算总价再清空的测试。")]

    result = run_task(task, ScriptedModelClient(outputs), timeout=60, fix_proof="repair")

    assert result["passed"], result["failure"]
    assert result["fix_proof"] == "proven" and result["fix_proof_repairs"] == 1
    assert "退回1" in report.render_row(result)


def test_without_fix_proof_the_bench_records_nothing():
    task = select_tasks(["cart_total_after_clear"])[0]

    result = run_task(task, ScriptedModelClient(CART_THEATER), timeout=60, verify_gate=True)

    assert result["verify_status"] == "passed" and result["fix_proof"] == ""


def test_a_bare_pytest_regression_test_is_graded_the_way_fix_proof_runs_it():
    # 裸 `def test_` 文件当脚本跑只会定义函数、退出码 0：判分会误以为它抓不住 bug，和修复证明（用 pytest）矛盾
    task = select_tasks(["posts_shared_default_tags"])[0]
    fix = call(
        "patch_file",
        path="posts.py",
        old_text='def new_post(title, tags=[]):\n    """Create a post; tags defaults to an empty list."""\n    return {"title": title.strip(), "tags": tags}',
        new_text='def new_post(title, tags=None):\n    """Create a post; tags defaults to an empty list."""\n    return {"title": title.strip(), "tags": [] if tags is None else tags}',
    )
    outputs = [call("read_file", path="posts.py"), fix, call("write_file", path="tests/test_tags.py", content=POSTS_REAL_TEST), final()]

    result = run_task(task, ScriptedModelClient(outputs), timeout=60, fix_proof="report")

    assert result["passed"], result["failure"]
    assert result["fix_proof"] == "proven"


def test_a_bare_pytest_test_that_misses_the_bug_fails_grading_and_is_theater():
    task = select_tasks(["posts_shared_default_tags"])[0]
    fix = call(
        "patch_file",
        path="posts.py",
        old_text="def new_post(title, tags=[]):",
        new_text="def new_post(title, tags=None):\n    tags = [] if tags is None else tags",
    )
    test = "from posts import new_post, tag_post\n\n\ndef test_tag():\n    assert tag_post(new_post(\"a\"), \"X\")[\"tags\"] == [\"x\"]\n"
    outputs = [call("read_file", path="posts.py"), fix, call("write_file", path="test_tag.py", content=test), final()]

    result = run_task(task, ScriptedModelClient(outputs), timeout=60, fix_proof="report")

    assert not result["passed"] and "抓不住" in result["failure"]
    assert result["fix_proof"] == "theater"


def test_summary_counts_statuses_and_agreement_with_grading():
    rows = [
        {"passed": True, "fix_proof": "proven", "fix_proof_repairs": 0, "wall_seconds": 1},
        {"passed": False, "fix_proof": "theater", "fix_proof_repairs": 0, "wall_seconds": 1},
        {"passed": False, "fix_proof": "proven", "fix_proof_repairs": 1, "wall_seconds": 1},
        {"passed": True, "fix_proof": "skipped:tests_unchanged", "fix_proof_repairs": 0, "wall_seconds": 1},
    ]

    summary = report.summarize(rows)

    assert summary["fix_proof"] == {"proven": 2, "skipped:tests_unchanged": 1, "theater": 1}
    assert summary["fix_proof_repairs"] == 1
    assert summary["fix_proof_agreement"] == {"decided": 3, "agree": 2}
    record = report.build_record(rows and [dict(row, task="t", attempt=1, steps=1) for row in rows], PROOF[:1], provider="p", model="m", set_hash="h", repeat=1, fix_proof="repair")
    text = report.render_record(record)
    assert "修复证明（repair）：已证明 2、skipped:tests_unchanged 1、没测到 1；退回模型 1 次" in text
    assert "给出结论的 3 次里 2 次和隐藏判分一致" in text


def test_fix_proof_mode_is_its_own_comparison_class():
    base = dict(provider="p", model="m", set_hash="h", repeat=1, max_steps=50)
    old_verify = report.build_record([], PROOF, verify=True, **base)
    old_verify.pop("fix_proof")  # 0.12.0 之前的结果文件没有这一项
    report_mode = report.build_record([], PROOF, fix_proof="report", **base)
    repair_mode = report.build_record([], PROOF, fix_proof="repair", **base)
    plain_verify = report.build_record([], PROOF, verify=True, **base)
    for item in (report_mode, repair_mode, plain_verify):
        item["captured_at"] = "2999-01-01T00:00:00Z"

    assert report_mode["verify"] is True
    assert report.previous_comparable(report_mode, [old_verify]) is None
    assert report.previous_comparable(repair_mode, [report_mode]) is None
    assert report.previous_comparable(plain_verify, [old_verify]) is old_verify


def test_cli_fix_proof_flag_runs_the_suite_and_saves_the_mode(tmp_path):
    def oracle(task):
        return ScriptedModelClient(scripted_outputs(task, python_command()))

    out = tmp_path / "results"
    code = run_bench_command(
        ["--suite", "proof", "--tasks", "export_drops_last_page", "--fix-proof", "report", "--out", str(out), "--save"],
        client_factory=oracle,
    )

    assert code == 0
    record = json.loads(next(out.glob("*.json")).read_text(encoding="utf-8"))
    assert record["fix_proof"] == "report" and record["verify"] is True
    assert record["runs"][0]["fix_proof"] == "proven"


def test_cli_stops_before_any_run_when_pytest_is_missing(monkeypatch, capsys):
    monkeypatch.setattr("bunnybyte.bench.cli.pytest_preflight", lambda: "python3 没有安装 pytest")

    code = run_bench_command(["--suite", "proof", "--fix-proof", "repair"], client_factory=lambda _task: pytest.fail("不该开始跑"))

    assert code == 2
    assert "pytest" in capsys.readouterr().err


def grade(task, tmp_path, changes):
    write_tree(tmp_path, task.files)
    before = snapshot(tmp_path)
    write_tree(tmp_path, changes)
    return verify(task, tmp_path, "", before)


CART_FIXED = select_tasks(["cart_total_after_clear"])[0].files["cart.py"].replace(
    "    def clear(self):\n        self.items = []\n", "    def clear(self):\n        self.items = []\n        self._total = None\n"
)


def test_a_real_test_named_like_x_test_py_counts_like_it_does_for_fix_proof(tmp_path):
    task = select_tasks(["cart_total_after_clear"])[0]
    assert grade(task, tmp_path, {"cart.py": CART_FIXED, "cart_test.py": CART_REAL_TEST}) == []


def test_editing_the_existing_test_in_place_counts(tmp_path):
    task = select_tasks(["cart_total_after_clear"])[0]
    edited = task.files["test_cart.py"] + CART_REAL_TEST.split("class RegressionTest(unittest.TestCase):\n", 1)[1]
    assert grade(task, tmp_path, {"cart.py": CART_FIXED, "test_cart.py": edited}) == []


def test_a_test_that_only_fails_on_a_missing_helper_is_weak_not_a_catch(tmp_path):
    # 修复证明把这种情况算“弱”：旧代码上导入不了新函数，说明不了测试测到了行为
    task = select_tasks(["cart_total_after_clear"])[0]
    fixed = CART_FIXED + "\n\ndef empty_cart():\n    return Cart()\n"
    unittest_style = "import unittest\n\nfrom cart import empty_cart\n\n\nclass T(unittest.TestCase):\n    def test_empty(self):\n        self.assertEqual(empty_cart().total(), 0)\n"
    pytest_style = "from cart import empty_cart\n\n\ndef test_empty():\n    assert empty_cart().total() == 0\n"

    for name, test in (("test_helper.py", unittest_style), ("test_helper_bare.py", pytest_style)):
        problems = grade(task, tmp_path / name, {"cart.py": fixed, name: test})
        assert problems and ("缺少接口" in problems[0] or "收集失败" in problems[0]), problems


def test_a_test_file_with_no_tests_is_not_a_regression_test(tmp_path):
    task = select_tasks(["cart_total_after_clear"])[0]
    empty = "import unittest\n\nfrom cart import Cart\n\n\nclass T(unittest.TestCase):\n    pass\n"
    problems = grade(task, tmp_path, {"cart.py": CART_FIXED, "test_nothing.py": empty})
    assert problems and "没有通过" in problems[0]


def test_the_proof_suite_checks_for_pytest_even_without_fix_proof(monkeypatch, capsys):
    monkeypatch.setattr("bunnybyte.bench.cli.pytest_preflight", lambda: "python3 没有安装 pytest")

    assert run_bench_command(["--suite", "proof"], client_factory=lambda _task: pytest.fail("不该开始跑")) == 2
    assert run_bench_command(["--tasks", "fix_failing_test", "--list"]) == 0
