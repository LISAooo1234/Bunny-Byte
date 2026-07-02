import json
from datetime import datetime, timezone
from pathlib import Path

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.core.task_state import TaskState
from bunnybyte.features.skills_runtime import invoke_skill
from bunnybyte.testing import ScriptedModelClient


def build_agent(tmp_path, outputs=None, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    return BunnyByte(
        model_client=ScriptedModelClient(outputs or []),
        workspace=workspace,
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        approval_policy="auto",
        **kwargs,
    )


def seed_completed_turn(agent):
    agent.current_turn_id = "turn_skill"
    agent.current_run_id = "run_skill"
    agent.record({"role": "user", "content": "We fixed a repeated paper-review workflow."})
    agent.record(
        {
            "role": "tool",
            "name": "read_file",
            "args": {"path": "README.md"},
            "content": "important evidence",
        }
    )
    agent.record({"role": "assistant", "content": "Done with verification."})
    task = TaskState.create(
        task_id="turn_skill",
        run_id="run_skill",
        user_request="Fix the repeated workflow",
    )
    task.tool_steps = 3
    task.finish_success("Done with verification.")
    return task


def accepted_proposal():
    return {
        "completed": True,
        "should_create_skill": True,
        "skill": {
            "name": "paper-review-workflow",
            "description": "Review related papers and preserve reusable comparison steps.",
            "when_to_use": "Use when comparing multiple papers in the same research area.",
            "not_when_to_use": "Do not use for one-off paper summaries without cross-paper workflow reuse.",
            "steps": [
                "Identify the paper goal and neighboring papers.",
                "Compare assumptions, methods, and evaluation limits.",
                "Record verification questions before final synthesis.",
            ],
            "verification": ["Check that the final answer cites the compared papers explicitly."],
        },
        "evidence": {
            "why": "The turn produced a repeatable paper-comparison workflow.",
            "trigger_signals": ["multi-step workflow", "verification step"],
            "key_history": ["event_000001 user asked for a repeated paper-review workflow"],
            "reusable_steps": ["compare assumptions", "verify claims"],
            "source_event_ids": ["event_000001"],
            "confidence": 0.86,
        },
    }


def accepted_review():
    return {
        "decision": "accept",
        "necessity_score": 0.82,
        "reasons": ["Evidence is concrete and the workflow is reusable."],
        "risks": [],
        "required_changes": [],
    }


def test_skill_evolution_promotes_accepted_proposal(tmp_path, monkeypatch):
    agent = build_agent(tmp_path, [])
    task = seed_completed_turn(agent)
    manager = agent.skill_evolution
    responses = iter(
        [
            (f"<final>{json.dumps(accepted_proposal())}</final>", "proposal-agent"),
            (f"<final>{json.dumps(accepted_review())}</final>", "review-agent"),
        ]
    )
    monkeypatch.setattr(manager, "_run_isolated_agent", lambda _role, _prompt: next(responses))

    result = manager.review_completed_turn(task.to_dict(), "Done with verification.")

    skill_path = tmp_path / ".bunnybyte" / "skills" / "paper-review-workflow" / "SKILL.md"
    evidence_path = (
        tmp_path
        / ".bunnybyte"
        / "skills"
        / "paper-review-workflow"
        / "references"
        / f"evidence-{result['proposal_id']}.md"
    )
    assert result["status"] == "promoted"
    assert skill_path.exists()
    assert evidence_path.exists()
    text = skill_path.read_text(encoding="utf-8")
    evidence_text = evidence_path.read_text(encoding="utf-8")
    assert "agent-created: true" in text
    assert "not-when-to-use: Do not use for one-off paper summaries" in text
    assert "When Not To Use" in text
    assert "source_event_id: event_000001" in text
    assert "## Source Event IDs" in evidence_text
    assert "event_000001" in evidence_text
    assert "paper-review-workflow" in agent.skills
    tree = (
        tmp_path / ".bunnybyte" / "skill-evolution" / "SKILL_TREE.md"
    ).read_text(encoding="utf-8")
    assert "avoid when: Do not use for one-off paper summaries" in tree
    assert "Skill routing index:" in agent.prefix
    assert ".bunnybyte/skill-evolution/SKILL_TREE.md" in agent.prefix


def test_skill_evolution_rejects_weak_evidence(tmp_path, monkeypatch):
    agent = build_agent(tmp_path, [])
    task = seed_completed_turn(agent)
    manager = agent.skill_evolution
    weak = accepted_proposal()
    weak["evidence"] = {"why": "", "confidence": 0.2}
    monkeypatch.setattr(
        manager,
        "_run_isolated_agent",
        lambda _role, _prompt: (f"<final>{json.dumps(weak)}</final>", "proposal-agent"),
    )

    result = manager.review_completed_turn(task.to_dict(), "Done with verification.")

    assert result["status"] == "rejected_quality_gate"
    assert not (tmp_path / ".bunnybyte" / "skills" / "paper-review-workflow").exists()
    assert "missing evidence.why" in result["quality_errors"]


def test_after_turn_skips_scripted_model_without_curator_side_effect(tmp_path):
    agent = build_agent(tmp_path, [])
    task = seed_completed_turn(agent)

    result = agent.skill_evolution.after_turn(task, "Done with verification.")

    assert result == {"started": False, "reason": "background_gate"}
    assert not (tmp_path / ".bunnybyte" / "skill-evolution" / "curator_state.json").exists()


def test_skill_curator_stages_low_usage_delete_without_deleting(tmp_path):
    agent = build_agent(tmp_path, [])
    skill_dir = tmp_path / ".bunnybyte" / "skills" / "old-generated-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: old-generated-skill
description: Old generated workflow
when-to-use: Use for old generated work
agent-created: true
created-by: bunnybyte-skill-evolution
created-at: 2026-01-01T00:00:00+00:00
---
Old workflow.
""",
        encoding="utf-8",
    )

    result = agent.skill_evolution.maybe_run_curator(
        force=True,
        now_dt=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )

    actions = result["report"]["actions"]
    assert actions
    assert actions[0]["action"] == "delete"
    assert actions[0]["approval_required"] is True
    assert actions[0]["status"] == "pending_user_approval"
    assert (skill_dir / "SKILL.md").exists()


def test_skill_curator_reviews_manual_low_usage_without_delete_action(tmp_path):
    agent = build_agent(tmp_path, [])
    skill_dir = tmp_path / "skills" / "manual-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: manual-skill
description: Manual workflow
when-to-use: Use for manual work
created-at: 2026-01-01T00:00:00+00:00
---
Manual workflow.
""",
        encoding="utf-8",
    )

    result = agent.skill_evolution.maybe_run_curator(
        force=True,
        now_dt=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )

    actions = result["report"]["actions"]
    assert actions
    assert actions[0]["action"] == "low_usage_review"
    assert actions[0]["approval_required"] is False
    assert (skill_dir / "SKILL.md").exists()


