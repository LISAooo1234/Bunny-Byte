"""Non-destructive session compaction for model-facing context."""

from .compact_support import build_summary, model_summary_text, new_compaction_id, summary_item
from .transcript_fence import USER_MESSAGE_CHARS, collect_user_messages

MAX_STORED_USER_MESSAGES = 60


class CompactManager:
    def __init__(self, agent):
        self.agent = agent

    def compact(self, trigger="manual", keep_recent_turns=3):
        history = list(self.agent.session.get("history", []))
        groups = self._group(history)
        if len(groups) <= keep_recent_turns:
            summary = build_summary(trigger, history, history, "")
            self.agent.session_event_bus.emit("compaction_created", summary)
            return summary

        compacted_turns, kept_turns = self._split_groups_for_compaction(groups, keep_recent_turns)
        compacted_items = [item for _, items in compacted_turns for item in items]
        kept_items = [item for _, items in kept_turns for item in items]
        if not compacted_items:
            summary = build_summary(trigger, history, self.prompt_history(), "")
            self.agent.session_event_bus.emit("compaction_created", summary)
            return summary
        summary_text = model_summary_text(self.agent, compacted_items, self._group)
        previous = (self.active_compaction() or {}).get("user_messages") or []
        user_messages = [text[:USER_MESSAGE_CHARS] for text in collect_user_messages(compacted_items, previous)]
        user_messages = user_messages[-MAX_STORED_USER_MESSAGES:]
        compaction_id = new_compaction_id(self.agent.session)
        summary = summary_item(compaction_id, summary_text, user_messages=user_messages)
        preserved_event_ids = [str(item.get("event_id", "")) for item in kept_items if item.get("event_id")]
        summarized_event_ids = [str(item.get("event_id", "")) for item in compacted_items if item.get("event_id")]
        view_items = [summary, *kept_items]
        payload = build_summary(trigger, history, view_items, summary_text)
        payload.update(
            {
                "id": compaction_id,
                "strategy": "model_summary_v1",
                "summary": summary_text,
                "user_messages": user_messages,
                "summary_event_id": summary["event_id"],
                "summarized_event_ids": summarized_event_ids,
                "preserved_event_ids": preserved_event_ids,
                "preserved_from_event_id": preserved_event_ids[0] if preserved_event_ids else "",
                "last_summarized_event_id": summarized_event_ids[-1] if summarized_event_ids else "",
            }
        )
        self.agent.session.setdefault("compactions", []).append(payload)
        self.agent.session["context_view"] = {
            "active_compaction_id": compaction_id,
            "summary_event_id": summary["event_id"],
            "preserved_from_event_id": payload["preserved_from_event_id"],
            "last_summarized_event_id": payload["last_summarized_event_id"],
        }
        self.agent.session_path = self.agent.session_store.save(self.agent.session)
        self.agent.session_event_bus.emit(
            "compaction_created",
            {key: value for key, value in payload.items() if key not in {"summary", "user_messages"}},
        )
        if self.agent.current_task_state:
            self.agent.emit_trace(
                self.agent.current_task_state,
                "compaction_started",
                {"trigger": trigger, "pre_tokens": payload["pre_tokens"]},
            )
            self.agent.emit_trace(
                self.agent.current_task_state,
                "compaction_finished",
                {key: value for key, value in payload.items() if key not in {"summary", "user_messages"}},
            )
        return payload

    def prompt_history(self):
        history = list(self.agent.session.get("history", []))
        compaction = self.active_compaction()
        if not compaction:
            return history
        summary_event = summary_item(
            str(compaction.get("id", "compact")),
            str(compaction.get("summary", "")).strip(),
            event_id=str(compaction.get("summary_event_id", "")) or None,
            user_messages=compaction.get("user_messages") or (),
        )
        preserved_ids = {str(event_id) for event_id in compaction.get("preserved_event_ids", []) if str(event_id)}
        last_summarized = str(compaction.get("last_summarized_event_id", ""))
        preserved = []
        include_after_boundary = not preserved_ids
        for history_item in history:
            event_id = str(history_item.get("event_id", ""))
            if event_id in preserved_ids:
                preserved.append(history_item)
                continue
            if include_after_boundary:
                preserved.append(history_item)
                continue
            if last_summarized and event_id == last_summarized:
                include_after_boundary = True
        return [summary_event, *preserved]

    def active_compaction(self):
        context_view = self.agent.session.get("context_view") or {}
        active_id = str(context_view.get("active_compaction_id", "") or "")
        if not active_id:
            return None
        for item in reversed(self.agent.session.get("compactions", [])):
            if str(item.get("id", "")) == active_id:
                return item
        return None

    def _split_groups_for_compaction(self, groups, keep_recent_turns):
        compaction = self.active_compaction()
        if compaction:
            last_summarized = str(compaction.get("last_summarized_event_id", ""))
            if last_summarized:
                start_index = 0
                for index, (_, items) in enumerate(groups):
                    if any(str(item.get("event_id", "")) == last_summarized for item in items):
                        start_index = index + 1
                        break
                groups = groups[start_index:]
        if len(groups) <= keep_recent_turns:
            return [], groups
        return groups[:-keep_recent_turns], groups[-keep_recent_turns:]

    @staticmethod
    def _group(history):
        groups = []
        by_id = {}
        for item in history:
            turn_id = str(item.get("turn_id") or "legacy")
            if turn_id not in by_id:
                by_id[turn_id] = []
                groups.append((turn_id, by_id[turn_id]))
            by_id[turn_id].append(item)
        return groups
