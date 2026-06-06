import json

from bunnybyte.testing import ScriptedModelClient
from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.core.context_manager import ContextManager


def build_agent(tmp_path, outputs=None, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".bunnybyte" / "sessions")
    return BunnyByte(
        model_client=ScriptedModelClient(outputs or []),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        **kwargs,
    )


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_context_usage_is_recorded_for_real_turn_report_and_session_events(tmp_path):
    agent = build_agent(tmp_path, ["<final>hello</final>"])

    assert agent.ask("hi") == "hello"

    report = json.loads((agent.current_run_dir / "report.json").read_text(encoding="utf-8"))
    usage = report["prompt_metadata"]["context_usage"]

    assert usage["estimation_method"] == "chars_div_4"
    assert usage["sections"]["prefix"]["chars"] > 0
    assert usage["sections"]["tools"]["chars"] > 0
    assert usage["sections"]["current_request"]["chars"] == len("Current user request:\nhi")
    assert usage["total_estimated_tokens"] == sum(section["tokens"] for section in usage["sections"].values())
    assert usage["free_tokens"] == usage["context_window"] - usage["total_estimated_tokens"] - usage["reserved_output_tokens"]

    events = read_jsonl(agent.session_event_bus.path)
    assert any(event["event"] == "context_usage_recorded" and event["run_id"] == agent.current_task_state.run_id for event in events)


def test_history_records_turn_ids_and_renders_without_orphan_tool_results(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":1}}</tool>',
            "<final>read it</final>",
            "<final>second done</final>",
        ],
    )

    assert agent.ask("read the file") == "read it"
    assert agent.ask("continue") == "second done"

    history = agent.session["history"]
    assert all(item["turn_id"] for item in history)
    assert {item["run_id"] for item in history} >= {agent.current_task_state.run_id}

    prompt = agent.prompt("summarize")
    assert "Turn " in prompt
    assert "[tool:read_file]" in prompt
    assert prompt.index("[user] read the file") < prompt.index("[tool:read_file]")


def test_manual_compact_creates_summary_event_and_shortens_future_history(tmp_path):
    agent = build_agent(tmp_path, ["<summary>Detailed compact summary for old work.</summary>"])
    for index in range(16):
        agent.record({"role": "user", "content": f"old request {index} " + ("x" * 80), "created_at": f"2026-05-12T10:{index:02d}:00+00:00"})
        agent.record({"role": "assistant", "content": f"old answer {index} " + ("y" * 80), "created_at": f"2026-05-12T10:{index:02d}:30+00:00"})
    original_history = list(agent.session["history"])

    before_prompt = agent.prompt("next")
    summary = agent.compact_history(trigger="manual")
    after_prompt = agent.prompt("next")

    assert summary["trigger"] == "manual"
    assert summary["pre_tokens"] > summary["post_tokens"]
    assert summary["strategy"] == "model_summary_v1"
    assert summary["summary"] == "Detailed compact summary for old work."
    assert agent.session["history"] == original_history
    assert agent.session["context_view"]["active_compaction_id"] == summary["id"]
    assert "Compacted context summary:" in after_prompt
    assert "Detailed compact summary for old work." in after_prompt
    assert "old request 0" not in after_prompt
    assert len(after_prompt) < len(before_prompt)

    events = read_jsonl(agent.session_event_bus.path)
    assert any(event["event"] == "compaction_created" and event["trigger"] == "manual" for event in events)


def test_prompt_over_budget_triggers_auto_compaction_during_real_turn(tmp_path):
    agent = build_agent(tmp_path, ["<summary>Auto compact summary.</summary>", "<final>done</final>"])
    agent.context_manager = ContextManager(
        agent,
        total_budget=100,
        section_budgets={"prefix": 40, "memory": 40, "relevant_memory": 40, "history": 40},
        section_floors={"prefix": 40, "memory": 40, "relevant_memory": 40, "history": 40},
    )
    for index in range(8):
        agent.record({"role": "user", "content": f"old request {index} " + ("x" * 80), "created_at": f"2026-05-12T10:{index:02d}:00+00:00"})
        agent.record({"role": "assistant", "content": f"old answer {index} " + ("y" * 80), "created_at": f"2026-05-12T10:{index:02d}:30+00:00"})

    assert agent.ask("finish") == "done"

    assert agent.last_prompt_metadata["auto_compacted"] is True
    assert any(item["trigger"] == "auto_prompt_over_budget" for item in agent.session["compactions"])
    assert agent.session["context_view"]["active_compaction_id"]


def test_resume_uses_context_view_without_destroying_full_history(tmp_path):
    agent = build_agent(tmp_path, ["<summary>Resume compact summary.</summary>"])
    for index in range(6):
        agent.record({"role": "user", "content": f"resume old request {index}", "created_at": f"2026-05-12T10:{index:02d}:00+00:00"})
        agent.record({"role": "assistant", "content": f"resume old answer {index}", "created_at": f"2026-05-12T10:{index:02d}:30+00:00"})
    original_id = agent.session["id"]
    original_history = list(agent.session["history"])
    agent.compact_history(trigger="manual", keep_recent_turns=2)

    resumed = BunnyByte.from_session(
        model_client=ScriptedModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=agent.session_store,
        session_id=original_id,
        approval_policy="auto",
    )

    assert resumed.session["history"] == original_history
    prompt = resumed.prompt("continue")
    assert "Resume compact summary." in prompt
    assert "resume old request 0" not in prompt
    assert "resume old request 5" in prompt


def test_compact_summary_failure_keeps_history_and_uses_fallback(tmp_path):
    agent = build_agent(tmp_path, [RuntimeError("summary unavailable")])
    for index in range(5):
        agent.record({"role": "user", "content": f"fallback request {index}", "created_at": f"2026-05-12T10:{index:02d}:00+00:00"})
        agent.record({"role": "assistant", "content": f"fallback answer {index}", "created_at": f"2026-05-12T10:{index:02d}:30+00:00"})
    original_history = list(agent.session["history"])

    summary = agent.compact_history(trigger="manual", keep_recent_turns=2)

    assert agent.session["history"] == original_history
    assert summary["summary"].startswith("Compacted session summary:")
    assert "fallback request 0" not in agent.prompt("continue")
