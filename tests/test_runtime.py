import json

from bunnybyte.testing import ScriptedModelClient
from bunnybyte import Engine, BunnyByte, SessionEventBus, SessionStore, WorkspaceContext


def build_agent(tmp_path, outputs, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".bunnybyte" / "sessions")
    return BunnyByte(
        model_client=ScriptedModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        **kwargs,
    )


def read_session_events(agent):
    path = agent.session_event_bus.path
    assert path.exists()
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def event_names(agent):
    return [event["event"] for event in read_session_events(agent)]


def test_engine_drives_real_session_and_persists_event_timeline(tmp_path):
    agent = build_agent(tmp_path, ["<final>Done.</final>"])

    assert isinstance(agent.engine, Engine)
    assert isinstance(agent.session_event_bus, SessionEventBus)

    answer = agent.ask("ship runtime")

    assert answer == "Done."
    assert agent.session_path.exists()
    assert (
        agent.session_event_bus.path
        == tmp_path / ".bunnybyte" / "sessions" / f"{agent.session['id']}.events.jsonl"
    )
    assert event_names(agent) == [
        "session_started",
        "turn_started",
        "user_message",
        "context_usage_recorded",
        "model_requested",
        "model_parsed",
        "assistant_message",
        "turn_finished",
    ]


def test_runtime_default_section_caps_reduce_prefix_before_auto_compact(tmp_path):
    agent = build_agent(tmp_path, ["<summary>old history summarized</summary>"])
    agent.prefix = "PREFIX " + ("A" * 210_000)
    for index in range(5):
        agent.record({"role": "user", "content": f"history-{index}", "created_at": f"2026-04-07T10:0{index}:00+00:00"})

    _prompt, metadata = agent._build_prompt_and_metadata("trigger auto compact")

    assert metadata.get("auto_compacted") is not True
    assert metadata["prompt_over_budget"] is False
    assert metadata["sections"]["prefix"]["rendered_chars"] == agent.context_manager.section_budgets["prefix"]
    assert not agent.session.get("compactions")


def test_engine_wraps_real_tools_with_session_events(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool name="write_file" path="notes/result.txt"><content>ok\n</content></tool>',
            "<final>Wrote it.</final>",
        ],
    )

    answer = agent.ask("write a file")

    assert answer == "Wrote it."
    assert (tmp_path / "notes" / "result.txt").read_text(encoding="utf-8") == "ok\n"
    events = read_session_events(agent)
    names = [event["event"] for event in events]
    assert "tool_started" in names
    assert "tool_finished" in names
    tool_finished = next(event for event in events if event["event"] == "tool_finished")
    assert tool_finished["tool_name"] == "write_file"
    assert tool_finished["status"] == "ok"
    assert tool_finished["workspace_changed"] is True


def test_worker_notifications_use_result_preview_instead_of_full_result():
    from bunnybyte.core.worker_notifications import render_worker_notification

    payload = render_worker_notification(
        {
            "id": "agent_1",
            "description": "scan auth module",
            "status": "completed",
            "result_preview": "found stale token refresh path and missing retry guard",
            "tool_steps": 5,
            "attempts": 2,
            "duration_ms": 3210,
            "report_path": ".bunnybyte/runs/run_worker/report.json",
            "trace_path": ".bunnybyte/runs/run_worker/trace.jsonl",
            "session_event_path": ".bunnybyte/sessions/worker.events.jsonl",
            "tool_error_codes": ["tool_failed"],
        }
    )

    assert "<result_preview>found stale token refresh path and missing retry guard</result_preview>" in payload
    assert "<result>" not in payload
    assert "<report_path>.bunnybyte/runs/run_worker/report.json</report_path>" in payload
    assert "<tool_error_codes>tool_failed</tool_error_codes>" in payload


def test_runtime_prefix_instructs_model_to_batch_independent_read_only_tools(tmp_path):
    agent = build_agent(tmp_path, [])

    prompt = agent.prompt("inspect files")

    assert "request multiple read-only tools in the same response" in prompt
    assert "read_file, list_files, and search" in prompt
    assert "Do not batch write_file, patch_file, run_shell, agent" in prompt


def test_engine_executes_parallel_safe_tools_in_one_batch_and_preserves_order(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '[{"name":"read_file","args":{"path":"a.txt"}}, {"name":"read_file","args":{"path":"b.txt"}}]',
            "<final>Done.</final>",
        ],
        max_steps=4,
    )

    answer = agent.ask("read both")

    assert answer == "Done."
    tool_history = [item for item in agent.session["history"] if item.get("role") == "tool"]
    assert [item["args"]["path"] for item in tool_history] == ["a.txt", "b.txt"]
    assert agent.current_task_state.tool_steps == 2
    events = read_session_events(agent)
    started = [event for event in events if event["event"] == "tool_started"]
    finished = [event for event in events if event["event"] == "tool_finished"]
    assert [event["tool_name"] for event in started[:2]] == ["read_file", "read_file"]
    assert [event["tool_name"] for event in finished[:2]] == ["read_file", "read_file"]


