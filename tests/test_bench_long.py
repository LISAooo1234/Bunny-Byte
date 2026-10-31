import os
import subprocess
import sys

import pytest

from bunnybyte.bench.cli import build_bench_parser
from bunnybyte.bench.runner import python_command, run_task, snapshot, verify, write_tree
from bunnybyte.bench.tasks import all_tasks, long_tasks, scripted_outputs, select_tasks, task_set_hash
from bunnybyte.core.context_manager import DEFAULT_SECTION_BUDGETS
from bunnybyte.core.turn_history_support import current_turn_soft_limit
from bunnybyte.testing import ScriptedModelClient

LONG = long_tasks()


def test_long_suite_is_separate_and_selectable():
    assert [task.id for task in select_tasks(suite="long")] == ["long_context_ripple"]
    assert task_set_hash(LONG) != task_set_hash(all_tasks())
    assert len(all_tasks()) == 10
    assert build_bench_parser().parse_args(["--suite", "long"]).suite == "long"


def test_long_verifier_rejects_the_fixture_and_a_fix_of_only_the_named_module(tmp_path):
    task = LONG[0]
    write_tree(tmp_path, task.files)
    before = snapshot(tmp_path)
    assert verify(task, tmp_path, "", before)

    invoices = tmp_path / "invoices.py"
    invoices.write_text(
        invoices.read_text(encoding="utf-8").replace('"Invoice total: %.2f" % amount', 'f"Invoice total: {amount:,.2f}"'),
        encoding="utf-8",
    )
    done = subprocess.run([sys.executable, "-m", "unittest", "-q", "test_money"], cwd=tmp_path, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr  # 被点名的测试过了
    assert verify(task, tmp_path, "", before)  # 其余七个模块没改，隐藏判分仍然判负


def test_long_oracle_passes_crosses_the_soft_limit_and_keeps_reuse(tmp_path):
    task = LONG[0]
    client = ScriptedModelClient(scripted_outputs(task, python_command()))
    result = run_task(task, client, timeout=120)

    assert result["passed"], result["failure"]
    # 八个文件整份读完会越过本轮软上限：套件要真的触发本轮压缩，否则量不到想量的东西
    assert max(len(prompt) for prompt in client.prompts) > current_turn_soft_limit(DEFAULT_SECTION_BUDGETS["history"])
    compressed = [prompt for prompt in client.prompts if "invoices.py lines 1-" in prompt]
    assert compressed, "本轮压缩没有发生"
    shares = [len(os.path.commonprefix([a, b])) / len(b) for a, b in zip(client.prompts, client.prompts[1:])]
    assert sum(shares) / len(shares) > 0.75
    assert result["reused_chars"] / result["prompt_chars"] > 0.75


@pytest.mark.parametrize("module", ["accounts", "exports"])
def test_each_module_ends_with_its_own_describe(module):
    source = LONG[0].files[f"{module}.py"]
    assert source.rstrip().endswith("% amount")
    assert len(source.splitlines()) > 300
