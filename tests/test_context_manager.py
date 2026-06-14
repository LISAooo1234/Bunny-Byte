from bunnybyte.testing import ScriptedModelClient
from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.core.context_manager import (
    ContextManager,
    DEFAULT_SECTION_BUDGETS,
    DEFAULT_SECTION_FLOORS,
    DEFAULT_TOTAL_BUDGET,
)


def build_workspace(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return WorkspaceContext.build(tmp_path)


def build_agent(tmp_path, outputs, **kwargs):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".bunnybyte" / "sessions")
    approval_policy = kwargs.pop("approval_policy", "auto")
    return BunnyByte(
        model_client=ScriptedModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        **kwargs,
    )


def render_read_file_result(path, marker, total_lines=140):
    body = "\n".join(
        f"{number:>4}: {marker} line {number}"
        for number in range(1, total_lines + 1)
    )
    return (
        f"# {path}\n{body}\n"
        f'<read_file_meta path="{path}" start="1" end="{total_lines}" '
        f'returned_lines="{total_lines}" total_lines="{total_lines}" eof="true" />'
    )


def test_context_manager_default_budget_is_auto_compact_threshold():
    assert DEFAULT_TOTAL_BUDGET == 180_000


def test_context_manager_default_section_budgets_leave_current_request_headroom(tmp_path):
    manager = ContextManager(build_agent(tmp_path, []))

    assert manager.section_budgets == DEFAULT_SECTION_BUDGETS
    assert sum(manager.section_budgets.values()) < DEFAULT_TOTAL_BUDGET
    assert manager.section_budgets["history"] > manager.section_budgets["prefix"] > manager.section_budgets["relevant_memory"]


