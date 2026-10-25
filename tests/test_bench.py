import io
import json
import re
import time
from pathlib import Path
from unittest import mock

import pytest

from bunnybyte.bench import report
from bunnybyte.bench.cli import run_bench_command
from bunnybyte.bench.runner import (
    isolated_home,
    python_command,
    run_task,
    snapshot,
    trace_usage,
    verify,
    write_tree,
)
from bunnybyte.bench.tasks import all_tasks, scripted_outputs, select_tasks, task_set_hash
from bunnybyte.cli import main
from bunnybyte.testing import ScriptedModelClient

TASKS = all_tasks()
TASK_IDS = [task.id for task in TASKS]


def oracle_client(task):
    return ScriptedModelClient(scripted_outputs(task, python_command()))


def test_task_catalog_is_small_fixed_and_well_formed():
    assert len(TASKS) == 8
    assert len(set(TASK_IDS)) == 8
    for task in TASKS:
        assert task.prompt and task.files and task.solution
        assert task.verifier or task.answer_regex
        assert "final" in task.solution[-1]
        assert all(name in task.files for name in task.frozen)
    assert task_set_hash(TASKS) == task_set_hash(all_tasks())
    assert task_set_hash(TASKS[:-1]) != task_set_hash(TASKS)


def test_select_tasks_rejects_unknown_ids():
    assert [t.id for t in select_tasks(["add_cli_flag"])] == ["add_cli_flag"]
    with pytest.raises(ValueError, match="nope"):
        select_tasks(["nope"])


@pytest.mark.parametrize("task", TASKS, ids=TASK_IDS)
def test_verifier_rejects_the_untouched_fixture(task, tmp_path):
    write_tree(tmp_path, task.files)
    problems = verify(task, tmp_path, "", snapshot(tmp_path))

    assert problems, f"{task.id} would pass without any work"


@pytest.mark.parametrize("task", TASKS, ids=TASK_IDS)
def test_oracle_solution_passes_through_the_real_agent_loop(task):
    result = run_task(task, oracle_client(task), timeout=60)

    assert result["passed"], result["failure"]
    assert result["failure"] == ""
    assert 0 < result["steps"] <= task.step_budget
    assert result["stop_reason"] == "final_answer_returned"
    assert result["model_calls"] >= result["steps"]
    assert result["over_budget"] is False


def test_step_ceiling_is_the_product_default_and_over_budget_is_only_a_flag():
    task = select_tasks(["add_cli_flag"])[0]
    assert task.step_budget < 50

    padded = [f'<tool>{{"name":"list_files","args":{{"path":"d{i}"}}}}</tool>' for i in range(task.step_budget)]
    outputs = padded + scripted_outputs(task, python_command())
    result = run_task(task, ScriptedModelClient(outputs), timeout=60)

    assert result["passed"] and result["over_budget"] is True
    assert result["steps"] > task.step_budget

    capped = run_task(task, ScriptedModelClient(list(outputs)), timeout=60, max_steps=3)
    assert not capped["passed"] and capped["stop_reason"] == "step_limit_reached"


def test_hidden_verifier_files_are_absent_until_verification_and_removed_after(tmp_path):
    task = select_tasks(["fix_failing_test"])[0]
    seen = []

    class WatchingClient(ScriptedModelClient):
        def complete(self, prompt, max_new_tokens, **kwargs):
            seen.append((tmp_path / "verify_stats.py").exists())
            return super().complete(prompt, max_new_tokens, **kwargs)

    result = run_task(
        task,
        WatchingClient(scripted_outputs(task, python_command())),
        keep_dir=tmp_path,
        timeout=60,
    )

    assert result["passed"]
    assert seen and not any(seen)
    assert not (tmp_path / "verify_stats.py").exists()


def test_editing_a_frozen_file_fails_the_task(tmp_path):
    task = select_tasks(["fix_failing_test"])[0]
    write_tree(tmp_path, task.files)
    before = snapshot(tmp_path)
    (tmp_path / "test_stats.py").write_text("import unittest\n", encoding="utf-8")
    (tmp_path / "stats.py").write_text(
        task.files["stats.py"].replace("//", "/"), encoding="utf-8"
    )

    problems = verify(task, tmp_path, "", before)

    assert any("test_stats.py" in problem for problem in problems)


