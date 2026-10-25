import json

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.core.stall_budget import (
    QUARANTINE,
    STOP,
    STOP_AFTER_QUARANTINE,
    TRIP_TOTAL,
    TRIP_WINDOW,
    TRIP_WINDOW_HITS,
    StallBudget,
)
from bunnybyte.providers.base import ModelToolCall
from bunnybyte.testing import ScriptedModelClient

STUB = {"tool_error_code": "file_range_already_read"}
REPEAT = {"tool_error_code": "repeated_identical_call"}
OK = {"tool_error_code": ""}
EDIT = {"tool_error_code": "", "workspace_changed": True}


def feed(stall, pattern, name="read_file"):
    """'.' = useful result, 'W' = wasted reply, 'E' = edit; returns the last assess() verdict."""
    verdict = ""
    for mark in pattern:
        metadata = {".": OK, "W": STUB, "E": EDIT}[mark]
        stall.observe("patch_file" if mark == "E" else name, metadata)
        verdict = stall.assess()
    return verdict


def test_wasted_replies_are_refunded_up_to_half_the_budget():
    stall = StallBudget(max_steps=10)
    for _ in range(3):
        stall.observe("read_file", STUB)
    stall.observe("search", OK)
    assert stall.charge(4) == 1
    assert stall.last_refund == 3 and stall.wasted_total == 3

    for _ in range(4):
        stall.observe("read_file", REPEAT)
    assert stall.charge(4) == 2  # refund cap of 5 leaves only two more to waive
    assert stall.refunded == 5


def test_waste_is_read_from_result_metadata_not_reply_text():
    stall = StallBudget(max_steps=50)
    stall.observe("read_file", {"tool_error_code": "file_range_already_read"})
    stall.observe("read_file", {"tool_error_code": "not_found"})
    stall.observe("run_shell", {"tool_status": "rejected"})
    assert stall.wasted_total == 1


def test_charge_never_refunds_more_than_was_executed():
    stall = StallBudget(max_steps=50)
    stall.observe("read_file", STUB)
    stall.observe("read_file", STUB)
    assert stall.charge(1) == 0
    assert stall.charge(1) == 1  # the round's waste was consumed


def test_trips_when_most_of_the_recent_window_is_wasted():
    stall = StallBudget(max_steps=50)
    assert feed(stall, "....WWWW") == ""
    assert feed(stall, "W") == QUARANTINE
    assert TRIP_WINDOW_HITS <= TRIP_WINDOW


def test_trips_on_a_slow_trickle_of_waste():
    stall = StallBudget(max_steps=50)
    assert feed(stall, "W..." * (TRIP_TOTAL - 1)) == ""
    assert feed(stall, "W") == QUARANTINE


def test_recorded_flash_run_is_quarantined_then_stopped_only_after_its_edit_landed():
    # recover_from_bad_patch on deepseek-v4-flash: 13 of 25 results wasted, runs of at most 4.
    # The patch landed at result 6; everything after it was the model re-checking finished work.
    stall = StallBudget(max_steps=50)
    verdicts = [feed(stall, mark) for mark in "..WWWE.WW.WWWW....WWW...W"]
    assert verdicts.index(QUARANTINE) > 5
    assert verdicts.index(STOP) > verdicts.index(QUARANTINE)
    assert verdicts.index(STOP) < 24  # saves the tail of the run


def test_probation_ends_when_the_model_stops_looping():
    stall = StallBudget(max_steps=50)
    assert feed(stall, "W" * 5) == QUARANTINE
    assert feed(stall, "W..") == ""
    assert feed(stall, "." * 5) == ""
    assert not stall.quarantined
    assert feed(stall, "WWWW") == ""  # a fresh count, not a stop


def test_an_edit_resets_the_escalation():
    stall = StallBudget(max_steps=50)
    assert feed(stall, "WWWW") == ""
    assert feed(stall, "E") == ""
    assert feed(stall, "WWWW") == ""
    assert stall.wasted_total == 8  # the metric keeps counting across edits


def test_stop_only_after_quarantine_was_ignored():
    stall = StallBudget(max_steps=50)
    assert feed(stall, "W" * 5) == QUARANTINE
    assert feed(stall, "W" * (STOP_AFTER_QUARANTINE - 1)) == ""
    assert feed(stall, "W") == STOP


def test_quarantine_hides_repeated_read_only_tools_for_two_calls_then_restores_them():
    stall = StallBudget(max_steps=50)
    feed(stall, "W" * 5)
    specs = [{"name": "read_file"}, {"name": "patch_file"}, {"name": "run_shell"}, {"name": "search"}]

    offered, notice = stall.next_call(specs)
    assert [spec["name"] for spec in offered] == ["patch_file", "run_shell", "search"]
    assert "read_file" in notice
    stall.call_done()
    offered, _ = stall.next_call(specs)
    assert "read_file" not in [spec["name"] for spec in offered]
    stall.call_done()
    offered, notice = stall.next_call(specs)
    assert offered == specs and notice == ""


def test_a_failed_model_request_does_not_use_up_the_quarantine():
    stall = StallBudget(max_steps=50)
    feed(stall, "W" * 5)
    specs = [{"name": "read_file"}, {"name": "patch_file"}]
    for _ in range(3):  # provider retries ask again without call_done()
        offered, _ = stall.next_call(specs)
        assert [spec["name"] for spec in offered] == ["patch_file"]