def test_skill_curator_approval_archives_instead_of_rm(tmp_path):
    agent = build_agent(tmp_path, [])
    skill_dir = tmp_path / ".bunnybyte" / "skills" / "old-generated-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: old-generated-skill
description: Old generated workflow
when-to-use: Use for old generated work
agent-created: true
created-by: bunnybyte-skill-evolution
created-at: 2026-01-01T00:00:00+00:00
---
Old workflow.
""",
        encoding="utf-8",
    )
    result = agent.skill_evolution.maybe_run_curator(
        force=True,
        now_dt=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    maintenance_id = result["report"]["maintenance_id"]

    action = agent.skill_evolution.approve_maintenance_action(maintenance_id, 1)

    assert action["status"] == "approved_archived"
    assert not skill_dir.exists()
    assert (tmp_path / ".bunnybyte" / "skills" / ".archive").exists()
    assert Path(action["archive_path"]).exists()


def test_skill_evolution_slash_curator_and_approve(tmp_path):
    from bunnybyte.cli import handle_repl_command

    agent = build_agent(tmp_path, [])
    skill_dir = tmp_path / ".bunnybyte" / "skills" / "old-generated-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: old-generated-skill
description: Old generated workflow
when-to-use: Use for old generated work
agent-created: true
created-by: bunnybyte-skill-evolution
created-at: 2026-01-01T00:00:00+00:00
---
Old workflow.
""",
        encoding="utf-8",
    )

    handled, should_exit, output = handle_repl_command(agent, "/skill-evolution curator")

    assert handled is True
    assert should_exit is False
    assert "## Skill Curator" in output
    maintenance_id = next(
        path.stem for path in (tmp_path / ".bunnybyte" / "skill-evolution" / "maintenance").glob("*.json")
    )

    handled, should_exit, output = handle_repl_command(
        agent,
        f"/skill-evolution approve {maintenance_id} 1",
    )

    assert handled is True
    assert should_exit is False
    assert "approved_archived" in output
    assert not skill_dir.exists()


def test_skill_invocation_records_usage(tmp_path):
    agent = build_agent(tmp_path, [])
    skill_dir = tmp_path / ".bunnybyte" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: demo-skill
description: Demo skill
disable-model-invocation: true
---
Run demo $ARGUMENTS.
""",
        encoding="utf-8",
    )
    agent.refresh_skills()

    assert "Run demo now." == invoke_skill(agent, "demo-skill", "now")

    usage = json.loads(
        (tmp_path / ".bunnybyte" / "skill-evolution" / "skill_usage.json").read_text(
            encoding="utf-8"
        )
    )
    assert usage["demo-skill"]["use_count"] == 1