def test_read_only_task_fails_when_the_agent_writes_a_file():
    task = select_tasks(["answer_codebase_question"])[0]
    outputs = [
        '<tool>{"name":"write_file","args":{"path":"notes.txt","content":"hi"}}</tool>',
        "<final>net/retry.py: MAX_RETRIES = 5, BACKOFF_BASE_SECONDS = 0.5</final>",
    ]

    result = run_task(task, ScriptedModelClient(outputs), timeout=60)

    assert not result["passed"]
    assert "只读任务" in result["failure"] and "notes.txt" in result["failure"]


def test_wrong_answer_to_read_only_question_fails():
    task = select_tasks(["answer_codebase_question"])[0]

    result = run_task(task, ScriptedModelClient(["<final>大约 3 次，见文档。</final>"]), timeout=60)

    assert not result["passed"]
    assert "最终回答缺少" in result["failure"]


def test_model_failure_is_a_failed_row_not_a_crash():
    task = select_tasks(["add_cli_flag"])[0]

    result = run_task(task, ScriptedModelClient([RuntimeError("boom")]), timeout=60)

    assert result["passed"] is False
    assert result["failure"]


def test_wall_clock_watchdog_aborts_a_slow_run():
    task = select_tasks(["add_cli_flag"])[0]

    class SlowClient(ScriptedModelClient):
        calls = 0

        def complete(self, prompt, max_new_tokens, **kwargs):
            time.sleep(0.3)
            SlowClient.calls += 1
            return f'<tool>{{"name":"list_files","args":{{"path":"d{SlowClient.calls}"}}}}</tool>'

    started = time.monotonic()
    result = run_task(task, SlowClient([]), timeout=0.5)

    assert time.monotonic() - started < 20
    assert result["timed_out"] is True
    assert result["passed"] is False
    assert "墙钟上限" in result["failure"]


def test_isolated_home_hides_user_level_skills(tmp_path):
    from bunnybyte.features.skills import discover_skills

    home = tmp_path / "home"
    skill_dir = home / ".bunnybyte" / "skills" / "personal-quirk"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: personal-quirk\ndescription: only on this machine\n---\nbody\n", encoding="utf-8"
    )
    project = tmp_path / "project"
    project.mkdir()
    real_home = Path.home()

    with mock.patch.object(Path, "home", return_value=home):
        assert "personal-quirk" in discover_skills(project)
        with isolated_home(tmp_path / "empty"):
            assert "personal-quirk" not in discover_skills(project)
        assert "personal-quirk" in discover_skills(project)

    assert Path.home() == real_home


def test_trace_usage_sums_reported_tokens_and_keeps_missing_as_none(tmp_path):
    trace = tmp_path / "trace.jsonl"
    assert trace_usage(trace)["input_tokens"] is None

    events = [
        {"event": "model_parsed", "completion_metadata": {"input_tokens": 100, "output_tokens": 7}},
        {"event": "tool_executed", "completion_metadata": {"input_tokens": 999}},
        {"event": "model_parsed", "completion_metadata": {"input_tokens": 50, "cached_tokens": 40}},
        {"event": "model_parsed", "completion_metadata": {}},
    ]
    trace.write_text("\n".join(json.dumps(item) for item in events) + "\nnot json\n", encoding="utf-8")

    usage = trace_usage(trace)

    assert usage == {"input_tokens": 150, "output_tokens": 7, "cached_tokens": 40}


def make_rows(passed_flags, steps=4):
    return [
        {
            "task": f"t{index}",
            "attempt": 1,
            "passed": flag,
            "failure": "" if flag else "verifier 未通过：x",
            "steps": steps,
            "over_budget": False,
            "model_calls": steps + 1,
            "input_tokens": 100,
            "output_tokens": 10,
            "cached_tokens": None,
            "wall_seconds": 2.0,
            "stop_reason": "final_answer_returned",
            "timed_out": False,
        }
        for index, flag in enumerate(passed_flags)
    ]


def make_record(flags, captured_at, set_hash="abc", model="m1", max_steps=50):
    tasks = [type("T", (), {"id": f"t{i}"})() for i in range(len(flags))]
    record = report.build_record(
        make_rows(flags), tasks, provider="deepseek", model=model, set_hash=set_hash, repeat=1, max_steps=max_steps
    )
    record["captured_at"] = captured_at
    return record


