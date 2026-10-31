"""Shared rendering and metadata helpers for ContextManager."""

from __future__ import annotations

from dataclasses import dataclass

from ..features import skills as skillslib
from .context_usage import ContextUsageAnalyzer
from .memory_scope import scope_summary
from .transcript_recall import render_prompt_recall


@dataclass
class SectionRender:
    raw: str
    budget: int
    rendered: str
    details: dict | None = None

    @property
    def raw_chars(self):
        return len(self.raw)

    @property
    def rendered_chars(self):
        return len(self.rendered)


def per_note_budget(budget, note_count, header):
    if note_count <= 0:
        return 0
    overhead = len(header) + 3 * note_count
    usable = max(0, budget - overhead)
    return max(1, usable // note_count)


def render_sections_without_reduction(
    agent,
    history_builder,
    section_texts,
    selected_notes=None,
    selected_recall=None,
    current_request_section="current_request",
):
    selected_notes = selected_notes or []
    selected_recall = selected_recall or []
    relevant_lines = ["Relevant memory:"]
    if selected_notes:
        relevant_lines.extend(f"- {note['text']}" for note in selected_notes)
    else:
        relevant_lines.append("- none")
    relevant_raw = "\n".join(relevant_lines)
    history = list(getattr(agent, "session", {}).get("history", []))
    history_raw = history_builder.raw_text(history)
    recall_raw = render_prompt_recall(
        selected_recall,
        budget=sum(len(str(snippet)) for snippet in selected_recall) + 400,
    )
    plain = {
        section: SectionRender(raw=text, budget=len(text), rendered=text, details={})
        for section, text in section_texts.items()
        if section not in {"history", current_request_section}
    }
    return {
        **plain,
        "relevant_memory": SectionRender(
            raw=relevant_raw,
            budget=len(relevant_raw),
            rendered=relevant_raw,
            details={
                "selected_notes": [note["text"] for note in selected_notes],
                "rendered_notes": [note["text"] for note in selected_notes],
                "selected_count": len(selected_notes),
                "rendered_count": len(selected_notes),
                "note_budget": 0,
            },
        ),
        "transcript_recall": SectionRender(
            raw=recall_raw,
            budget=len(recall_raw),
            rendered=recall_raw,
            details={"selected_snippets": selected_recall, "rendered_count": len(selected_recall)},
        ),
        "history": SectionRender(
            raw=history_raw,
            budget=len(history_raw),
            rendered=history_raw,
            details={"rendered_entries": []},
        ),
        current_request_section: SectionRender(
            raw=section_texts[current_request_section],
            budget=0,
            rendered=section_texts[current_request_section],
            details={},
        ),
    }


def build_context_metadata(
    manager,
    prompt,
    rendered,
    budgets,
    reduction_log,
    selected_notes,
    selected_recall,
    user_message,
    section_texts,
):
    section_order = manager.SECTION_ORDER
    current_request_section = manager.CURRENT_REQUEST_SECTION
    section_metadata = {}
    for section in section_order[:-1]:
        section_metadata[section] = {
            "raw_chars": rendered[section].raw_chars,
            "budget_chars": int(budgets.get(section, 0)),
            "rendered_chars": rendered[section].rendered_chars,
        }
    section_metadata[current_request_section] = {
        "raw_chars": len(section_texts[current_request_section]),
        "budget_chars": None,
        "rendered_chars": len(rendered[current_request_section].rendered),
    }
    return {
        "prompt_chars": len(prompt),
        "prompt_budget_chars": manager.total_budget,
        "prompt_over_budget": len(prompt) > manager.total_budget,
        "section_order": list(section_order),
        "section_budgets": {
            section: (None if section == current_request_section else int(budgets.get(section, 0)))
            for section in section_order
        },
        "sections": section_metadata,
        "budget_reductions": reduction_log,
        "reduction_order": list(manager.reduction_order),
        "relevant_memory": _relevant_memory_metadata(manager, rendered, selected_notes),
        "transcript_recall": _transcript_recall_metadata(rendered, selected_recall),
        "history": _history_metadata(rendered),
        "skills": _skills_metadata(manager.agent),
        "current_request": {
            "text": user_message,
            "raw_chars": len(user_message),
            "rendered_chars": len(user_message),
            "section_chars": len(rendered[current_request_section].rendered),
        },
        "context_usage": ContextUsageAnalyzer(manager.agent).analyze(rendered),
    }


def _relevant_memory_metadata(manager, rendered, selected_notes):
    return {
        "limit": manager.RELEVANT_MEMORY_LIMIT,
        "memory_scope": scope_summary(manager.agent),
        "selected_count": len(selected_notes),
        "selected_notes": [note["text"] for note in selected_notes],
        "selected_sources": [str(note.get("source", "")).strip() for note in selected_notes],
        "selected_kinds": [
            str(note.get("kind", "episodic")).strip() or "episodic"
            for note in selected_notes
        ],
        "selected_durable_count": sum(
            1
            for note in selected_notes
            if (str(note.get("kind", "episodic")).strip() or "episodic") == "durable"
        ),
        "raw_chars": rendered["relevant_memory"].raw_chars,
        "rendered_chars": rendered["relevant_memory"].rendered_chars,
        "rendered_notes": list(rendered["relevant_memory"].details.get("rendered_notes", [])),
        "rendered_count": int(rendered["relevant_memory"].details.get("rendered_count", 0)),
    }


def _transcript_recall_metadata(rendered, selected_recall):
    return {
        "limit": 4,
        "selected_count": len(selected_recall),
        "selected_turns": [str(snippet.get("turn_id", "")) for snippet in selected_recall],
        "source_event_ids": [
            event_id
            for snippet in selected_recall
            for event_id in list(snippet.get("event_ids", []) or [])
        ],
        "raw_chars": rendered["transcript_recall"].raw_chars,
        "rendered_chars": rendered["transcript_recall"].rendered_chars,
        "rendered_count": int(rendered["transcript_recall"].details.get("rendered_count", 0)),
    }


def _history_metadata(rendered):
    details = rendered["history"].details
    return {
        "raw_chars": rendered["history"].raw_chars,
        "rendered_chars": rendered["history"].rendered_chars,
        "older_entries_count": int(details.get("older_entries_count", 0)),
        "collapsed_duplicate_reads": int(details.get("collapsed_duplicate_reads", 0)),
        "reused_file_summary_count": int(details.get("reused_file_summary_count", 0)),
        "summarized_tool_count": int(details.get("summarized_tool_count", 0)),
        "same_turn_compacted_entries": int(details.get("same_turn_compacted_entries", 0)),
        "same_turn_compacted_tools": int(details.get("same_turn_compacted_tools", 0)),
        "same_turn_compacted_notifications": int(details.get("same_turn_compacted_notifications", 0)),
        "budget_clipped": bool(details.get("budget_clipped", False)),
        "rendered_turns": int(details.get("rendered_turns", 0)),
        # 本轮哪些 read_file 结果在提示词里、看得到第几行到第几行；读文件去重据此判断要不要重新给内容
        "visible_reads": dict(details.get("visible_reads") or {}),
        "rendered_through": str(details.get("rendered_through", "")),
        "item_cap": details.get("item_cap"),
    }


def _skills_metadata(agent):
    skills = getattr(agent, "skills", {})
    items = [skill.metadata() for skill in skillslib.list_skills(skills, user_invocable_only=False)]
    return {
        "available_count": len(items),
        "user_invocable_count": sum(1 for item in items if item["user_invocable"]),
        "items": items,
    }