def test_context_manager_section_floors_use_configured_defaults_and_scale_down_for_small_caps(tmp_path):
    default_manager = ContextManager(build_agent(tmp_path, []))
    assert default_manager.section_floors == {
        section: min(
            DEFAULT_SECTION_FLOORS[section],
            max(20, default_manager.section_budgets[section] // 4),
        )
        for section in DEFAULT_SECTION_FLOORS
    }

    scaled_manager = ContextManager(
        build_agent(tmp_path, []),
        total_budget=900,
        section_budgets={
            "prefix": 120,
            "memory": 120,
            "skills": 60,
            "relevant_memory": 80,
            "history": 260,
        },
    )

    assert scaled_manager.section_floors == {
        "prefix": 30,
        "memory": 30,
        "skills": 20,
        "relevant_memory": 20,
        "history": 65,
    }


def test_context_manager_assembles_sections_in_expected_order(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.memory.append_note("deploy key is red", tags=("deploy",), created_at="2026-04-07T10:00:00+00:00")
    agent.record({"role": "user", "content": "old request", "created_at": "2026-04-07T09:59:00+00:00"})
    agent.record({"role": "assistant", "content": "old answer", "created_at": "2026-04-07T10:00:30+00:00"})

    prompt, metadata = ContextManager(agent).build("Where is the deploy key?")

    assert prompt.index("You are BunnyByte") < prompt.index("Memory:")
    assert prompt.index("Memory:") < prompt.index("Available skills:")
    assert prompt.index("Available skills:") < prompt.index("Relevant memory:")
    assert prompt.index("Relevant memory:") < prompt.index("Transcript:")
    assert prompt.index("Transcript:") < prompt.index("Current user request:")
    assert prompt.rstrip().endswith("Current user request:\nWhere is the deploy key?")
    assert metadata["section_order"] == ["prefix", "memory", "skills", "relevant_memory", "history", "current_request"]


def test_context_manager_does_not_compress_older_turns_when_within_budget(tmp_path):
    agent = build_agent(tmp_path, [])
    old_content = "OLD-CONTEXT " + ("D" * 260)
    old_tool_content = "TOOL-CONTENT " + ("T" * 260)
    agent.record({"role": "user", "content": old_content, "created_at": "2026-04-07T09:59:00+00:00"})
    agent.record(
        {
            "role": "tool",
            "name": "read_file",
            "args": {"path": "old.txt", "start": 1, "end": 5},
            "content": old_tool_content,
            "created_at": "2026-04-07T09:59:30+00:00",
        }
    )
    for minute in range(1, 8):
        role = "assistant" if minute % 2 == 1 else "user"
        agent.record({"role": role, "content": f"recent-{minute}", "created_at": f"2026-04-07T10:0{minute}:00+00:00"})

    prompt, metadata = ContextManager(agent).build("keep full history")

    assert old_content in prompt
    assert old_tool_content in prompt
    assert metadata["history"]["raw_chars"] == metadata["history"]["rendered_chars"]
    assert metadata["history"]["older_entries_count"] == 0
    assert metadata["history"]["summarized_tool_count"] == 0
    assert metadata["history"]["collapsed_duplicate_reads"] == 0
    assert metadata["history"]["budget_clipped"] is False


def test_context_manager_reduces_relevant_memory_before_history_and_preserves_newer_context(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.prefix = "PREFIX " + ("A" * 600)
    agent.memory.render_memory_text = lambda: "MEMORY " + ("B" * 600)
    agent.memory.append_note("keep episodic note one " + ("C" * 220), tags=("keep",), created_at="2026-04-07T10:00:00+00:00")
    agent.memory.append_note("keep episodic note two " + ("D" * 220), tags=("keep",), created_at="2026-04-07T10:01:00+00:00")
    agent.memory.append_note("keep episodic note three " + ("E" * 220), tags=("keep",), created_at="2026-04-07T10:02:00+00:00")
    agent.record({"role": "user", "content": "OLD-CONTEXT " + ("D" * 260), "created_at": "2026-04-07T09:59:00+00:00"})
    for minute in range(1, 8):
        role = "assistant" if minute % 2 == 1 else "user"
        content = "RECENT-CONTEXT " + ("E" * 260) if minute == 7 else f"recent-{minute} " + ("E" * 180)
        agent.record({"role": role, "content": content, "created_at": f"2026-04-07T10:0{minute}:00+00:00"})

    manager = ContextManager(
        agent,
        total_budget=700,
        section_budgets={
            "prefix": 120,
            "memory": 120,
            "skills": 60,
            "relevant_memory": 120,
            "history": 400,
        },
    )

    prompt, metadata = manager.build("keep this request verbatim")

    for section in ("prefix", "memory", "relevant_memory", "history"):
        assert metadata["sections"][section]["rendered_chars"] <= metadata["sections"][section]["budget_chars"]

    reduction_sections = [entry["section"] for entry in metadata["budget_reductions"]]
    assert reduction_sections[0] == "relevant_memory"
    assert reduction_sections
    assert "RECENT-CONTEXT" in prompt
    assert "OLD-CONTEXT" not in prompt
    assert "keep this request verbatim" in prompt


def test_context_manager_renders_top_three_episodic_notes_per_note_under_budget(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.memory.append_note("alpha episodic note " + ("A" * 120), tags=("recall",), created_at="2026-04-07T10:00:00+00:00")
    agent.memory.append_note("beta episodic recall note " + ("B" * 120), created_at="2026-04-07T10:01:00+00:00")
    agent.memory.append_note("gamma episodic note " + ("C" * 120), tags=("recall",), created_at="2026-04-07T10:02:00+00:00")
    agent.memory.append_note("older unmatched note", created_at="2026-04-07T09:59:00+00:00")
    agent.memory.append_note("Unrelated note", created_at="2026-04-07T11:00:00+00:00")

    prompt, metadata = ContextManager(
        agent,
        total_budget=500,
        section_budgets={
            "prefix": 60,
            "memory": 60,
            "skills": 80,
            "relevant_memory": 80,
            "history": 60,
        },
    ).build("recall")

    assert metadata["relevant_memory"]["selected_count"] == 3
    assert metadata["relevant_memory"]["limit"] == 3
    assert metadata["relevant_memory"]["selected_notes"] == [
        "gamma episodic note " + ("C" * 120),
        "alpha episodic note " + ("A" * 120),
        "beta episodic recall note " + ("B" * 120),
    ]
    assert len(metadata["relevant_memory"]["rendered_notes"]) == 3
    assert metadata["relevant_memory"]["rendered_count"] == 3
    assert metadata["relevant_memory"]["rendered_notes"][0].startswith("gamma episodi")
    assert metadata["relevant_memory"]["rendered_notes"][1].startswith("alpha episodi")
    assert metadata["relevant_memory"]["rendered_notes"][2].startswith("beta episodi")
    relevant_section = prompt.split("Relevant memory:\n", 1)[1].split("\n\nTranscript:", 1)[0]
    assert len([line for line in relevant_section.splitlines() if line.startswith("- ")]) == 3
    assert "alpha episodi" in relevant_section
    assert "beta episodic" in relevant_section
    assert "gamma episodi" in relevant_section
    assert "older unmatched note" not in relevant_section


def test_context_manager_preserves_current_request_when_over_budget(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.prefix = "PREFIX " + ("A" * 600)
    agent.memory.render_memory_text = lambda: "MEMORY " + ("B" * 600)
    agent.memory.retrieval_view = lambda query, limit=3: "Relevant memory:\n" + "\n".join(f"- {i} " + ("C" * 220) for i in range(5))
    agent.history_text = lambda: "Transcript:\n" + "\n".join(f"[user] {i} " + ("D" * 220) for i in range(5))

    request = "please preserve this request exactly"
    prompt, metadata = ContextManager(
        agent,
        total_budget=250,
        section_budgets={
            "prefix": 80,
            "memory": 80,
            "skills": 40,
            "relevant_memory": 80,
            "history": 80,
        },
    ).build(request)

    assert prompt.split("Current user request:\n", 1)[1] == request
    assert metadata["current_request"]["text"] == request
    assert metadata["current_request"]["rendered_chars"] == len(request)


def test_context_manager_collapses_older_duplicate_reads_into_one_summary_line(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\nbeta\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])
    agent.memory.set_file_summary("sample.txt", "alpha | beta")
    agent.memory.remember_file("sample.txt")

    for created_at in ("2026-04-07T09:00:00+00:00", "2026-04-07T09:01:00+00:00"):
        agent.record(
            {
                "role": "tool",
                "name": "read_file",
                "args": {"path": "sample.txt", "start": 1, "end": 2},
                "content": "# sample.txt\nalpha\nbeta\n",
                "created_at": created_at,
            }
        )

    for minute in range(2, 8):
        role = "user" if minute % 2 == 0 else "assistant"
        agent.record(
            {
                "role": role,
                "content": f"recent-{minute}",
                "created_at": f"2026-04-07T09:0{minute}:00+00:00",
            }
        )

    prompt, metadata = ContextManager(
        agent,
        total_budget=900,
        section_budgets={
            "prefix": 120,
            "memory": 120,
            "skills": 60,
            "relevant_memory": 80,
            "history": 260,
        },
    ).build("check the file")
    transcript = prompt.split("\n\nTranscript:\n", 1)[1].split("\n\nCurrent user request:", 1)[0]

    assert transcript.count("[tool:read_file]") == 0
    assert "sample.txt -> alpha | beta" in transcript
    assert metadata["history"]["older_entries_count"] == 1
    assert metadata["history"]["collapsed_duplicate_reads"] == 1
    assert metadata["history"]["reused_file_summary_count"] == 1


def test_context_manager_summarizes_older_tool_output_into_one_line(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.record(
        {
            "role": "tool",
            "name": "run_shell",
            "args": {"command": "pytest -q"},
            "content": "FAIL test_one\nFAIL test_two\nFAIL test_three\nFAIL test_four\n",
            "created_at": "2026-04-07T09:00:00+00:00",
        }
    )

    for minute in range(1, 7):
        role = "user" if minute % 2 == 1 else "assistant"
        agent.record(
            {
                "role": role,
                "content": f"recent-{minute}",
                "created_at": f"2026-04-07T09:0{minute}:00+00:00",
            }
        )

    prompt, metadata = ContextManager(
        agent,
        total_budget=900,
        section_budgets={
            "prefix": 120,
            "memory": 120,
            "skills": 60,
            "relevant_memory": 80,
            "history": 260,
        },
    ).build("check failures")
    transcript = prompt.split("\n\nTranscript:\n", 1)[1].split("\n\nCurrent user request:", 1)[0]

    assert 'pytest -q -> FAIL test_one | FAIL test_two | FAIL test_three' in transcript
    assert "FAIL test_four" not in transcript
    assert metadata["history"]["summarized_tool_count"] == 1
    assert metadata["history"]["reused_file_summary_count"] == 0


def test_context_manager_microcompacts_large_current_turn_before_budget_cliff(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.current_turn_id = "task_000001"
    agent.record({"role": "user", "content": "inspect the repository", "created_at": "2026-04-07T09:00:00+00:00"})
    for index in range(1, 9):
        agent.record(
            {
                "role": "tool",
                "name": "read_file",
                "args": {"path": f"file_{index}.py", "start": 1, "end": 140},
                "content": render_read_file_result(f"file_{index}.py", f"MARKER-{index}"),
                "created_at": f"2026-04-07T09:{index:02d}:00+00:00",
            }
        )
    agent.record({"role": "assistant", "content": "still checking dependencies", "created_at": "2026-04-07T09:10:00+00:00"})

    prompt, metadata = ContextManager(
        agent,
        total_budget=40_000,
        section_budgets={
            "prefix": 120,
            "memory": 120,
            "skills": 60,
            "relevant_memory": 80,
            "history": 20_000,
        },
    ).build("continue")
    transcript = prompt.split("\n\nTranscript:\n", 1)[1].split("\n\nCurrent user request:", 1)[0]

    assert metadata["history"]["older_entries_count"] == 0
    assert metadata["history"]["same_turn_compacted_entries"] >= 4
    assert metadata["history"]["same_turn_compacted_tools"] >= 4
    assert metadata["history"]["budget_clipped"] is False
    assert "MARKER-1 line 1" not in transcript
    assert "file_1.py lines 1-140 of 140 complete" in transcript
    assert "MARKER-8 line 1" in transcript


def test_context_manager_microcompacts_worker_notifications_in_current_turn(tmp_path):
    from bunnybyte.core.worker_notifications import render_worker_notification

    agent = build_agent(tmp_path, [])
    agent.current_turn_id = "task_000002"
    agent.record({"role": "user", "content": "audit auth flow", "created_at": "2026-04-07T09:00:00+00:00"})
    agent.record(
        {
            "role": "user",
            "content": render_worker_notification(
                {
                    "id": "agent_1",
                    "description": "scan auth module",
                    "status": "completed",
                    "result_preview": "reviewed auth flow and found stale token refresh path",
                    "tool_steps": 5,
                    "attempts": 2,
                    "duration_ms": 3210,
                    "report_path": ".bunnybyte/runs/run_worker/report.json",
                    "trace_path": ".bunnybyte/runs/run_worker/trace.jsonl",
                    "session_event_path": ".bunnybyte/sessions/worker.events.jsonl",
                    "tool_error_codes": [],
                }
            ),
            "created_at": "2026-04-07T09:01:00+00:00",
        }
    )
    for index in range(1, 7):
        agent.record(
            {
                "role": "tool",
                "name": "read_file",
                "args": {"path": f"auth_{index}.py", "start": 1, "end": 140},
                "content": render_read_file_result(f"auth_{index}.py", f"AUTH-{index}"),
                "created_at": f"2026-04-07T09:{index + 1:02d}:00+00:00",
            }
        )
    agent.record({"role": "assistant", "content": "done inspecting", "created_at": "2026-04-07T09:09:00+00:00"})

    prompt, metadata = ContextManager(
        agent,
        total_budget=32_000,
        section_budgets={
            "prefix": 120,
            "memory": 120,
            "skills": 60,
            "relevant_memory": 80,
            "history": 16_000,
        },
    ).build("continue")
    transcript = prompt.split("\n\nTranscript:\n", 1)[1].split("\n\nCurrent user request:", 1)[0]

    assert "worker agent_1 completed" in transcript
    assert "reviewed auth flow and found stale token refresh path" in transcript
    assert "<task-notification>" not in transcript
    assert metadata["history"]["same_turn_compacted_notifications"] == 1


def test_context_manager_relevant_memory_can_mix_durable_notes(tmp_path):
    memory_root = tmp_path / ".bunnybyte" / "memory"
    topics_dir = memory_root / "topics"
    topics_dir.mkdir(parents=True)
    (memory_root / "MEMORY.md").write_text(
        "# Durable Memory Index\n\n"
        "- [project-conventions](topics/project-conventions.md): Project Conventions\n"
        "  - summary: Stable repository conventions.\n"
        "  - tags: convention\n",
        encoding="utf-8",
    )
    (topics_dir / "project-conventions.md").write_text(
        "# Project Conventions\n\n"
        "- topic: project-conventions\n"
        "- summary: Stable repository conventions.\n"
        "- tags: convention\n"
        "- updated_at: 2026-04-12T08:14:49+00:00\n\n"
        "## Notes\n"
        "- Use constrained tools instead of guessing.\n",
        encoding="utf-8",
    )

    agent = build_agent(tmp_path, [])

    prompt, metadata = ContextManager(agent).build("What conventions should I follow?")
    relevant_section = prompt.split("Relevant memory:\n", 1)[1].split("\n\nTranscript:", 1)[0]

    assert "Use constrained tools instead of guessing." in relevant_section
    assert any("Use constrained tools instead of guessing." in item for item in metadata["relevant_memory"]["selected_notes"])
    assert metadata["relevant_memory"]["selected_durable_count"] == 1
    assert metadata["relevant_memory"]["selected_sources"] == ["project-conventions"]
    assert metadata["relevant_memory"]["selected_kinds"] == ["durable"]
