"""Session-level event bus.

The run trace is per-task and diagnostic. The session event bus is the durable,
coarse-grained timeline for the interactive session itself.
"""

import json
from pathlib import Path

from .workspace import now


class SessionEventBus:
    def __init__(self, session_id, path, redact=None, defer=False):
        self.session_id = str(session_id)
        self.path = Path(path)
        self.redact = redact or (lambda value: value)
        self.defer = bool(defer)
        self._buffer = []
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event, payload=None):
        record = dict(payload or {})
        record["event"] = str(event)
        record["session_id"] = self.session_id
        record["created_at"] = now()
        record = self.redact(record)
        if self.defer:
            self._buffer.append(record)
            return record
        self._write(record)
        return record

    def activate(self):
        self.defer = False
        buffered = list(self._buffer)
        self._buffer.clear()
        for record in buffered:
            self._write(record)

    def _write(self, record):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, sort_keys=True) + "\n")