def test_summary_aggregates_pass_rate_steps_and_tokens():
    summary = report.summarize(make_rows([True, True, False, True]))

    assert summary["passed"] == 3 and summary["runs"] == 4
    assert summary["pass_rate"] == 0.75
    assert summary["avg_steps"] == 4
    assert summary["input_tokens"] == 400 and summary["output_tokens"] == 40
    assert summary["cached_tokens"] is None
    assert summary["total_wall_seconds"] == 8.0


def test_previous_comparable_requires_same_task_set_provider_and_model(tmp_path):
    older = make_record([True, False], "2026-10-01T00:00:00Z")
    other_set = make_record([True, True], "2026-10-02T00:00:00Z", set_hash="zzz")
    other_model = make_record([True, True], "2026-10-03T00:00:00Z", model="m2")
    other_steps = make_record([True, True], "2026-10-03T12:00:00Z", max_steps=12)
    current = make_record([True, True], "2026-10-04T00:00:00Z")
    for item in (older, other_set, other_model, other_steps):
        report.save_record(item, tmp_path)

    history = report.load_history(tmp_path)
    previous = report.previous_comparable(current, history)

    assert previous["captured_at"] == "2026-10-01T00:00:00Z"
    text = report.render_record(current, previous)
    assert "+50.0 个百分点" in text
    assert "100.0%" in text
    assert "噪声" in text
    assert "无可比较" in report.render_record(current, None)


def test_save_record_round_trips_and_history_renders(tmp_path):
    record = make_record([True, False], "2026-10-26T01:02:03Z")
    path = report.save_record(record, tmp_path)

    assert path.name == "20261026-010203-deepseek-m1.json"
    assert json.loads(path.read_text(encoding="utf-8"))["summary"]["passed"] == 1
    history_text = report.render_history(report.load_history(tmp_path))
    assert "2026-10-26" in history_text and "50.0%" in history_text
    assert "还没有" in report.render_history([])


def test_cached_tokens_are_shown_next_to_input_tokens():
    rows = make_rows([True])
    rows[0]["cached_tokens"] = 900
    record = make_record([True], "2026-10-26T00:00:00Z")
    record["summary"] = report.summarize(rows)

    assert "输入 100（另有 900 命中缓存） / 输出 10" in report.render_record(record)
    assert "in 100 / out 10" in report.render_row(rows[0])


def test_unreported_tokens_are_not_rendered_as_zero():
    rows = make_rows([True])
    rows[0]["input_tokens"] = rows[0]["output_tokens"] = None
    record = make_record([True], "2026-10-26T00:00:00Z")
    record["summary"] = report.summarize(rows)

    assert "provider 未上报" in report.render_record(record)


def run_cli(argv, tmp_path, factory=oracle_client):
    stream = io.StringIO()
    code = run_bench_command(["--out", str(tmp_path / "results"), *argv], client_factory=factory, stream=stream)
    return code, stream.getvalue()


def test_cli_list_and_unknown_task(tmp_path, capsys):
    code, text = run_cli(["--list"], tmp_path)
    assert code == 0 and "fix_failing_test" in text and "answer_codebase_question" in text

    code, _ = run_cli(["--tasks", "nope"], tmp_path)
    assert code == 2
    assert "nope" in capsys.readouterr().err


def test_cli_runs_saves_history_and_reports_delta(tmp_path):
    code, first = run_cli(["--tasks", "add_cli_flag,count_rows_to_file", "--save"], tmp_path)
    assert code == 0
    assert "通过率：2/2 = 100.0%" in first
    assert "无可比较" in first
    saved = list((tmp_path / "results").glob("*.json"))
    assert len(saved) == 1
    record = json.loads(saved[0].read_text(encoding="utf-8"))
    assert record["task_ids"] == ["add_cli_flag", "count_rows_to_file"]
    assert record["summary"]["pass_rate"] == 1.0
    assert "/Users" not in saved[0].read_text(encoding="utf-8")

    # 再跑一次：同任务集 + 同 provider/model，应当和上一条比较（同一秒内文件名可能相同，先改旧记录的时间）
    record["captured_at"] = "2026-01-01T00:00:00Z"
    saved[0].write_text(json.dumps(record), encoding="utf-8")
    code, second = run_cli(["--tasks", "add_cli_flag,count_rows_to_file", "--save"], tmp_path)
    assert code == 0 and "通过率 +0.0 个百分点" in second

    code, history = run_cli(["--history"], tmp_path)
    assert code == 0 and "100.0%" in history


