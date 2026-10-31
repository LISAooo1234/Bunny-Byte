import json
from dataclasses import replace

import pytest

from bunnybyte.bench import report, runner
from bunnybyte.bench.cli import build_bench_parser, run_bench_command
from bunnybyte.bench.runner import GOAL_SUITE_MAX_STEPS, python_command, run_goal_check, run_task, snapshot, verify, write_tree
from bunnybyte.bench.tasks import all_tasks, goal_tasks, scripted_outputs, select_tasks, task_set_hash
from bunnybyte.core.engine_helpers import GOAL_STEP_LIMIT_SUFFIX, STEP_LIMIT_SUFFIX
from bunnybyte.providers.base import ModelResult
from bunnybyte.testing import ScriptedModelClient

GOAL = goal_tasks()
IDS = [task.id for task in GOAL]


def oracle(task):
    return scripted_outputs(task, python_command())


def split_after_steps(outputs, steps):
    """参考解在第 steps 个工具调用之后插一条“进展总结”，模拟一轮步数用尽，剩下的放到下一轮。"""
    return [*outputs[:steps], "<final>第一轮先做到这里。</final>", *outputs[steps:]]


def metered(outputs):
    """每次调用都上报 token：会话计量器才会把它算作一次付费调用。"""
    return [ModelResult(text=text, metadata={"input_tokens": 100, "output_tokens": 5}) for text in outputs]


def test_goal_suite_is_separate_and_core_hash_is_unchanged():
    assert [task.id for task in select_tasks(suite="goal")] == IDS
    assert task_set_hash(all_tasks()) == "6cd4145cd51d"  # goal_check 只在有值时进指纹，旧结果仍然可比
    assert all(not task.goal_check for task in all_tasks())
    assert task_set_hash(GOAL) != task_set_hash(all_tasks())


@pytest.mark.parametrize("task", GOAL, ids=IDS)
def test_fixture_fails_the_check_and_oracle_needs_more_than_one_capped_turn(task, tmp_path):
    write_tree(tmp_path, task.files)
    assert run_goal_check(task, tmp_path) is False  # 检查一开始必须失败，否则目标模式直接拒绝
    assert verify(task, tmp_path, "", snapshot(tmp_path))
    tools = [step for step in task.solution if "final" not in step]
    assert len(tools) > GOAL_SUITE_MAX_STEPS  # 高效完成也要超过 12 步，一轮做不完


@pytest.mark.parametrize("task", GOAL, ids=IDS)
def test_oracle_passes_check_and_hidden_verifier_in_one_uncapped_turn(task):
    result = run_task(task, ScriptedModelClient(oracle(task)), timeout=120)

    assert result["passed"], result["failure"]
    assert result["check_passed"] is True
    assert result["stop_reason"] == "final_answer_returned"
    assert result["goal_status"] == ""


@pytest.mark.parametrize("task", GOAL, ids=IDS)
def test_goal_mode_crosses_rounds_where_one_capped_turn_fails(task):
    outputs = split_after_steps(oracle(task), GOAL_SUITE_MAX_STEPS)

    single = run_task(task, ScriptedModelClient(list(outputs)), timeout=120, max_steps=GOAL_SUITE_MAX_STEPS, verify_gate=True)
    assert not single["passed"] and single["stop_reason"] == "step_limit_reached"
    assert single["check_passed"] is False

    goal = run_task(task, ScriptedModelClient(metered(outputs)), timeout=120, max_steps=GOAL_SUITE_MAX_STEPS, goal_rounds=4)
    assert goal["passed"], goal["failure"]
    assert goal["goal_status"] == "achieved" and goal["goal_rounds"] == 2
    assert goal["round_stops"] == ["step_limit_reached", "final_answer_returned"]
    tools = len([step for step in task.solution if "final" not in step])
    assert goal["steps"] == tools  # 两轮的步数加起来，而不是只看最后一轮
    assert goal["model_calls"] == len(outputs) - 1  # 两轮的模型调用加起来（attempts 不含步数用尽时的总结调用）
    assert goal["paid_calls"] == len(outputs)  # 花费口径的调用数含那次总结
    assert goal["input_tokens"] == 100 * len(outputs)


def test_false_achieved_when_the_visible_check_passes_but_the_hidden_verifier_fails():
    task = select_tasks(["implement_toolkit"])[0]
    # 只满足看得见的测试：clamp 不检查 low > high，隐藏判分会抓到
    outputs = []
    for step in task.solution:
        if step.get("name") == "patch_file" and step["args"]["path"] == "clamp.py":
            step = {"name": "patch_file", "args": dict(step["args"], new_text="    return max(low, min(high, value))")}
        outputs.append(step)
    client = ScriptedModelClient(scripted_outputs(replace(task, solution=tuple(outputs)), python_command()))

    result = run_task(task, client, timeout=120, goal_rounds=3)

    assert result["goal_status"] == "achieved" and result["check_passed"] is True
    assert not result["passed"] and "verifier" in result["failure"]
    summary = report.summarize([result])
    assert summary["false_achieved"] == 1 and summary["goal_achieved"] == 1
    assert "[检查通过但判分未过]" in report.render_row(result)


