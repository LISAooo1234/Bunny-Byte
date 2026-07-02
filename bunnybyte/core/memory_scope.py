"""Memory scope controls for durable and cross-session recall."""

from __future__ import annotations

from .transcript_recall import retrieve_transcript_snippets

DEFAULT_MEMORY_SCOPE_MODE = "project"
MEMORY_SCOPE_MODES = {"off", "project", "selected", "all"}
MEMORY_SCOPE_ALIASES = {"global": "all", "none": "off", "default": "project"}
MEMORY_SCOPE_SESSION_CAP = 12
MEMORY_SCOPE_STARTUP_PROMPT_ATTR = "_memory_scope_startup_prompted"


def default_memory_scope():
    return {"mode": DEFAULT_MEMORY_SCOPE_MODE, "session_ids": []}


def normalize_memory_scope(scope):
    if not isinstance(scope, dict):
        return default_memory_scope()
    mode = str(scope.get("mode", DEFAULT_MEMORY_SCOPE_MODE) or DEFAULT_MEMORY_SCOPE_MODE).strip().lower()
    mode = MEMORY_SCOPE_ALIASES.get(mode, mode)
    if mode not in MEMORY_SCOPE_MODES:
        mode = DEFAULT_MEMORY_SCOPE_MODE
    session_ids = [str(item).strip() for item in scope.get("session_ids", []) or [] if str(item).strip()]
    if mode != "selected":
        session_ids = []
    return {"mode": mode, "session_ids": _dedupe(session_ids)}


def set_memory_scope(session, mode, session_ids=None):
    mode = MEMORY_SCOPE_ALIASES.get(str(mode or "").strip().lower(), str(mode or "").strip().lower())
    if mode not in MEMORY_SCOPE_MODES:
        raise ValueError("memory scope must be one of off, project, selected, all")
    scope = {"mode": mode, "session_ids": list(session_ids or [])}
    normalized = normalize_memory_scope(scope)
    if mode == "selected" and not normalized["session_ids"]:
        raise ValueError("selected memory scope requires at least one session id")
    session["memory_scope"] = normalized
    return normalized


def should_prompt_memory_scope_startup(agent):
    return bool(getattr(agent, "is_pending_session", False)) and not bool(
        getattr(agent, MEMORY_SCOPE_STARTUP_PROMPT_ATTR, False)
    )


def mark_memory_scope_startup_prompted(agent):
    setattr(agent, MEMORY_SCOPE_STARTUP_PROMPT_ATTR, True)


def durable_memory_enabled(session):
    mode = normalize_memory_scope(session.get("memory_scope")).get("mode")
    return mode in {"project", "all"}


def scoped_session_ids(agent):
    scope = normalize_memory_scope(agent.session.get("memory_scope"))
    mode = scope["mode"]
    current_id = str(agent.session.get("id", ""))
    if mode == "selected":
        return [session_id for session_id in scope["session_ids"] if session_id != current_id]
    if mode != "all":
        return []
    rows = agent.session_store.list_sessions()
    ids = [str(row.get("id", "")) for row in rows if str(row.get("id", "")) and str(row.get("id", "")) != current_id]
    return ids[:MEMORY_SCOPE_SESSION_CAP]


def retrieve_scoped_session_snippets(agent, query, limit=4, char_budget=4_000):
    snippets = []
    for session_id in scoped_session_ids(agent):
        try:
            session = agent.session_store.load(session_id)
        except Exception:
            continue
        candidates = retrieve_transcript_snippets(
            query,
            session.get("history", []),
            visible_event_ids=set(),
            turn_index=session.get("turn_index", {}),
            limit=limit,
            char_budget=char_budget,
        )
        topic = str(session.get("topic", "") or "").strip()
        for snippet in candidates:
            enriched = dict(snippet)
            enriched["source_session_id"] = session_id
            enriched["source_session_topic"] = topic
            snippets.append(enriched)
    snippets.sort(key=lambda item: (int(item.get("score", 0)), str(item.get("created_at", ""))), reverse=True)
    return snippets[:limit]


def scope_summary(agent):
    scope = normalize_memory_scope(agent.session.get("memory_scope"))
    mode = scope["mode"]
    session_ids = scoped_session_ids(agent)
    return {
        "mode": mode,
        "session_ids": session_ids if mode in {"selected", "all"} else [],
        "durable_memory": durable_memory_enabled(agent.session),
        "cross_session_recall": mode in {"selected", "all"},
    }


def render_memory_scope(agent):
    summary = scope_summary(agent)
    rows = [
        ("Mode", summary["mode"]),
        ("Durable memory", "on" if summary["durable_memory"] else "off"),
        ("Cross-session recall", "on" if summary["cross_session_recall"] else "off"),
        ("Sessions", ", ".join(summary["session_ids"]) or "-"),
    ]
    lines = ["## Memory Scope", "", "| Field | Value |", "| --- | --- |"]
    for label, value in rows:
        lines.append(f"| {label} | `{value}` |")
    lines.extend(
        [
            "",
            "Usage: `/memory-scope off|project|all|selected <session...>`",
        ]
    )
    return "\n".join(lines)


def _dedupe(values):
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