def test_cli_json_mode_and_fail_under(tmp_path):
    failing = lambda task: ScriptedModelClient(["<final>没干活</final>"])  # noqa: E731
    code, text = run_cli(["--tasks", "add_cli_flag", "--json", "--no-save", "--fail-under", "0.5"], tmp_path, failing)

    assert code == 1
    data = json.loads(text)
    assert data["summary"]["passed"] == 0
    assert data["runs"][0]["failure"]
    assert not (tmp_path / "results").exists()


def test_main_dispatches_bench_subcommand(capsys):
    assert main(["bench", "--list"]) == 0
    assert "multi_file_rename" in capsys.readouterr().out


def test_answer_patterns_accept_chinese_answers_and_reject_wrong_numbers():
    task = select_tasks(["answer_codebase_question"])[0]

    def passes(answer):
        return all(re.search(p, answer, re.I | re.S) for p in task.answer_regex)

    assert passes("最大重试5次，退避基数0.5秒，定义在 net/retry.py。")
    assert passes("net/retry.py 中 MAX_RETRIES = 5，BACKOFF_BASE_SECONDS = 0.5")
    assert not passes("net/retry.py：重试 3 次，退避 0.5 秒")
    assert not passes("net/retry.py：重试 15 次，退避 2.5 秒")
    assert not passes("重试 5 次，退避 0.5 秒")


def test_failure_text_never_contains_local_paths():
    task = select_tasks(["fix_failing_test"])[0]

    result = run_task(task, ScriptedModelClient(["<final>什么都没做</final>"]), timeout=60)

    assert not result["passed"]
    assert "verifier" in result["failure"]
    for marker in ("/private/var", "/var/folders", "/tmp/", str(Path.home())):
        assert marker not in result["failure"]


def test_hung_model_call_is_abandoned_after_the_abort_grace(monkeypatch):
    from bunnybyte.bench import runner

    monkeypatch.setattr(runner, "ABORT_GRACE_SECONDS", 0.2)
    task = select_tasks(["add_cli_flag"])[0]

    class HungClient(ScriptedModelClient):
        def complete(self, prompt, max_new_tokens, **kwargs):
            time.sleep(3)
            return "<final>late</final>"

    started = time.monotonic()
    result = run_task(task, HungClient([]), timeout=0.3)

    assert time.monotonic() - started < 2.5
    assert result["timed_out"] and not result["passed"]
    assert "未能生效" in result["failure"]


def test_git_sha_is_empty_outside_a_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "PACKAGE_ROOT", tmp_path)

    assert report._git("rev-parse", "--short", "HEAD") == ""


def test_same_second_results_do_not_overwrite_each_other(tmp_path):
    record = make_record([True], "2026-10-26T01:02:03Z")

    first = report.save_record(record, tmp_path)
    second = report.save_record(record, tmp_path)

    assert first != second and first.exists() and second.exists()
    assert len(report.load_history(tmp_path)) == 2


def test_cached_token_wording_follows_the_protocol_semantics():
    rows = make_rows([True])
    rows[0]["cached_tokens"] = 900
    record = make_record([True], "2026-10-26T00:00:00Z")
    record["summary"] = report.summarize(rows)

    assert "另有 900" in report.render_record(record)
    record["protocol"] = "openai"
    assert "其中 900" in report.render_record(record)


def test_partial_runs_are_not_saved_unless_asked(tmp_path):
    code, text = run_cli(["--tasks", "add_cli_flag"], tmp_path)

    assert code == 0 and "未写入结果文件" in text
    assert not (tmp_path / "results").exists()


def test_default_out_dir_only_uses_a_bunny_byte_checkout(tmp_path, monkeypatch):
    from bunnybyte.bench.cli import resolve_out_dir

    (tmp_path / "benchmarks" / "results").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    assert resolve_out_dir(None) != tmp_path / "benchmarks" / "results"

    (tmp_path / "bunnybyte" / "bench").mkdir(parents=True)
    (tmp_path / "bunnybyte" / "bench" / "tasks.py").write_text("", encoding="utf-8")
    assert resolve_out_dir(None) == tmp_path / "benchmarks" / "results"