def test_unmet_goal_reports_reason_rounds_and_cost_bucket():
    task = select_tasks(["fix_six_bugs"])[0]
    reads = [f'<tool>{{"name":"read_file","args":{{"path":"{name}.py"}}}}</tool>' for name in ("average", "window")]
    outputs = [reads[0], "<final>先看了一个文件。</final>", reads[1], "<final>又看了一个。</final>"]

    result = run_task(task, ScriptedModelClient(outputs), timeout=120, max_steps=GOAL_SUITE_MAX_STEPS, goal_rounds=2)

    assert not result["passed"]
    assert result["goal_status"] == "unmet" and result["goal_rounds"] == 2
    assert "没有进展" in result["goal_reason"]  # 第二轮只读不改：没有进展就停，不再接着花钱
    assert result["check_passed"] is False


def test_goal_rounds_need_a_task_with_a_check():
    with pytest.raises(ValueError, match="goal_check"):
        run_task(select_tasks(["fix_failing_test"])[0], ScriptedModelClient([]), goal_rounds=2)


def test_goal_rounds_are_a_separate_comparison_class():
    rows = [{"task": "t", "attempt": 1, "passed": True, "failure": "", "steps": 3, "wall_seconds": 1.0}]
    tasks = [type("T", (), {"id": "t"})()]
    older = report.build_record(rows, tasks, provider="p", model="m", set_hash="h", repeat=1, max_steps=12)
    older["captured_at"] = "2026-01-01T00:00:00Z"
    newer = report.build_record(rows, tasks, provider="p", model="m", set_hash="h", repeat=1, max_steps=12, goal_rounds=4)
    newer["captured_at"] = "2026-01-02T00:00:00Z"

    assert report.previous_comparable(newer, [older]) is None
    assert report.previous_comparable(older | {"captured_at": "2026-01-03T00:00:00Z"}, [older]) is older
    assert "目标模式（最多 4 轮）" in report.render_record(newer)


def test_cli_goal_suite_defaults_to_twelve_steps_and_rejects_tasks_without_a_check(tmp_path, capsys):
    assert build_bench_parser().parse_args(["--suite", "goal"]).max_steps is None
    def factory(task):
        return ScriptedModelClient(oracle(task))

    code = run_bench_command(["--suite", "goal", "--tasks", "fix_six_bugs", "--no-save", "--json", "--out", str(tmp_path)], client_factory=factory)
    record = json.loads(capsys.readouterr().out)
    assert code == 0 and record["max_steps"] == GOAL_SUITE_MAX_STEPS and record["goal_rounds"] == 0

    assert run_bench_command(["--tasks", "fix_failing_test", "--goal-rounds", "2", "--no-save"], client_factory=factory) == 2
    assert "没有检查命令" in capsys.readouterr().err


def test_step_limit_suffix_inside_a_goal_does_not_ask_for_resume():
    task = select_tasks(["fix_six_bugs"])[0]
    outputs = split_after_steps(oracle(task), GOAL_SUITE_MAX_STEPS)
    seen = []

    original = runner.run_totals

    def capture(agent, state):
        seen.extend(item.get("content", "") for item in agent.session["history"] if item.get("role") == "assistant")
        return original(agent, state)

    runner.run_totals = capture
    try:
        run_task(task, ScriptedModelClient(list(outputs)), timeout=120, max_steps=GOAL_SUITE_MAX_STEPS, goal_rounds=4)
    finally:
        runner.run_totals = original
    first = next(text for text in seen if "第一轮先做到这里" in text)
    assert GOAL_STEP_LIMIT_SUFFIX.strip() in first
    assert "/resume" not in first and "/resume" in STEP_LIMIT_SUFFIX


def test_totals_ignore_run_dirs_that_are_not_this_session(tmp_path):
    """子 agent 的运行写在同一个运行目录里：不能算进主会话的步数。"""
    task = select_tasks(["fix_six_bugs"])[0]
    original = runner.run_totals

    def with_stray_run(agent, state):
        stray = agent.run_store.task_state_path("run_00000000-000000-worker")
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text(json.dumps({"run_id": "run_00000000-000000-worker", "tool_steps": 99, "attempts": 99}), encoding="utf-8")
        return original(agent, state)

    runner.run_totals = with_stray_run
    try:
        result = run_task(task, ScriptedModelClient(oracle(task)), timeout=120)
    finally:
        runner.run_totals = original
    assert result["steps"] == len([step for step in task.solution if "final" not in step])
    assert result["round_stops"] == ["final_answer_returned"]


def test_timeout_closes_an_event_stream_that_ignores_the_abort_flag():
    """run_turn 开头会清掉中止标记：落在两轮之间的中止可能被吞掉。超时后必须在下一个事件处关掉生成器。"""
    import threading
    import time

    produced, closed = [], threading.Event()

    def stream():
        try:
            while True:  # 像一个吞掉了中止标记、还在接着开新一轮的目标
                time.sleep(0.02)
                produced.append(1)
                yield {"type": "runtime_notice", "content": "next round"}
        finally:
            closed.set()

    class Agent:
        def abort_current_turn(self):
            pass

    result = {}
    _answer, error, stuck = runner._drive(Agent(), stream, 0.2, result, close_on_stop=True)
    assert result["timed_out"] and error is None and not stuck
    assert closed.wait(2)
    count = len(produced)
    time.sleep(0.2)
    assert len(produced) == count  # 关掉之后不再产生事件（不再花钱）
