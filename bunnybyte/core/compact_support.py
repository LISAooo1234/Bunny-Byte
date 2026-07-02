"""Pure helpers for session compaction."""

import re

from ..providers.base import complete_model
from .context_usage import estimate_tokens
from .workspace import clip, now

COMPACT_MAX_OUTPUT_TOKENS = 4096
COMPACT_SUMMARY_FALLBACK_PREFIX = "Compacted session summary:"
COMPACT_SUMMARY_PROMPT = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

Your task is to create a detailed summary of the BunnyByte conversation so far. This summary will be placed at the start of a continuing coding-agent session; newer messages will follow after it. Preserve enough context for the agent to continue development work without losing important details.

Your summary should include these sections:

1. Primary Request and Intent: Capture the user's explicit requests and intents in detail.
2. Key Technical Concepts: List important technical concepts, technologies, files, commands, and runtime behavior discussed.
3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. Include why each file mattered and important snippets or function names when available.
4. Errors and fixes: List errors, failed approaches, test failures, and how they were fixed. Include user corrections and feedback.
5. Problem Solving: Document problems solved and ongoing troubleshooting.
6. All user messages: List all user messages from the summarized portion that are not tool results.
7. Pending Tasks: Outline tasks explicitly requested but not finished.
8. Current Work: Describe precisely what was being worked on immediately before this summary request.
9. Optional Next Step: If there is a next step directly tied to the most recent work, include it with direct quotes when possible.

Return only:
<summary>
...
</summary>

Conversation to summarize:
{transcript}
"""


def compact_output_tokens(max_new_tokens):
    if max_new_tokens is None:
        return None
    return max(COMPACT_MAX_OUTPUT_TOKENS, int(max_new_tokens or 0))


def build_summary(trigger, before, after, summary_text):
    pre_chars = sum(len(str(item.get("content", ""))) for item in before)
    post_chars = sum(len(str(item.get("content", ""))) for item in after)
    return {
        "trigger": str(trigger),
        "created_at": now(),
        "pre_tokens": estimate_tokens(pre_chars),
        "post_tokens": estimate_tokens(post_chars),
        "pre_items": len(before),
        "post_items": len(after),
        "summary_chars": len(summary_text),
    }


def model_summary_text(agent, items, group_items):
    prompt = COMPACT_SUMMARY_PROMPT.format(transcript=render_items_for_summary(items, group_items))
    try:
        result = complete_model(
            agent.model_client,
            prompt,
            compact_output_tokens(getattr(agent, "max_new_tokens", None)),
        )
        summary = extract_summary(result.text)
        if summary:
            return summary
    except Exception:
        return fallback_summary_text(items)
    return fallback_summary_text(items)


def extract_summary(text):
    text = str(text or "").strip()
    match = re.search(r"<summary>(.*?)</summary>", text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    text = re.sub(r"<analysis>.*?</analysis>", "", text, flags=re.DOTALL | re.IGNORECASE)
    return text.strip()


def render_items_for_summary(items, group_items):
    lines = []
    for turn_id, turn_items in group_items(items):
        lines.append(f"Turn {turn_id}:")
        for item in turn_items:
            role = str(item.get("role", ""))
            if role == "tool":
                lines.append(
                    f"[tool:{item.get('name', '')}] args={item.get('args', {})} result={clip(item.get('content', ''), 2000)}"
                )
            else:
                lines.append(f"[{role}] {clip(item.get('content', ''), 3000)}")
    return "\n".join(lines)


def fallback_summary_text(items):
    files_read = []
    files_modified = []
    user_requests = []
    assistant_notes = []
    for item in items:
        if item.get("role") == "user":
            user_requests.append(str(item.get("content", "")).strip())
        elif item.get("role") == "assistant":
            assistant_notes.append(str(item.get("content", "")).strip())
        elif item.get("role") == "tool":
            path = str(item.get("args", {}).get("path", "")).strip()
            if item.get("name") == "read_file" and path:
                files_read.append(path)
            if item.get("name") in {"write_file", "patch_file"} and path:
                files_modified.append(path)
    return "\n".join(
        [
            COMPACT_SUMMARY_FALLBACK_PREFIX,
            f"- Goal: {user_requests[-1] if user_requests else '-'}",
            "- Constraints and preferences: -",
            f"- Files read: {', '.join(sorted(set(files_read))) or '-'}",
            f"- Files modified: {', '.join(sorted(set(files_modified))) or '-'}",
            f"- Key decisions: {assistant_notes[-1] if assistant_notes else '-'}",
            f"- Current progress: compacted {len(items)} history items",
            "- Open blockers: -",
            "- Next step: continue from the latest preserved turn",
            "- Critical context: earlier turns were compacted; use preserved latest turns for exact wording",
        ]
    )


def summary_item(compaction_id, summary_text, event_id=None):
    return {
        "role": "system",
        "kind": "compact_summary",
        "content": "Compacted context summary:\n" + str(summary_text or "").strip(),
        "created_at": now(),
        "source": "compact",
        "turn_id": compaction_id,
        "run_id": "",
        "event_id": event_id or f"{compaction_id}_summary",
    }


def new_compaction_id(session):
    sequence = len(session.get("compactions", [])) + 1
    return f"compact_{now().replace(':', '').replace('-', '').replace('+', '_')}_{sequence:03d}"
