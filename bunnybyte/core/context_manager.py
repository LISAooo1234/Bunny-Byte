"""Prompt 组装与上下文预算控制。

这个模块负责决定：每一轮到底把多少 prefix、memory、相关笔记、历史
以及当前用户请求送进模型。
"""

from __future__ import annotations

from ..features import skills as skillslib
from .context_support import (
    SectionRender,
    build_context_metadata,
    per_note_budget,
    render_sections_without_reduction,
)
from .memory_scope import durable_memory_enabled, retrieve_scoped_session_snippets
from .prompt_layout import memory_sections
from .transcript_recall import prompt_visible_event_ids, render_prompt_recall, retrieve_transcript_snippets
from .turn_history import TurnHistoryBuilder, tail_clip
from .turn_history_support import empty_history_details

# 一轮里不变的在前（provider 前缀缓存能复用），每次调用都会变的运行状态（memory）放在历史之后
SECTION_ORDER = (
    "prefix", "skills", "memory_guide", "history", "relevant_memory", "transcript_recall", "memory", "current_request"
)
CURRENT_REQUEST_SECTION = "current_request"
_DEFAULT_SECTION_WEIGHTS = {
    "prefix": 0.18,
    "memory": 0.08,
    "memory_guide": 0.05,  # 记忆说明约 4k 字，加上 30 条 MEMORY.md 索引约 6.6k
    "skills": 0.08,
    "relevant_memory": 0.04,
    "transcript_recall": 0.04,
    "history": 0.53,
}
DEFAULT_CURRENT_REQUEST_RESERVE_RATIO = 0.12
DEFAULT_CURRENT_REQUEST_RESERVE_MAX = 24_000
DEFAULT_CURRENT_REQUEST_RESERVE_MIN = 2_000
_MIN_SECTION_BUDGET = 20


def _default_current_request_reserve(total_budget):
    total_budget = max(0, int(total_budget))
    desired = max(
        DEFAULT_CURRENT_REQUEST_RESERVE_MIN,
        int(total_budget * DEFAULT_CURRENT_REQUEST_RESERVE_RATIO),
    )
    reserve = min(DEFAULT_CURRENT_REQUEST_RESERVE_MAX, desired)
    max_reserve = max(0, total_budget - (_MIN_SECTION_BUDGET * len(_DEFAULT_SECTION_WEIGHTS)))
    return min(reserve, max_reserve)


def default_section_budgets(total_budget):
    total_budget = max(0, int(total_budget))
    allocatable = max(
        _MIN_SECTION_BUDGET * len(_DEFAULT_SECTION_WEIGHTS),
        total_budget - _default_current_request_reserve(total_budget),
    )
    budgets = {}
    remaining = allocatable
    for section, weight in _DEFAULT_SECTION_WEIGHTS.items():
        if section == "history":
            budgets[section] = remaining
            continue
        value = max(_MIN_SECTION_BUDGET, int(allocatable * weight))
        budgets[section] = value
        remaining -= value
    budgets["history"] = max(_MIN_SECTION_BUDGET, remaining)
    return budgets


DEFAULT_TOTAL_BUDGET = 180_000
DEFAULT_SECTION_BUDGETS = default_section_budgets(DEFAULT_TOTAL_BUDGET)
DEFAULT_SECTION_FLOORS = {
    "prefix": 8_000,
    "memory": 4_000,
    "memory_guide": 1_000,
    "skills": 2_000,
    "relevant_memory": 1_000,
    "transcript_recall": 800,
    "history": 20_000,
}
# 当 prompt 超预算时，会优先压缩这些 section：历史之后的先缩，不变的前缀部分最后才动（缩了会让缓存失效）。
DEFAULT_REDUCTION_ORDER = ("transcript_recall", "relevant_memory", "memory", "skills", "memory_guide", "prefix", "history")
RELEVANT_MEMORY_LIMIT = 3
TRANSCRIPT_RECALL_LIMIT = 4


