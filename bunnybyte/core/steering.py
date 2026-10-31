"""Mid-turn steering: text the user types while a turn runs reaches the model at its next call.

The TUI puts messages into the runtime's `SteerQueue`; the turn loop drains it
before every model call and once more before it accepts a final answer. A
drained message becomes a real user history item (`source: "steer"`, so the
transcript fence and compaction count it as user input), is appended to the
task's request (report.json, critic), and is repeated under the current request
in every later prompt of the turn. Messages only arrive between model calls: a
running shell command or model request is not interrupted.
"""

import threading

from .workspace import clip, now

STEER_SOURCE = "steer"
PROMPT_LIMIT = 5  # 当前请求区不受预算裁剪：最多重复最近 5 条，每条 1000 字
PROMPT_CHARS = 1000
FREE_FINAL_ROUNDS = 5  # 交卷时收到插话而多出的模型调用，最多这么多次不计入重试预算
PROMPT_HEADER = (
    "Follow-up messages the user sent while you were working on this request "
    "(newest last; where they conflict with the request above, they take priority):"
)


class SteerQueue:
    def __init__(self):
        self._lock = threading.Lock()
        self._items = []
        self._next_id = 0

    def put(self, text):
        text = str(text or "").strip()
        if not text:
            return 0
        with self._lock:
            self._next_id += 1
            self._items.append((self._next_id, text))
            return self._next_id

    def take_all(self):
        with self._lock:
            items, self._items = self._items, []
            return items


class TurnSteering:
    """Per-turn view of the queue: what was delivered and how the prompt repeats it."""

    def __init__(self, engine, task_state):
        self.engine = engine
        self.task_state = task_state
        self.delivered = []
        self.final_rounds = 0

    @property
    def agent(self):
        return self.engine.runtime

    def drain(self):
        """Generator: record queued messages as user turns. Returns how many arrived."""
        items = self._take()
        if items:
            yield from self._deliver(items)
        return len(items)

    def at_final(self, final):
        """Generator: True when messages arrived as the model was about to finish, so the loop must go on.

        The answer it was about to give is kept in history (before the new messages)
        and shown, so the model knows what it already said and the user sees it.
        """
        items = self._take()
        if not items:
            return False
        self.agent.record({"role": "assistant", "content": final, "created_at": now()})
        yield {"type": "assistant_preamble", "run_id": self.task_state.run_id, "content": final}
        yield from self._deliver(items)
        self.final_rounds += 1
        return True

    def _take(self):
        queue = getattr(self.agent, "steer_queue", None)
        return queue.take_all() if queue is not None else []

    def _deliver(self, items):
        agent, task_state = self.agent, self.task_state
        for _, text in items:
            agent.record({"role": "user", "content": text, "created_at": now(), "source": STEER_SOURCE})
            task_state.user_request = f"{task_state.user_request}\n\n用户追加：{text}"
            self.delivered.append(text)
        ids = [steer_id for steer_id, _ in items]
        texts = [text for _, text in items]
        agent.session_event_bus.emit(
            "user_steer", {"run_id": task_state.run_id, "ids": ids, "content": clip("\n".join(texts), 500)}
        )
        agent.emit_trace(task_state, "user_steer", {"ids": ids, "messages": [clip(text, 300) for text in texts]})
        yield {"type": "steer_delivered", "run_id": task_state.run_id, "ids": ids, "content": "\n".join(texts)}

    def free_round(self):
        return self.final_rounds < FREE_FINAL_ROUNDS

    def request_text(self, user_message):
        if not self.delivered:
            return user_message
        shown = self.delivered[-PROMPT_LIMIT:]
        lines = [user_message, "", PROMPT_HEADER]
        if len(self.delivered) > len(shown):
            lines.append(f"- ({len(self.delivered) - len(shown)} earlier follow-up messages are in the transcript)")
        for text in shown:
            body = (text if len(text) <= PROMPT_CHARS else text[: PROMPT_CHARS - 3] + "...").splitlines() or [""]
            lines.append(f"- {body[0]}")
            lines.extend(f"  {line}" for line in body[1:])
        return "\n".join(lines)
