"""Transcript turn indexing and recall helpers."""

from __future__ import annotations

import json
import re
from collections import Counter, OrderedDict

from ..providers.base import complete_model
from .workspace import clip, now

DEFAULT_RECALL_LIMIT = 4
DEFAULT_RECALL_BUDGET = 4_000
TURN_KEYWORD_LIMIT = 5
TURN_INDEX_MODEL_MAX_OUTPUT_TOKENS = 512
TURN_INDEX_MODEL_PROMPT = """CRITICAL: Respond with one compact JSON object only. Do not call tools.

Summarize this BunnyByte turn for future transcript retrieval. Produce:
- keywords: 4 to 5 short search keywords or phrases
- summary: one short sentence
- entities: important file paths, commands, functions, modules, or named concepts

Keep secrets out. Prefer stable technical nouns over generic words.

Local fallback index:
{local_index}

Turn transcript:
{turn_text}

Return JSON like:
{{"keywords":["..."],"summary":"...","entities":["..."]}}
"""

_IDENT_RE = re.compile(r"[A-Za-z0-9_./:-]+")
_CJK_RE = re.compile(r"[\u3400-\u9fff]{2,}")
_PATH_RE = re.compile(r"(?:[\w.-]+/)+[\w./-]+|[\w.-]+\.(?:py|js|ts|tsx|jsx|md|json|toml|yaml|yml|txt|sh)")
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "be",
    "but",
    "by",
    "for",
    "from",
    "i",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "with",
    "you",
    "your",
    "user",
    "assistant",
    "tool",
}


def tokenize(text):
    tokens = []
    value = str(text or "")
    for match in _IDENT_RE.finditer(value):
        raw = match.group(0).strip(".,;:()[]{}<>\"'`").lower()
        if not raw or raw in _STOPWORDS:
            continue
        tokens.append(raw)
        for part in re.split(r"[./:_-]+", raw):
            if len(part) > 1 and part not in _STOPWORDS:
                tokens.append(part)
    for seq in _CJK_RE.findall(value):
        tokens.append(seq)
        tokens.extend(seq[index : index + 2] for index in range(0, max(0, len(seq) - 1)))
        tokens.extend(seq[index : index + 3] for index in range(0, max(0, len(seq) - 2)))
    return tokens


def prompt_visible_event_ids(agent):
    builder = getattr(agent, "compact_manager", None)
    if builder is not None and hasattr(builder, "prompt_history"):
        history = builder.prompt_history()
    else:
        history = getattr(agent, "session", {}).get("history", [])
    return {str(item.get("event_id", "")) for item in history if item.get("event_id")}


def build_turn_index(history, turn_id):
    turn_id = str(turn_id or "").strip()
    items = [item for item in history if str(item.get("turn_id", "")) == turn_id]
    if not turn_id or not items:
        return {}
    texts = [_item_text(item) for item in items]
    combined = "\n".join(texts)
    entities = _extract_entities(items, combined)
    keywords = _extract_keywords(combined, entities, limit=TURN_KEYWORD_LIMIT)
    return {
        "turn_id": turn_id,
        "created_at": str(items[0].get("created_at", "")),
        "updated_at": str(items[-1].get("created_at", "")),
        "indexed_at": now(),
        "item_count": len(items),
        "event_ids": [str(item.get("event_id", "")) for item in items if item.get("event_id")],
        "keywords": keywords,
        "summary": _summarize_turn(items),
        "entities": entities[:12],
    }


def update_turn_index(session, turn_id):
    index = build_turn_index(session.get("history", []), turn_id)
    if not index:
        return {}
    session.setdefault("turn_index", {})[str(turn_id)] = index
    order = session.setdefault("turn_index_order", [])
    if str(turn_id) not in order:
        order.append(str(turn_id))
    return index


