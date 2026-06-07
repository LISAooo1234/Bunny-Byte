"""Session state helpers shared by the runtime lifecycle."""

from ..features import memory as memorylib
from .session_topics import (
    DEFAULT_SESSION_TOPIC,
    derive_session_topic,
    normalize_session_topic,
    topic_from_history,
)


class SessionStateMixin:
    def _ensure_session_shape(self):
        self.session.setdefault("history", [])
        self.session.setdefault("memory", memorylib.default_memory_state())
        checkpoints = self.session.setdefault("checkpoints", {})
        if not isinstance(checkpoints, dict):
            checkpoints = {}
            self.session["checkpoints"] = checkpoints
        checkpoints.setdefault("current_id", "")
        checkpoints.setdefault("items", {})
        runtime_identity = self.session.setdefault("runtime_identity", {})
        if not isinstance(runtime_identity, dict):
            self.session["runtime_identity"] = {}
        resume_state = self.session.setdefault("resume_state", {})
        if not isinstance(resume_state, dict):
            self.session["resume_state"] = {}
        runtime_mode = self.session.setdefault("runtime_mode", {"mode": "default"})
        if not isinstance(runtime_mode, dict):
            self.session["runtime_mode"] = {"mode": "default"}
        read_ledger = self.session.setdefault("read_ledger", {})
        if not isinstance(read_ledger, dict):
            self.session["read_ledger"] = {}
        topic = str(self.session.get("topic", "") or "").strip()
        if not topic:
            self.session["topic"] = topic_from_history(self.session.get("history", []))

    @property
    def session_topic(self):
        return str(self.session.get("topic", "") or DEFAULT_SESSION_TOPIC).strip()

    def set_session_topic(self, topic):
        normalized = normalize_session_topic(topic)
        if not normalized:
            raise ValueError("topic must not be empty")
        self.ensure_session_started()
        self.session["topic"] = normalized
        self.session_path = self.session_store.save(self.session)
        self.session_event_bus.emit("session_topic_changed", {"topic": normalized})
        return normalized

    def _maybe_update_session_topic(self, user_message):
        current = str(self.session.get("topic", "") or "").strip()
        if current and current != DEFAULT_SESSION_TOPIC:
            return current
        topic = derive_session_topic(user_message)
        if not topic:
            return current or DEFAULT_SESSION_TOPIC
        self.session["topic"] = topic
        event_bus = getattr(self, "session_event_bus", None)
        if event_bus is not None and getattr(self, "_emit_derived_topic_event", False):
            self._emit_derived_topic_event = False
            event_bus.emit(
                "session_topic_changed",
                {"topic": topic, "source": "first_user_message"},
            )
        return topic

    @property
    def is_pending_session(self):
        return bool(getattr(self, "_lazy_session_requested", False))

    def ensure_session_started(self):
        pending = self.is_pending_session
        if pending:
            self._lazy_session_requested = False
            self.session_store.save(self.session)
        if not getattr(self, "_session_started_emitted", False):
            if pending and getattr(self.session_event_bus, "defer", False):
                self.session_event_bus.defer = False
            self.session_event_bus.emit(
                "session_started", {"workspace_root": self.workspace.repo_root}
            )
            self._session_started_emitted = True
        if pending:
            activate = getattr(self.session_event_bus, "activate", None)
            if callable(activate):
                activate()
        self.session_path = self.session_store.save(self.session)
        return self.session["id"]