class ContextManager:
    SECTION_ORDER = SECTION_ORDER
    CURRENT_REQUEST_SECTION = CURRENT_REQUEST_SECTION
    RELEVANT_MEMORY_LIMIT = RELEVANT_MEMORY_LIMIT

    def __init__(
        self,
        agent,
        total_budget=DEFAULT_TOTAL_BUDGET,
        section_budgets=None,
        section_floors=None,
        reduction_order=None,
    ):
        self.agent = agent
        self._uses_default_section_budgets = True
        self._total_budget = 0
        self._section_budgets = {}
        self.total_budget = int(total_budget)
        self.section_budgets = section_budgets
        self._section_floor_overrides = {str(key): int(value) for key, value in (section_floors or {}).items()}
        self.section_floors = self._compute_section_floors()
        self.reduction_order = tuple(reduction_order or DEFAULT_REDUCTION_ORDER)
        self.history_builder = TurnHistoryBuilder(agent)

    @property
    def total_budget(self):
        return self._total_budget

    @total_budget.setter
    def total_budget(self, value):
        self._total_budget = int(value)
        if getattr(self, "_uses_default_section_budgets", False):
            self._section_budgets = default_section_budgets(self._total_budget)

    @property
    def section_budgets(self):
        return dict(self._section_budgets)

    @section_budgets.setter
    def section_budgets(self, budgets):
        if budgets is None:
            self._uses_default_section_budgets = True
            self._section_budgets = default_section_budgets(self.total_budget)
            return
        self._uses_default_section_budgets = False
        resolved = default_section_budgets(self.total_budget)
        resolved.update({str(key): int(value) for key, value in budgets.items()})
        self._section_budgets = resolved

    def build(self, user_message):
        """按预算组装一轮完整 prompt。

        为什么存在：
        仅靠用户这一轮输入，模型并不知道当前仓库状态、会话里已经读过什么、
        哪些旧信息还值得继续参考。这个函数负责把“稳定基线 + 工作记忆 +
        相关笔记 + 历史 + 当前请求”拼成真正发给模型的 prompt。

        输入 / 输出：
        - 输入：`user_message`，也就是用户当前这一轮的新请求。
        - 输出：`(prompt, metadata)`。
          `prompt` 是最终发送给模型的文本；
          `metadata` 记录了每个 section 的原始长度、裁剪后的长度、是否触发了
          预算收缩等信息，后续会进入 trace/report，便于解释这轮 prompt
          是怎么被拼出来的。

        在 agent 链路里的位置：
        它位于 `BunnyByte.ask()` 的每轮模型调用之前，是“真正发请求给模型”
        的最后一道组装工序。`WorkspaceContext` 提供稳定前缀，`LayeredMemory`
        提供工作记忆，这个函数则把它们和当前请求合成一份可控大小的 prompt。
        """
        user_message = str(user_message)
        self.section_floors = self._compute_section_floors()
        memory_enabled = True
        relevant_memory_enabled = True
        transcript_recall_enabled = True
        context_reduction_enabled = True
        if hasattr(self.agent, "feature_enabled"):
            memory_enabled = self.agent.feature_enabled("memory")
            relevant_memory_enabled = self.agent.feature_enabled("relevant_memory")
            transcript_recall_enabled = self.agent.feature_enabled("transcript_recall")
            context_reduction_enabled = self.agent.feature_enabled("context_reduction")
        durable_enabled = durable_memory_enabled(getattr(self.agent, "session", {}))
        section_texts = {
            "prefix": str(getattr(self.agent, "prefix", "")),
            "skills": skillslib.render_prompt_section(getattr(self.agent, "skills", {})),
            **memory_sections(self.agent, memory_enabled, durable_enabled),
            "history": "",
            CURRENT_REQUEST_SECTION: f"Current user request:\n{user_message}",
        }
        selected_notes = []
        if (
            memory_enabled
            and relevant_memory_enabled
            and durable_enabled
            and hasattr(self.agent, "memory")
            and hasattr(self.agent.memory, "retrieval_candidates")
        ):
            selected_notes = self.agent.memory.retrieval_candidates(user_message, limit=RELEVANT_MEMORY_LIMIT)
        selected_recall = []
        if transcript_recall_enabled:
            selected_recall = retrieve_transcript_snippets(
                user_message,
                getattr(self.agent, "session", {}).get("history", []),
                visible_event_ids=prompt_visible_event_ids(self.agent),
                turn_index=getattr(self.agent, "session", {}).get("turn_index", {}),
                limit=TRANSCRIPT_RECALL_LIMIT,
                char_budget=int(self._section_budgets.get("transcript_recall", 0) or 0),
            )
            selected_recall.extend(
                retrieve_scoped_session_snippets(
                    self.agent,
                    user_message,
                    limit=TRANSCRIPT_RECALL_LIMIT,
                    char_budget=int(self._section_budgets.get("transcript_recall", 0) or 0),
                )
            )

        if not context_reduction_enabled:
            rendered = self._render_sections_without_reduction(section_texts, selected_notes=selected_notes, selected_recall=selected_recall)
            prompt = self._assemble_prompt(rendered)
            metadata = self._metadata(
                prompt=prompt,
                rendered=rendered,
                budgets={section: render.budget for section, render in rendered.items() if section != CURRENT_REQUEST_SECTION},
                reduction_log=[],
                selected_notes=selected_notes,
                selected_recall=selected_recall,
                user_message=user_message,
                section_texts=section_texts,
            )
            return prompt, metadata

        budgets = dict(self._section_budgets)
        rendered = self._render_sections(section_texts, budgets, selected_notes=selected_notes, selected_recall=selected_recall)
        prompt = self._assemble_prompt(rendered)
        reduction_log = []

        while len(prompt) > self.total_budget:
            overflow = len(prompt) - self.total_budget
            reduced = False
            for section in self.reduction_order:
                floor = int(self.section_floors.get(section, 0))
                current_budget = int(budgets.get(section, 0))
                if current_budget <= floor:
                    continue
                new_budget = max(floor, current_budget - overflow)
                if new_budget >= current_budget:
                    continue
                reduction_log.append(
                    {
                        "section": section,
                        "before_chars": current_budget,
                        "after_chars": new_budget,
                        "overflow_chars": overflow,
                    }
                )
                budgets[section] = new_budget
                rendered = self._render_sections(section_texts, budgets, selected_notes=selected_notes, selected_recall=selected_recall)
                prompt = self._assemble_prompt(rendered)
                reduced = True
                break
            if not reduced:
                break

        metadata = self._metadata(
            prompt=prompt,
            rendered=rendered,
            budgets=budgets,
            reduction_log=reduction_log,
            selected_notes=selected_notes,
            selected_recall=selected_recall,
            user_message=user_message,
            section_texts=section_texts,
        )
        return prompt, metadata

    def _render_sections_without_reduction(self, section_texts, selected_notes=None, selected_recall=None):
        return render_sections_without_reduction(
            self.agent,
            self.history_builder,
            section_texts,
            selected_notes=selected_notes,
            selected_recall=selected_recall,
            current_request_section=CURRENT_REQUEST_SECTION,
        )

    def _compute_section_floors(self):
        floors = {
            section: min(
                int(DEFAULT_SECTION_FLOORS.get(section, max(_MIN_SECTION_BUDGET, int(budget) // 4))),
                max(_MIN_SECTION_BUDGET, int(budget) // 4),
            )
            for section, budget in self._section_budgets.items()
        }
        if getattr(getattr(self.agent, "workspace", None), "project_rules", ()):  # 规则让前缀变长；吃紧时别把工具列表裁掉
            prefix_size = min(len(str(self.agent.prefix)), int(self._section_budgets.get("prefix", 0)))
            floors["prefix"] = max(floors.get("prefix", 0), prefix_size)
        floors.update(self._section_floor_overrides)
        return floors

    def _render_sections(self, section_texts, budgets, selected_notes=None, selected_recall=None):
        rendered = {}
        for section in SECTION_ORDER:
            budget = budgets.get(section)
            if section == CURRENT_REQUEST_SECTION:
                raw = section_texts[section]
                rendered[section] = SectionRender(raw=raw, budget=0, rendered=raw, details={})
            elif section == "relevant_memory":
                rendered[section] = self._render_relevant_memory(selected_notes or [], int(budget or 0))
            elif section == "transcript_recall":
                rendered[section] = self._render_transcript_recall(selected_recall or [], int(budget or 0))
            elif section == "history":
                rendered[section] = self._render_history_section(int(budget or 0))
            else:
                raw = section_texts[section]
                rendered_text = tail_clip(raw, int(budget)) if budget is not None else raw
                rendered[section] = SectionRender(raw=raw, budget=int(budget) if budget is not None else 0, rendered=rendered_text, details={})
        return rendered

    def _render_transcript_recall(self, selected_recall, budget):
        raw = render_prompt_recall(selected_recall, budget=max(budget, 1_000))
        rendered = render_prompt_recall(selected_recall, budget=budget)
        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details={"selected_snippets": selected_recall, "rendered_count": len(selected_recall)},
        )

    def _render_relevant_memory(self, selected_notes, budget):
        header = "Relevant memory:"
        note_texts = [str(note.get("text", "")) for note in selected_notes if str(note.get("text", "")).strip()]
        raw_lines = [header] + [f"- {text}" for text in note_texts]
        raw = "\n".join(raw_lines) if note_texts else "\n".join([header, "- none"])
        if not note_texts:
            rendered = raw
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered=rendered,
                details={
                    "selected_notes": [],
                    "rendered_notes": [],
                    "selected_count": 0,
                    "rendered_count": 0,
                    "note_budget": 0,
                },
            )

        note_budget = per_note_budget(budget, len(note_texts), header)
        rendered_notes = []
        while True:
            # 让每条 note 平分这一段的预算，避免一条超长笔记把其他笔记都挤掉。
            rendered_notes = [tail_clip(text, note_budget) for text in note_texts]
            rendered = "\n".join([header] + [f"- {text}" for text in rendered_notes])
            if len(rendered) <= budget or note_budget <= 1:
                break
            note_budget -= 1

        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details={
                "selected_notes": note_texts,
                "rendered_notes": rendered_notes,
                "selected_count": len(note_texts),
                "rendered_count": len(rendered_notes),
                "note_budget": note_budget,
            },
        )

    def _render_history_section(self, budget):
        history = self.history_builder.prompt_history()
        raw = self.history_builder.raw_text(history)
        if not history:
            return SectionRender(raw=raw, budget=budget, rendered="Transcript:\n- empty", details=empty_history_details())

        # 切点按配置的历史预算算：超预算时临时缩小的预算不应该挪动本轮的切点（挪了提示词缓存就断）
        stable_budget = int(self._section_budgets.get("history", 0) or 0) or budget
        rendered, history_details = self.history_builder.render_section(budget, stable_budget=stable_budget)

        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details=history_details,
        )

    def _assemble_prompt(self, rendered):
        return "\n\n".join(rendered[section].rendered for section in SECTION_ORDER if rendered[section].rendered).strip()

    def _metadata(self, prompt, rendered, budgets, reduction_log, selected_notes, selected_recall, user_message, section_texts):
        return build_context_metadata(
            self,
            prompt,
            rendered,
            budgets,
            reduction_log,
            selected_notes,
            selected_recall,
            user_message,
            section_texts,
        )