def refine_turn_index_with_model(model_client, history, turn_id, index, max_new_tokens=None):
    items = [item for item in history if str(item.get("turn_id", "")) == str(turn_id)]
    if not items or not index:
        return index
    prompt = TURN_INDEX_MODEL_PROMPT.format(
        local_index=json.dumps(_public_index(index), ensure_ascii=False, sort_keys=True),
        turn_text=clip("\n".join(_item_text(item) for item in items), 8_000),
    )
    output_tokens = TURN_INDEX_MODEL_MAX_OUTPUT_TOKENS
    if max_new_tokens is not None:
        output_tokens = min(TURN_INDEX_MODEL_MAX_OUTPUT_TOKENS, max(64, int(max_new_tokens)))
    result = complete_model(model_client, prompt, output_tokens)
    parsed = _parse_model_index(result.text)
    if not parsed:
        return index
    refined = dict(index)
    local_keywords = list(index.get("keywords", []) or [])
    model_keywords = _clean_list(parsed.get("keywords", []), limit=TURN_KEYWORD_LIMIT)
    refined["keywords"] = _dedupe([*model_keywords, *local_keywords])[:TURN_KEYWORD_LIMIT]
    summary = str(parsed.get("summary", "")).strip()
    if summary:
        refined["summary"] = clip(summary, 240)
    refined["entities"] = _dedupe(
        [
            *_clean_list(parsed.get("entities", []), limit=12),
            *list(index.get("entities", []) or []),
        ]
    )[:12]
    refined["indexer"] = "model_refined_v1"
    return refined


def retrieve_transcript_snippets(
    query,
    full_history,
    visible_event_ids=None,
    turn_index=None,
    limit=DEFAULT_RECALL_LIMIT,
    char_budget=DEFAULT_RECALL_BUDGET,
):
    query_tokens = Counter(tokenize(query))
    if not query_tokens:
        return []
    visible = {str(event_id) for event_id in (visible_event_ids or set()) if str(event_id)}
    turns = _group_turns(full_history)
    indexed = turn_index if isinstance(turn_index, dict) else {}
    ranked = []
    for turn_id, items in turns.items():
        hidden_items = [item for item in items if str(item.get("event_id", "")) not in visible]
        if not hidden_items:
            continue
        hidden_text = "\n".join(_item_text(item) for item in hidden_items)
        meta = indexed.get(turn_id, {}) if isinstance(indexed.get(turn_id, {}), dict) else {}
        score = _score_turn(query, query_tokens, hidden_text, meta)
        if score <= 0:
            continue
        ranked.append((score, turn_id, hidden_items, meta))
    ranked.sort(key=lambda item: (item[0], _turn_recency(item[2])), reverse=True)
    return _budgeted_snippets(ranked[: max(0, int(limit))], max(0, int(char_budget)))


def render_prompt_recall(snippets, budget=DEFAULT_RECALL_BUDGET):
    lines = [
        "Recovered transcript snippets:",
        "These are older hidden session transcript matches. Treat them as prior conversation evidence, not current filesystem state.",
    ]
    if not snippets:
        lines.append("- none")
        return "\n".join(lines)
    for snippet in snippets:
        source = ""
        if snippet.get("source_session_id"):
            source = f" session={snippet.get('source_session_id')}"
            if snippet.get("source_session_topic"):
                source += f" topic={snippet.get('source_session_topic')}"
        lines.extend(
            [
                f"- turn_id={snippet['turn_id']}{source} events={','.join(snippet['event_ids'][:5])}",
                f"  keywords={', '.join(snippet.get('keywords', [])) or '-'}",
                f"  summary={snippet.get('summary') or '-'}",
            ]
        )
        for line in str(snippet.get("text", "")).splitlines():
            lines.append(f"  {line}")
    rendered = "\n".join(lines)
    return _hard_clip(rendered, max(0, int(budget)))