def test_repeated_shell_or_edit_calls_are_never_removed_but_the_model_is_still_told():
    stall = StallBudget(max_steps=50)
    for _ in range(5):
        stall.observe("run_shell", REPEAT)
    assert stall.assess() == QUARANTINE
    specs = [{"name": "run_shell"}, {"name": "patch_file"}]
    offered, notice = stall.next_call(specs)
    assert offered == specs and "Do not repeat them" in notice


def build_agent(tmp_path, client, **kwargs):
    (tmp_path / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    return BunnyByte(
        model_client=client,
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        approval_policy="auto",
        **kwargs,
    )


def read_call():
    return "<tool>" + json.dumps({"name": "read_file", "args": {"path": "a.txt"}}) + "</tool>"


def test_repeat_replies_no_longer_eat_the_step_budget(tmp_path):
    client = ScriptedModelClient([read_call()] * 6 + ["<final>done</final>"])
    agent = build_agent(tmp_path, client, max_steps=6)

    assert agent.ask("look at a.txt") == "done"

    state = agent.current_task_state
    assert state.stop_reason == "final_answer_returned"
    assert state.tool_steps == 6 and state.wasted_steps == 5
    report = agent.run_store.report_path(state)
    assert json.loads(report.read_text(encoding="utf-8"))["wasted_steps"] == 5


def test_work_after_a_stub_run_is_not_cut_short(tmp_path):
    patch = "<tool>" + json.dumps(
        {"name": "patch_file", "args": {"path": "a.txt", "old_text": "one", "new_text": "uno"}}
    ) + "</tool>"
    client = ScriptedModelClient([read_call()] * 5 + [patch, "<final>patched</final>"])
    agent = build_agent(tmp_path, client, max_steps=20)

    assert agent.ask("change one to uno") == "patched"

    assert (tmp_path / "a.txt").read_text(encoding="utf-8").startswith("uno")
    assert agent.current_task_state.stop_reason == "final_answer_returned"


def test_stalled_model_is_stopped_early_with_an_honest_summary(tmp_path):
    reads = 1 + 5 + STOP_AFTER_QUARANTINE
    client = ScriptedModelClient([read_call()] * reads + ["<final>Only read a.txt.</final>"])
    agent = build_agent(tmp_path, client)

    answer = agent.ask("change a.txt")

    state = agent.current_task_state
    assert state.stop_reason == "loop_stalled" and state.status == "stopped"
    assert answer.startswith("Only read a.txt.") and "任务可能尚未完成" in answer
    assert state.tool_steps == reads and state.wasted_steps == reads - 1
    assert any("will not be offered" in str(prompt) for prompt in client.prompts)


def test_quarantine_removes_the_repeated_tool_from_native_tool_specs(tmp_path):
    call = ModelToolCall(name="read_file", args={"path": "a.txt"})
    outputs = [call] * (1 + 5 + 1) + ["Patched nothing; read only."]
    client = ScriptedModelClient(outputs, supports_native_tools=True, native_tool_protocol="anthropic")
    agent = build_agent(tmp_path, client)
    agent.ask("change a.txt")

    offered = [{spec["name"] for spec in batch} for batch in client.tool_batches if batch]
    assert "read_file" in offered[0] and "read_file" in offered[5]
    assert "read_file" not in offered[6] and "patch_file" in offered[6]


def test_wasted_steps_survive_task_state_round_trip():
    from bunnybyte.core.task_state import TaskState

    state = TaskState.create(task_id="t", user_request="x")
    state.wasted_steps = 4
    assert TaskState.from_dict(state.to_dict()).wasted_steps == 4


def test_missing_summary_still_ends_the_run_and_says_it_may_be_unfinished(tmp_path):
    reads = 1 + 5 + STOP_AFTER_QUARANTINE
    client = ScriptedModelClient([read_call()] * reads)  # no output left for the summary call
    agent = build_agent(tmp_path, client)

    answer = agent.ask("change a.txt")

    assert agent.current_task_state.stop_reason == "loop_stalled"
    assert "may be unfinished" in answer


def test_a_batch_of_repeated_calls_is_refunded_in_one_round(tmp_path):
    call = ModelToolCall(name="read_file", args={"path": "a.txt"})
    client = ScriptedModelClient(
        [[call, call, call, call], "all read"], supports_native_tools=True, native_tool_protocol="anthropic"
    )
    agent = build_agent(tmp_path, client, max_steps=5)

    assert agent.ask("read a.txt") == "all read"

    state = agent.current_task_state
    assert state.tool_steps == 4 and state.wasted_steps == 3
    assert state.stop_reason == "final_answer_returned"


def test_stalled_explore_worker_is_reported_as_stopped_not_completed(tmp_path):
    reads = 1 + 5 + STOP_AFTER_QUARANTINE
    spawn = (
        '<tool>{"name":"agent","args":{"description":"Inspect","prompt":"Read a.txt",'
        '"subagent_type":"Explore"}}</tool>'
    )
    outputs = [spawn] + [read_call()] * reads + ["<final>Only read it.</final>", "<final>noted</final>"]
    agent = build_agent(tmp_path, ScriptedModelClient(outputs), max_steps=30)

    assert agent.ask("explore a.txt") == "noted"

    notifications = [
        item["content"]
        for item in agent.session["history"]
        if item["role"] == "user" and "<task-notification>" in item["content"]
    ]
    assert len(notifications) == 1 and "<status>stopped</status>" in notifications[0]
