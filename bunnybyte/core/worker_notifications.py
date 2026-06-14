"""Worker notification rendering."""

from xml.sax.saxutils import escape


def render_worker_notification(item):
    result_preview = str(item.get("result_preview", "")).strip()
    parts = [
        "<task-notification>",
        f"<task-id>{escape(item['id'])}</task-id>",
        f"<status>{escape(item['status'])}</status>",
        f"<summary>{escape('Agent ' + item['description'] + ' ' + item['status'])}</summary>",
    ]
    if result_preview:
        parts.append(f"<result_preview>{escape(result_preview)}</result_preview>")
    parts.extend(
        [
            "<usage>",
            f"  <tool_uses>{int(item.get('tool_steps', 0))}</tool_uses>",
            f"  <attempts>{int(item.get('attempts', 0))}</attempts>",
            f"  <duration_ms>{int(item.get('duration_ms', 0))}</duration_ms>",
            "</usage>",
        ]
    )
    artifact_rows = []
    for key in ("report_path", "trace_path", "session_event_path"):
        value = str(item.get(key, "")).strip()
        if value:
            artifact_rows.append(f"  <{key}>{escape(value)}</{key}>")
    tool_error_codes = [str(code).strip() for code in item.get("tool_error_codes", []) if str(code).strip()]
    if artifact_rows or tool_error_codes:
        parts.append("<artifacts>")
        parts.extend(artifact_rows)
        if tool_error_codes:
            parts.append(f"  <tool_error_codes>{escape(','.join(tool_error_codes))}</tool_error_codes>")
        parts.append("</artifacts>")
    parts.append("</task-notification>")
    return "\n".join(parts)