def render_cli_recall(snippets):
    if not snippets:
        return "## Transcript Recall\n\nNo matching transcript snippets."
    lines = ["## Transcript Recall", ""]
    for index, snippet in enumerate(snippets, start=1):
        lines.extend(
            [
                f"### {index}. {snippet.get('source_session_id', 'current')} / {snippet['turn_id']}",
                "",
                f"- score: {snippet['score']}",
                f"- session: {snippet.get('source_session_id', 'current')}",
                f"- topic: {snippet.get('source_session_topic', '-') or '-'}",
                f"- events: {', '.join(snippet['event_ids']) or '-'}",
                f"- keywords: {', '.join(snippet.get('keywords', [])) or '-'}",
                f"- summary: {snippet.get('summary') or '-'}",
                "",
                "```text",
                str(snippet.get("text", "")).strip(),
                "```",
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def _item_text(item):
    role = str(item.get("role", "")).strip()
    if role == "tool":
        args = item.get("args", {}) or {}
        arg_text = json.dumps(args, ensure_ascii=False, sort_keys=True)
        return f"[tool:{item.get('name', '')}] {arg_text}\n{item.get('content', '')}"
    return f"[{role}] {item.get('content', '')}"


def _extract_entities(items, text):
    entities = []
    for item in items:
        args = item.get("args", {}) or {}
        for key in ("path", "file", "cwd"):
            value = str(args.get(key, "")).strip()
            if value:
                entities.append(value)
    entities.extend(match.group(0) for match in _PATH_RE.finditer(text))
    return _dedupe(entities)


def _extract_keywords(text, entities, limit):
    scores = Counter()
    for token in tokenize(text):
        if len(token) == 1 and not _CJK_RE.fullmatch(token):
            continue
        scores[token] += 1
    for entity in entities:
        for token in tokenize(entity):
            scores[token] += 3
    ordered = [token for token, _ in scores.most_common(max(1, int(limit) * 3))]
    return _dedupe(ordered)[: max(1, int(limit))]


def _summarize_turn(items):
    user = next((str(item.get("content", "")).strip() for item in items if item.get("role") == "user"), "")
    assistant = next((str(item.get("content", "")).strip() for item in reversed(items) if item.get("role") == "assistant"), "")
    parts = [part for part in (user, assistant) if part]
    return clip(" | ".join(parts), 240)


def _score_turn(query, query_tokens, hidden_text, meta):
    text_lower = hidden_text.lower()
    score = 0
    if str(query).strip().lower() in text_lower:
        score += 25
    content_tokens = Counter(tokenize(hidden_text))
    score += sum(min(count, content_tokens.get(token, 0)) for token, count in query_tokens.items()) * 2
    meta_tokens = Counter()
    for key in ("keywords", "entities"):
        for value in meta.get(key, []) or []:
            meta_tokens.update(tokenize(value))
    meta_tokens.update(tokenize(meta.get("summary", "")))
    score += sum(min(count, meta_tokens.get(token, 0)) for token, count in query_tokens.items()) * 7
    return score


def _parse_model_index(text):
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return {}
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def _clean_list(values, limit):
    if not isinstance(values, list):
        return []
    return _dedupe(str(value).strip() for value in values if str(value).strip())[:limit]


def _public_index(index):
    return {
        "keywords": list(index.get("keywords", []) or []),
        "summary": str(index.get("summary", "") or ""),
        "entities": list(index.get("entities", []) or []),
    }


def _budgeted_snippets(ranked, char_budget):
    snippets = []
    if char_budget <= 0:
        return snippets
    remaining = char_budget
    for score, turn_id, items, meta in ranked:
        text = _render_items(items)
        allowance = max(240, remaining // max(1, len(ranked) - len(snippets)))
        text = clip(text, allowance)
        event_ids = [str(item.get("event_id", "")) for item in items if item.get("event_id")]
        snippet = {
            "turn_id": turn_id,
            "score": int(score),
            "event_ids": event_ids,
            "keywords": list(meta.get("keywords", []) or [])[:TURN_KEYWORD_LIMIT],
            "summary": str(meta.get("summary", "") or _summarize_turn(items)),
            "created_at": str(items[0].get("created_at", "")) if items else "",
            "text": text,
        }
        cost = len(render_prompt_recall([snippet], budget=char_budget))
        if cost > remaining and snippets:
            break
        snippets.append(snippet)
        remaining = max(0, remaining - cost)
        if remaining <= 120:
            break
    return snippets


def _render_items(items):
    lines = []
    for item in items:
        event_id = str(item.get("event_id", ""))
        role = str(item.get("role", ""))
        if role == "tool":
            args = json.dumps(item.get("args", {}) or {}, ensure_ascii=False, sort_keys=True)
            lines.append(f"[tool:{item.get('name', '')} {event_id}] {args}")
            lines.append(clip(str(item.get("content", "")), 700))
        else:
            lines.append(f"[{role} {event_id}] {clip(str(item.get('content', '')), 900)}")
    return "\n".join(lines)


def _group_turns(history):
    turns = OrderedDict()
    for item in history:
        turn_id = str(item.get("turn_id") or "legacy")
        turns.setdefault(turn_id, []).append(item)
    return turns


def _turn_recency(items):
    if not items:
        return ""
    return str(items[-1].get("created_at", ""))


def _dedupe(values):
    seen = set()
    result = []
    for value in values:
        text = str(value or "").strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _hard_clip(text, limit):
    text = str(text or "")
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."
