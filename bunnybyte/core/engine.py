"""Turn-level runtime engine shell."""

import re

from .engine_turn import run_turn as run_turn_impl
from .workspace import clip, now

NON_TERMINAL_FINAL_CUE_PATTERN = re.compile(
    r"(?:^|[\n。！？])\s*(?:"
    r"(?:我(?:接下来|随后|之后|然后|马上)?(?:会|将|准备|打算|先)|"
    r"接下来我(?:会|将|准备|打算)|"
    r"下一步我(?:会|将|准备|打算)|"
    r"继续(?:做|执行|读取|查看|检查)|"
    r"(?:now|next)\s+i\s+(?:will|am going to)|"
    r"i(?:'ll|\s+will|\s+am going to))"
    r")",
    re.IGNORECASE,
)


class Engine:
    def __init__(self, runtime):
        self.runtime = runtime

    def ask(self, user_message):
        final_answer = ""
        for event in self.run_turn(user_message):
            if event["type"] in {"final", "stop"}:
                final_answer = event["content"]
        return final_answer

    def drain_worker_notifications(self):
        agent = self.runtime
        notifications = agent.worker_manager.drain_notifications()
        for notification in notifications:
            agent.record({"role": "user", "content": notification, "created_at": now()})
            agent.session_event_bus.emit(
                "worker_notification_drained",
                {
                    "run_id": getattr(agent, "current_run_id", ""),
                    "content": clip(notification, 500),
                },
            )
        return notifications

    def _drain_worker_notification_events(self):
        for notification in self.drain_worker_notifications():
            yield {
                "type": "worker_notification",
                "run_id": getattr(self.runtime, "current_run_id", ""),
                "content": notification,
            }

    def run_turn(self, user_message):
        yield from run_turn_impl(self, user_message)

    @staticmethod
    def _looks_like_premature_final(final, task_state):
        text = str(final or "").strip()
        if not text or task_state.tool_steps > 0:
            return False
        return bool(NON_TERMINAL_FINAL_CUE_PATTERN.search(text))

    @staticmethod
    def _premature_final_notice(final):
        return (
            "Your previous assistant answer said you were about to do more work, "
            "but a final answer ends the turn. If work remains, call the appropriate "
            "tool now instead of narrating the plan. Previous answer: "
            f"{clip(final, 500)}"
        )
