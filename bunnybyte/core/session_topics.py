"""Session topic helpers."""

import re


DEFAULT_SESSION_TOPIC = "Untitled session"
MAX_SESSION_TOPIC_CHARS = 72


def normalize_session_topic(value, limit=MAX_SESSION_TOPIC_CHARS):
    text = _clean_text(value)
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "..."


def derive_session_topic(value, limit=MAX_SESSION_TOPIC_CHARS):
    text = _clean_text(value)
    if not text or _is_synthetic_worker_prompt(text):
        return ""
    first_line = _first_meaningful_line(text)
    sentence = _first_sentence(first_line)
    return normalize_session_topic(sentence or first_line or text, limit=limit)


def topic_from_history(history):
    for item in history or []:
        if item.get("role") == "user":
            topic = derive_session_topic(item.get("content", ""))
            if topic:
                return topic
    return DEFAULT_SESSION_TOPIC


def _clean_text(value):
    text = str(value or "").strip()
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip("\"'` ")


def _is_synthetic_worker_prompt(text):
    lowered = str(text or "").lower()
    return (
        ("worker 子代理" in text and "你的任务" in text)
        or ("bunnybyte" in lowered and "worker" in lowered and "your task" in lowered)
        or ("subagent" in lowered and "your task" in lowered and "bunnybyte" in lowered)
    )


def _first_meaningful_line(text):
    for line in str(text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return str(text or "").strip()


def _first_sentence(text):
    match = re.search(r"(.+?[。！？!?])(?:\s|$)", str(text or ""))
    if match:
        return match.group(1).strip()
    return str(text or "").strip()
