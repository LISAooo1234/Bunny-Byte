"""Step accounting and escalation for tool replies that carry no new information.

A cheap model sometimes re-issues a call whose answer is a stub ("range already read
and unchanged", "repeated identical call"). Such replies are not charged to `max_steps`.
A model that keeps producing them is first denied the repeated read-only tool for a
couple of calls, and if it still loops it is asked to wrap up without tools.
"""

from .engine_helpers import request_step_limit_summary

WASTE_CODES = frozenset({"file_range_already_read", "repeated_identical_call"})
QUARANTINABLE_TOOLS = frozenset({"read_file", "list_files", "search"})

TRIP_TOTAL = 8
TRIP_WINDOW = 8
TRIP_WINDOW_HITS = 5
QUARANTINE_CALLS = 2
STOP_AFTER_QUARANTINE = 4
QUARANTINE_PROBATION = 8

QUARANTINE = "quarantine"
STOP = "stop"

REPEAT_NOTICE = (
    "Your recent tool calls kept repeating requests that returned nothing new. Do not "
    "repeat them. Work from the information you already have: make the edit, run a "
    "command, or return your final answer."
)
QUARANTINE_NOTICE = (
    "Your recent tool calls kept repeating requests that returned nothing new. "
    "{tools} will not be offered for your next {calls} steps. Work from the information "
    "you already have: make the edit, run a command, or return your final answer."
)
STALL_SUMMARY_NOTICE = (
    "Your tool calls kept repeating requests that returned nothing new, so tools are now "
    "disabled for this turn. Do not call any tool. Right now, answer in the user's "
    "language: (1) what you have done so far, (2) what is still unfinished or unverified "
    "(say plainly if the task is not complete), (3) how the user can continue."
)
STALL_STOP_SUFFIX = (
    "\n\n— 检测到模型反复发出没有新信息的重复调用，已提前结束本轮，任务可能尚未完成。"
    "在 REPL 输入 /resume 续接本会话，或换个说法让我继续。"
)
STALL_STOP_TEXT = (
    "Stopped because the model kept repeating tool calls that returned nothing new; "
    "the task may be unfinished."
)


class StallBudget:
    """Per-turn bookkeeping; progress (a workspace change) clears the escalation state."""

    def __init__(self, max_steps):
        self.refund_cap = max(0, int(max_steps)) // 2
        self.refunded = 0
        self.last_refund = 0
        self.wasted_total = 0
        self.round_wasted = 0
        self.recent = []
        self.quarantine = set()
        self.warn_left = 0
        self.quarantined = False
        self.probation = []

    def observe(self, name, metadata):
        metadata = metadata or {}
        if metadata.get("workspace_changed"):
            self._progress()
            return
        wasted = metadata.get("tool_error_code") in WASTE_CODES
        self.recent.append((str(name), wasted))
        if wasted:
            self.wasted_total += 1
            self.round_wasted += 1
        if self.quarantined:
            self.probation.append(wasted)

    def charge(self, executed):
        """Steps to charge for a round of `executed` tool calls, after the refund."""
        refund = max(0, min(self.round_wasted, executed, self.refund_cap - self.refunded))
        self.refunded += refund
        self.last_refund = refund
        self.round_wasted = 0
        return executed - refund

    def assess(self):
        """Escalate after a tool round: '' to carry on, QUARANTINE once, then STOP."""
        if self.quarantined:
            if sum(self.probation) >= STOP_AFTER_QUARANTINE:
                return STOP
            if len(self.probation) >= QUARANTINE_PROBATION:
                self._progress()  # the model got through probation without looping again
            return ""
        if not self._tripped():
            return ""
        self.quarantined = True
        self.probation = []
        recent_waste = [name for name, wasted in self.recent[-TRIP_WINDOW:] if wasted]
        names = recent_waste or [name for name, wasted in self.recent if wasted]
        self.quarantine = {name for name in names if name in QUARANTINABLE_TOOLS}
        self.warn_left = QUARANTINE_CALLS
        return QUARANTINE

    def next_call(self, native_tools):
        """Tools to offer on the next model request plus an optional prompt notice."""
        if self.warn_left <= 0:
            return native_tools, ""
        active = sorted(self.quarantine)
        if not active:
            return native_tools, REPEAT_NOTICE
        kept = native_tools
        if native_tools is not None:
            kept = [spec for spec in native_tools if spec.get("name") not in active]
        return kept, QUARANTINE_NOTICE.format(tools=", ".join(active), calls=QUARANTINE_CALLS)

    def call_done(self):
        """A model request completed; failed requests and provider retries do not count."""
        self.warn_left = max(0, self.warn_left - 1)

    def snapshot(self):
        return {
            "wasted_total": self.wasted_total,
            "refunded": self.refunded,
            "quarantined_tools": sorted(self.quarantine),
            "wasted_after_quarantine": sum(self.probation),
        }

    def _progress(self):
        self.recent.clear()
        self.quarantine = set()
        self.warn_left = 0
        self.quarantined = False
        self.probation = []

    def _tripped(self):
        flags = [wasted for _name, wasted in self.recent]
        return sum(flags) >= TRIP_TOTAL or sum(flags[-TRIP_WINDOW:]) >= TRIP_WINDOW_HITS


def watch_tool_events(events, stall):
    """Pass tool events through untouched while the stall budget observes their results."""
    try:
        while True:
            try:
                event = next(events)
            except StopIteration as done:
                return done.value
            if event.get("type") == "tool_result":
                stall.observe(event.get("name", ""), event.get("metadata"))
            yield event
    finally:
        events.close()


def finish_stalled(engine, task_state, user_message):
    """Ask the model, without tools, to report where it got to; returns the final text."""
    summary = request_step_limit_summary(
        engine, task_state, user_message, notice=STALL_SUMMARY_NOTICE, event="stall_summary"
    )
    final = summary + STALL_STOP_SUFFIX if summary else STALL_STOP_TEXT
    task_state.stop_loop_stalled(final)
    return final
