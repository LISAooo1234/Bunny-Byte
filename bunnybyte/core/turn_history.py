"""Turn-aware transcript rendering."""

from .turn_history_support import TurnHistoryRenderer, tail_clip

__all__ = ["TurnHistoryBuilder", "tail_clip"]


class TurnHistoryBuilder:
    def __init__(self, agent):
        self.agent = agent
        self.renderer = TurnHistoryRenderer(agent)

    def enrich(self, item):
        item = dict(item)
        if not item.get("turn_id"):
            current_turn = str(getattr(self.agent, "current_turn_id", "") or "")
            if not current_turn:
                if item.get("role") == "user" or not self.agent.session.get("_manual_turn_id"):
                    self.agent.session["_manual_turn_seq"] = int(self.agent.session.get("_manual_turn_seq", 0)) + 1
                    self.agent.session["_manual_turn_id"] = f"manual_{self.agent.session['_manual_turn_seq']:06d}"
                current_turn = str(self.agent.session.get("_manual_turn_id", "legacy"))
            item["turn_id"] = current_turn
        if not item.get("run_id"):
            item["run_id"] = str(getattr(self.agent, "current_run_id", "") or "")
        if not item.get("event_id"):
            self.agent.session["_event_seq"] = int(self.agent.session.get("_event_seq", 0)) + 1
            item["event_id"] = f"event_{self.agent.session['_event_seq']:06d}"
        item.setdefault("source", "runtime")
        return item

    def prompt_history(self):
        compact_manager = getattr(self.agent, "compact_manager", None)
        if compact_manager is not None and hasattr(compact_manager, "prompt_history"):
            return list(compact_manager.prompt_history())
        return list(getattr(self.agent, "session", {}).get("history", []))

    def raw_text(self, history):
        return self.renderer.raw_text(history)

    def render_section(self, budget, stable_budget=None):
        return self.renderer.render_section(self.prompt_history(), budget, stable_budget=stable_budget)