def test_parallel_batch_keeps_risky_tools_serial_between_read_batches(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '[{"name":"read_file","args":{"path":"a.txt"}}, {"name":"write_file","args":{"path":"out.txt","content":"ok\\n"}}, {"name":"read_file","args":{"path":"b.txt"}}]',
            "<final>Done.</final>",
        ],
        max_steps=5,
    )

    answer = agent.ask("read write read")

    assert answer == "Done."
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "ok\n"
    tool_history = [item for item in agent.session["history"] if item.get("role") == "tool"]
    assert [item["name"] for item in tool_history] == ["read_file", "write_file", "read_file"]
    assert [item["args"].get("path") for item in tool_history] == ["a.txt", "out.txt", "b.txt"]


def test_parallel_safe_tool_errors_do_not_cancel_batch(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '[{"name":"read_file","args":{"path":"a.txt"}}, {"name":"read_file","args":{"path":"missing.txt"}}]',
            "<final>Done.</final>",
        ],
        max_steps=4,
    )

    answer = agent.ask("read existing and missing")

    assert answer == "Done."
    tool_history = [item for item in agent.session["history"] if item.get("role") == "tool"]
    assert len(tool_history) == 2
    assert "alpha" in tool_history[0]["content"]
    assert "error:" in tool_history[1]["content"]
    assert agent.current_task_state.tool_steps == 2


def test_plan_mode_allows_only_the_active_plan_artifact_until_plan_is_written(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool name="write_file" path=".bunnybyte/plans/runtime-plan.md"><content># Plan\n- Build Engine\n</content></tool>',
            "<final>Plan ready.</final>",
        ],
        max_steps=3,
    )

    plan_path = agent.enter_plan_mode("runtime")

    assert plan_path == ".bunnybyte/plans/runtime-plan.md"
    assert agent.runtime_mode == "plan"
    rejected = agent.run_tool(
        "write_file", {"path": "src.py", "content": "print('no')\n"}
    )
    assert "plan mode" in rejected
    assert not (tmp_path / "src.py").exists()

    answer = agent.ask("draft the runtime plan")

    assert answer == "Plan ready."
    assert agent.runtime_mode == "default"
    assert (
        (tmp_path / ".bunnybyte" / "plans" / "runtime-plan.md")
        .read_text(encoding="utf-8")
        .startswith("# Plan")
    )
    names = event_names(agent)
    assert names.count("runtime_mode_changed") == 2


def test_plan_mode_rejects_final_before_the_plan_artifact_exists(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>Looks done.</final>",
            '<tool name="write_file" path=".bunnybyte/plans/runtime-plan.md"><content># Plan\n</content></tool>',
            "<final>Now done.</final>",
        ],
        max_steps=3,
    )

    agent.enter_plan_mode("runtime")

    assert agent.ask("make a plan") == "Now done."
    assert any(
        "Plan mode requires writing" in item["content"]
        for item in agent.session["history"]
    )


def test_plan_mode_tools_enter_and_exit_runtime_mode(tmp_path):
    agent = build_agent(tmp_path, [])

    entered = agent.run_tool("enter_plan_mode", {"topic": "Refactor Auth"})

    assert "mode: plan" in entered
    assert ".bunnybyte/plans/refactor-auth-plan.md" in entered
    assert agent.runtime_mode == "plan"
    assert agent.active_tool_profile.name == "plan"

    exited = agent.run_tool("exit_plan_mode", {})

    assert exited == "mode: default"
    assert agent.runtime_mode == "default"
    assert agent.active_tool_profile.name == "default"


def test_plan_path_accepts_absolute_path_inside_workspace(tmp_path):
    """模型偶尔给绝对路径，如 /workspace/repo/.bunnybyte/plans/foo —— 自动相对化，
    不应该让 agent 多走一次重试。"""
    from bunnybyte.core.plan_mode import _plan_path

    assert (
        _plan_path("Student Mgmt", "/workspace/repo/.bunnybyte/plans/student-mgmt.md")
        == ".bunnybyte/plans/student-mgmt.md"
    )
    assert (
        _plan_path("X", "./.bunnybyte/plans/x-plan.md") == ".bunnybyte/plans/x-plan.md"
    )
    # 真正越界的还是要拒
    import pytest

    with pytest.raises(ValueError, match="plan path must stay"):
        _plan_path("X", "/etc/passwd")
    with pytest.raises(ValueError, match="plan path must stay"):
        _plan_path("X", ".bunnybyte/plans/../escape.md")


def test_provider_surface_allows_profiles_without_reintroducing_ollama_client():
    import bunnybyte

    parser = bunnybyte.build_arg_parser()
    provider_action = next(
        action for action in parser._actions if action.dest == "provider"
    )

    assert provider_action.choices is None
    assert not hasattr(bunnybyte, "OllamaModelClient")
    assert parser.parse_args(["--provider", "deepseek"]).provider == "deepseek"
