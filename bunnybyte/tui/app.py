from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import threading
import time
from functools import partial

from textual.actions import SkipAction
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.events import Key
from textual.widgets import Button

from ..commands.slash import resolve_command
from ..cli import HELP_DETAILS, handle_repl_command, provider_profiles_for_agent
from ..core.memory_scope import (
    mark_memory_scope_startup_prompted,
    should_prompt_memory_scope_startup,
)
from ..core.workspace import clip
from . import theme as tui_theme
from .tool_cards import clip_cells
from .widgets import (
    AskUserPrompt,
    ChatLog,
    ConfirmPrompt,
    InputBar,
    MemoryScopeSessionPrompt,
    ProgressPanel,
    StatusBar,
    ThinkingIndicator,
    ToolCard,
    UserMessage,
    WelcomeBanner,
    confirm_preview,
    format_tool_args,
)


BUNNYBYTE_TUI_CSS = """
Screen {
    layout: vertical;
    background: $bb-bg;
}
"""

MEMORY_SCOPE_STARTUP_CHOICES = (
    ("project", "当前项目（默认）"),
    ("selected", "选择 session"),
    ("all", "全部项目历史"),
    ("off", "关闭记忆"),
)


class BunnyByteTuiApp(App):
    """Textual shell for the existing BunnyByte runtime.

    The TUI is deliberately a presentation layer: CLI argument parsing and agent
    construction still live in `bunnybyte.cli`, while turns are driven through the
    same `Engine.run_turn()` generator that powers the plain REPL.
    """

    CSS = BUNNYBYTE_TUI_CSS
    BINDINGS = [
        Binding(
            "ctrl+c,super+c",
            "copy_selected_text",
            "Copy selected text",
            priority=True,
            show=False,
        ),
        Binding("enter", "submit_input", "Send", priority=True, show=False),
        Binding("ctrl+x", "stop_turn", "Stop response", priority=True),
        Binding("ctrl+l", "clear_screen", "Clear"),
        Binding("ctrl+o", "toggle_tool_cards", "Toggle tool cards", show=False),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, agent, theme: str | None = None, **kwargs) -> None:
        self._initial_theme = tui_theme.resolve_theme(theme)
        tui_theme.set_active_theme(self._initial_theme)
        super().__init__(**kwargs)
        for name in tui_theme.THEME_NAMES:
            self.register_theme(tui_theme.textual_theme(name))
        self.theme = tui_theme.TEXTUAL_THEME[self._initial_theme]
        self.agent = agent
        self._turn_count = 0
        self._running_tool_cards: list[ToolCard] = []
        self._tool_cards_expanded = False
        self._confirm_prompt: ConfirmPrompt | None = None
        self._confirm_decision: tuple[threading.Event, dict] | None = None
        self._confirm_args: tuple[str, dict] | None = None
        self._ask_user_prompt: AskUserPrompt | None = None
        self._ask_user_decision: tuple[threading.Event, dict] | None = None
        self._memory_session_prompt: MemoryScopeSessionPrompt | None = None
        self._memory_session_decision: tuple[threading.Event, dict] | None = None
        self._memory_scope_startup_task: asyncio.Task | None = None
        self._assistant_stream_task: asyncio.Task | None = None
        self._agent_task_handle: asyncio.Task | None = None
        self._command_task_handle: asyncio.Task | None = None
        self._stop_requested = False
        self._steer_widgets: dict[int, UserMessage] = {}
        self._steer_typed_at = 0.0
        self._last_stop_reason = ""
        self._model_stream_widget = None
        self._model_stream_content = ""
        self._model_stream_rendered = ""
        self._last_retry_widget = None
        self._last_retry_content = ""
        self._last_retry_signature = ""
        self._last_retry_count = 0
        self._previous_approve = getattr(agent, "approve", None)
        self._previous_ask_user = getattr(agent, "ask_user_callback", None)
        self.agent.approve = self._approval_callback
        self.agent.ask_user_callback = self._ask_user_callback

    def compose(self) -> ComposeResult:
        yield WelcomeBanner(
            model_name=str(getattr(self.agent.model_client, "model", "")),
            cwd=str(getattr(self.agent, "root", "")),
            approval=str(getattr(self.agent, "approval_policy", "")),
        )
        yield ProgressPanel()
        yield ChatLog()
        yield ThinkingIndicator()
        yield StatusBar()
        yield InputBar()

    def get_theme_variable_defaults(self) -> dict[str, str]:
        # Ctrl+P 里选的 Textual 内置主题没有 $bb-* 变量：按它的明暗补上对应色板，CSS 才能解析。
        return tui_theme.palette(tui_theme.active_theme()).css_variables()

    def on_mount(self) -> None:
        self.theme_changed_signal.subscribe(self, self._on_theme_changed)
        self.query_one(StatusBar).update_agent(self.agent)
        self.query_one(WelcomeBanner).update_agent(self.agent)
        # --resume 启动时聊天区是空的，但会话已有历史：按历史决定是否收起横幅。
        if self.agent.session.get("history"):
            self.query_one(WelcomeBanner).set_collapsed(True)
        self.query_one(ProgressPanel).update_agent(self.agent)
        input_bar = self.query_one(InputBar)
        if hasattr(self.agent, "refresh_skills"):
            self.agent.refresh_skills()
        input_bar.set_provider_profiles(provider_profiles_for_agent(self.agent))
        input_bar.set_skills(getattr(self.agent, "skills", {}) or {})
        input_bar.set_workspace_root(getattr(self.agent, "root", None))
        input_bar.focus_input()
        self.set_interval(0.5, self._drain_idle_worker_notifications)
        if should_prompt_memory_scope_startup(self.agent):
            self._memory_scope_startup_task = asyncio.create_task(
                self._memory_scope_startup_prompt_task()
            )

    def on_unmount(self) -> None:
        tui_theme.set_active_theme(tui_theme.DEFAULT_THEME)
        if self._previous_approve is not None:
            self.agent.approve = self._previous_approve
        self.agent.ask_user_callback = self._previous_ask_user
        if self._memory_scope_startup_task is not None:
            self._memory_scope_startup_task.cancel()
        self._resolve_ask_user("")
        self._resolve_memory_session_scope([])

    def set_ui_theme(self, name: str) -> str:
        """切换深色 / 浅色（不重启）；返回实际生效的色板名。"""
        resolved = tui_theme.resolve_theme(name)
        tui_theme.set_active_theme(resolved)
        self.theme = tui_theme.TEXTUAL_THEME[resolved]
        self._repaint_rich_text()
        return resolved

    def _on_theme_changed(self, theme) -> None:
        name = tui_theme.theme_for_textual(theme)
        if name != tui_theme.active_theme():
            tui_theme.set_active_theme(name)
            self.refresh_css(animate=False)
        self._repaint_rich_text()

    def _repaint_rich_text(self) -> None:
        """Rich 文本的颜色在 render 时取色板；已经生成好的 diff / 卡片标题要重新生成一次。"""
        for card in self.query(ToolCard):
            card.refresh_theme()
        if self._confirm_prompt is not None and self._confirm_args is not None:
            self._confirm_prompt.preview = confirm_preview(*self._confirm_args)
        for widget in self.query("*"):
            widget.refresh()

    def _handle_theme_command(self, text: str) -> None:
        parts = text.split()
        current = tui_theme.active_theme()
        if len(parts) == 1:
            target = "light" if current == "dark" else "dark"
        elif parts[1].lower() in tui_theme.THEME_CHOICES:
            target = parts[1].lower()
        else:
            self.query_one(ChatLog).add_message("assistant", "用法：/theme [dark|light|auto]（不带参数则在深色/浅色之间切换）")
            return
        resolved = self.set_ui_theme(target)
        label = {"dark": "深色", "light": "浅色"}[resolved]
        self.query_one(ChatLog).add_message(
            "assistant",
            f"已切换到{label}主题（只对本次 TUI 生效；要长期使用，在 .bunnybyte.toml 里写 `[tui] theme = \"{resolved}\"`）。",
        )

    def action_clear_screen(self) -> None:
        self.query_one(ChatLog).clear_messages()

    def action_toggle_tool_cards(self) -> None:
        self._tool_cards_expanded = not self._tool_cards_expanded
        self.query_one(ChatLog).set_tool_cards_expanded(self._tool_cards_expanded)

    def action_copy_selected_text(self) -> None:
        selection = self.screen.get_selected_text()
        if selection is None:
            raise SkipAction()
        self.copy_to_clipboard(selection)
        _copy_to_system_clipboard(selection)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = str(event.button.id or "")
        if button_id == "stop-turn":
            event.stop()
            self.action_stop_turn()
            return
        if button_id.startswith("fork-"):
            event.stop()
            self._branch_from_target("fork", button_id.removeprefix("fork-"))
            return
        if button_id.startswith("rollback-"):
            event.stop()
            self._branch_from_target("rollback", button_id.removeprefix("rollback-"))
            return

    def action_stop_turn(self) -> None:
        if self._is_agent_turn_running():
            if self._stop_requested:
                return
            self._stop_requested = True
            self.agent.abort_current_turn()
            detail = "正在停止当前回复..."
            self.query_one(StatusBar).set_stopping()
            self.query_one(InputBar).set_busy(True, stopping=True)
            self.query_one(ThinkingIndicator).set_detail(detail)
            self.query_one(WelcomeBanner).advance_activity(detail)
            return
        if self._is_command_running():
            if self._command_task_handle is not None:
                self._command_task_handle.cancel()
            detail = "正在停止当前命令..."
            self.query_one(InputBar).set_busy(True, stopping=True)
            self.query_one(ThinkingIndicator).set_detail(detail)
            self.query_one(WelcomeBanner).advance_activity(detail)
            return
        raise SkipAction()

    def _is_agent_turn_running(self) -> bool:
        return self._agent_task_handle is not None and not self._agent_task_handle.done()

    def _is_command_running(self) -> bool:
        return self._command_task_handle is not None and not self._command_task_handle.done()

    PROMPT_ARM_SECONDS = 0.6

    def _prompt_open(self) -> bool:
        return any(prompt is not None for prompt in (self._confirm_prompt, self._ask_user_prompt, self._memory_session_prompt))

    def _steer_draft_open(self) -> bool:
        """弹窗出现时输入框里还有正在打的补充要求：键盘继续归输入框，弹窗等它发出或清空。"""
        bar = self.query_one(InputBar)
        return bar.accepting_steers and self._is_agent_turn_running() and bool(bar.input.value.strip())

    def _prompt_keys_armed(self) -> bool:
        # 正在打字时冒出来的审批弹窗不能被句子里的 y 或用来发送的回车批准：草稿清空且停手一会儿后才接受按键。
        if self._steer_draft_open():
            return False
        return time.monotonic() - self._steer_typed_at >= self.PROMPT_ARM_SECONDS

    def on_input_changed(self, event) -> None:
        value = str(getattr(event, "value", "") or "")
        # 只算回合运行中真的在打字：发送后清空不算；Changed 消息是异步到的，发送前那一下的旧值也不算
        if self._is_agent_turn_running() and value and value == self.query_one(InputBar).input.value:
            self._steer_typed_at = time.monotonic()
        if self._prompt_open():
            self._sync_input_lock()

    def action_submit_input(self) -> None:
        if self._prompt_open() and self._steer_draft_open():
            bar = self.query_one(InputBar)
            self._submit_steer(bar, bar.input.value.strip())
            self._sync_input_lock()
            return
        if self._prompt_open() and not self._prompt_keys_armed():
            return
        if self._memory_session_prompt is not None:
            self._resolve_memory_session_scope(
                self._memory_session_prompt.selected_session_ids
            )
            return
        if self._ask_user_prompt is not None:
            self._resolve_ask_user(self._ask_user_prompt.selected_choice)
            return
        if self._confirm_prompt is not None:
            self._resolve_confirm(self._confirm_prompt.selected)
            return
        bar = self.query_one(InputBar)
        text = bar.input.value.strip()
        if not text or bar.input.disabled:
            return
        if bar.mention_completion_pending() and bar.complete_slash_suggestion():
            return
        if bar.accepting_steers and self._is_agent_turn_running():
            self._submit_steer(bar, text)
            return
        selected_command = None
        candidate = bar.selected_slash_suggestion()
        if text.startswith("/") and candidate is not None:
            if " " in candidate.name or (
                " " not in text[1:] and resolve_command(text) is None
            ):
                selected_command = candidate
        if selected_command and bar.complete_slash_suggestion():
            text = bar.input.value.strip()
            if selected_command.requires_arguments:
                return
        bar.history.append(text)
        bar.history_index = len(bar.history)
        bar.input.value = ""
        bar.hide_slash_suggestions()
        # 聊天区显示占位符（不刷屏），发给运行时的是展开后的完整粘贴内容。
        full_text = bar.input.expand(text)
        if text.startswith("/"):
            if text.split(maxsplit=1)[0] == "/banner":
                self.query_one(WelcomeBanner).toggle_collapsed()
                return
            if text.split(maxsplit=1)[0] == "/theme":
                self.query_one(ChatLog).add_message("user", text)
                self._handle_theme_command(text)
                return
            self.query_one(ChatLog).add_message("user", text)
            self._handle_command(full_text)
            return
        self.query_one(ChatLog).add_message("user", text)
        self._run_agent(full_text, mention_text=bar.input.strip_tokens(text))

    def _submit_steer(self, bar: InputBar, text: str) -> None:
        """回合运行中按回车：话排进运行时队列，下一次模型调用前交给模型，不打断正在跑的工具。"""
        if text.startswith("/") and text.split(maxsplit=1)[0] not in {"/banner", "/theme"}:
            self.notify("agent 正在工作：斜杠命令请等这一轮结束或停止后再用；普通文字会作为补充要求送达。", severity="warning")
            return
        bar.history.append(text)
        bar.history_index = len(bar.history)
        bar.input.value = ""
        bar.hide_slash_suggestions()
        if text.split(maxsplit=1)[0] == "/banner":
            self.query_one(WelcomeBanner).toggle_collapsed()
            return
        if text.split(maxsplit=1)[0] == "/theme":
            self.query_one(ChatLog).add_message("user", text)
            self._handle_theme_command(text)
            return
        steer_id = self.agent.steer(bar.input.expand(text))
        if not steer_id:
            return
        widget = self.query_one(ChatLog).add_message("user", text)
        widget.set_steer_status("queued")
        self._steer_widgets[steer_id] = widget

    def _sync_input_lock(self) -> None:
        """弹出审批 / 提问 / 记忆范围选择时锁住输入框，y/n 和方向键才会落到弹窗上；关掉后恢复。"""
        bar = self.query_one(InputBar)
        if self._prompt_open():
            if self._steer_draft_open():
                bar.input.disabled = False
                if not getattr(self, "_draft_notice_shown", False):
                    self._draft_notice_shown = True
                    self.notify("弹出了需要你回答的提示：先回车发送或清空输入框里的文字，再用按键回答。", severity="warning")
                return
            bar.input.disabled = True
            return
        self._draft_notice_shown = False
        bar.input.disabled = bar.has_class("busy") and not bar.accepting_steers
        if not bar.input.disabled:
            bar.focus_input()

    def _mark_steers_delivered(self, ids) -> None:
        for steer_id in ids or []:
            widget = self._steer_widgets.pop(int(steer_id), None)
            if widget is not None:
                widget.set_steer_status("delivered")

    def _after_turn_steers(self) -> None:
        """回合结束时还没送达的话：正常结束就合成下一轮发出去；停止或出错就放回输入框，由用户决定。"""
        pending = self.agent.take_pending_steers()
        widgets = [self._steer_widgets.pop(steer_id, None) for steer_id, _ in pending]
        self._steer_widgets.clear()
        if not pending:
            return
        texts = [text for _, text in pending]
        if self._last_stop_reason == "final_answer_returned" and not self._stop_requested:
            for widget in widgets:
                if widget is not None:
                    widget.set_steer_status("next_turn")
            self._run_agent("\n\n".join(texts))
            return
        for widget in widgets:
            if widget is not None:
                widget.remove()
        bar = self.query_one(InputBar)
        bar.input.value = "\n".join(filter(None, [*texts, bar.input.value]))
        bar.input.cursor_position = len(bar.input.value)
        self.notify("这一轮没有正常结束，还没送达的补充要求放回了输入框。")

    def on_key(self, event: Key) -> None:
        if self._prompt_open() and not self._prompt_keys_armed():
            if not self._steer_draft_open():
                event.prevent_default()  # 刚停手打字：这一下按键不算回答弹窗
            return
        if self._memory_session_prompt is not None:
            if event.key in {"down", "right"}:
                self._memory_session_prompt.select_next()
                event.prevent_default()
            elif event.key in {"up", "left"}:
                self._memory_session_prompt.select_previous()
                event.prevent_default()
            elif event.key in {"space", "tab"}:
                self._memory_session_prompt.toggle_current()
                event.prevent_default()
            elif event.key == "enter":
                self._resolve_memory_session_scope(
                    self._memory_session_prompt.selected_session_ids
                )
                event.prevent_default()
            elif event.key == "escape":
                self._resolve_memory_session_scope([])
                event.prevent_default()
            return
        if self._ask_user_prompt is not None:
            if event.key in {"right", "down"}:
                self._ask_user_prompt.select_next()
                event.prevent_default()
            elif event.key in {"left", "up"}:
                self._ask_user_prompt.select_previous()
                event.prevent_default()
            elif event.key == "enter":
                self._resolve_ask_user(self._ask_user_prompt.selected_choice)
                event.prevent_default()
            elif event.key == "escape":
                self._resolve_ask_user("")
                event.prevent_default()
            return
        if self._confirm_prompt is not None:
            if event.key in {"y", "right"}:
                self._confirm_prompt.select_allow()
                event.prevent_default()
            elif event.key in {"n", "left"}:
                self._confirm_prompt.select_deny()
                event.prevent_default()
            elif event.key == "enter":
                self._resolve_confirm(self._confirm_prompt.selected)
                event.prevent_default()
            elif event.key == "escape":
                self._resolve_confirm(False)
                event.prevent_default()
            return
        bar = self.query_one(InputBar)
        if event.key == "tab" and bar.complete_slash_suggestion():
            event.prevent_default()
        elif event.key == "up" and bar.move_slash_selection(-1):
            event.prevent_default()
        elif event.key == "down" and bar.move_slash_selection(1):
            event.prevent_default()
        elif event.key == "escape":
            if self._is_agent_turn_running() or self._is_command_running():
                self.action_stop_turn()
            else:
                bar.hide_slash_suggestions()
            event.prevent_default()
        elif event.key == "up":
            bar.history_prev()
            event.prevent_default()
        elif event.key == "down":
            bar.history_next()
            event.prevent_default()

    def _handle_command(self, text: str) -> None:
        if self._command_should_run_in_executor(text):
            self._run_command_in_executor(text)
            return
        self._handle_command_result(text, from_thread=False)

    def _command_should_run_in_executor(self, text: str) -> bool:
        command = str(text or "").strip().split(maxsplit=1)[0].lstrip("/")
        if command in {"compact", "dream"}:
            return True
        if command == "skill":
            return True
        return command in (getattr(self.agent, "skills", {}) or {})

    def _run_command_in_executor(self, text: str) -> None:
        self.query_one(InputBar).set_busy(True, stoppable=True)
        self.query_one(WelcomeBanner).set_activity(True, f"运行 {text.strip()}")
        self.query_one(ThinkingIndicator).show("Ctrl+X / Esc 或 Stop 停止命令")
        self.query_one(ThinkingIndicator).set_detail(f"运行 {text.strip()}")
        self._thinking_timer = self.set_interval(0.3, self._advance_activity)
        self._command_task_handle = asyncio.create_task(self._command_task(text))

    async def _command_task(self, text: str) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, partial(self._handle_command_result, text, from_thread=True)
            )
        except asyncio.CancelledError:
            self.query_one(ChatLog).add_message("assistant", "已请求停止当前命令。")
            raise
        except Exception as exc:
            self.query_one(ChatLog).add_message("assistant", f"[错误] {exc}")
        finally:
            self._stop_thinking()
            self.query_one(InputBar).set_busy(False)
            self.query_one(InputBar).focus_input()
            self._refresh_runtime_identity()

    def _handle_command_result(self, text: str, from_thread: bool = False) -> None:
        previous_session_id = str(self.agent.session.get("id", ""))
        handled, should_exit, output = handle_repl_command(self.agent, text)

        def run_ui(callback, *args):
            if from_thread:
                self.call_from_thread(callback, *args)
            else:
                callback(*args)

        if should_exit:
            run_ui(self.exit)
            return
        if handled:
            current_session_id = str(self.agent.session.get("id", ""))
            if self._command_switched_session(text, previous_session_id, current_session_id):
                run_ui(self._render_session_history, _session_switch_message(text, output, current_session_id))
            else:
                run_ui(self._add_assistant_message, output)
            run_ui(self._refresh_runtime_identity)
            return
        run_ui(
            self._add_assistant_message,
            f"未知命令。使用 /help 查看帮助。\n\n{HELP_DETAILS}",
        )

    def _add_assistant_message(self, content: str) -> None:
        self.query_one(ChatLog).add_message("assistant", content)

    def _branch_from_target(self, command: str, target: str) -> None:
        if self._is_agent_turn_running() or self._is_command_running():
            self.query_one(ChatLog).add_message("assistant", "当前仍在运行，完成或停止后再操作历史节点。")
            return
        self._handle_command(f"/{command} {target}")

    def _command_switched_session(
        self, text: str, previous_session_id: str, current_session_id: str
    ) -> bool:
        if not text.strip().startswith(("/resume", "/fork", "/rollback")):
            return False
        return bool(current_session_id and current_session_id != previous_session_id)

    def _render_session_history(self, notice: str = "") -> None:
        chat = self.query_one(ChatLog)
        chat.clear_messages()
        self.query_one(WelcomeBanner).set_collapsed(bool(self.agent.session.get("history")))
        for item in self.agent.session.get("history", []):
            role = str(item.get("role", ""))
            content = str(item.get("content", ""))
            fork_target = str(item.get("event_id", "") or item.get("turn_id", ""))
            if role in {"user", "assistant"}:
                chat.add_message(role, content, fork_target=fork_target)
            elif role == "tool":
                name = str(item.get("name", "tool") or "tool")
                args = item.get("args") if isinstance(item.get("args"), dict) else {}
                chat.add_tool_history(name, args, content)
        session_id = str(self.agent.session.get("id", ""))
        chat.add_message("assistant", notice or f"已恢复会话 {session_id}")

    def _history_tool_summary(self, item: dict) -> str:
        name = str(item.get("name", "tool") or "tool")
        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        content = clip(str(item.get("content", "")), 120)
        if content:
            return f"{format_tool_args(name, args)} -> {content}"
        return format_tool_args(name, args)

    def _run_agent(self, text: str, mention_text: str | None = None) -> None:
        self._stop_requested = False
        self._last_stop_reason = ""
        self._running_tool_cards = []
        self.query_one(WelcomeBanner).set_collapsed(True)
        self.query_one(StatusBar).start_turn()
        self.query_one(InputBar).set_busy(True, accepts_input=True)
        self._sync_input_lock()
        self.query_one(WelcomeBanner).set_activity(True, "思考中")
        self.query_one(ThinkingIndicator).show("按 Ctrl+X / Esc 或点击 Stop 停止")
        self._thinking_timer = self.set_interval(0.3, self._advance_activity)
        self._agent_task_handle = asyncio.create_task(self._agent_task(text, mention_text))

    def _drain_idle_worker_notifications(self) -> None:
        if self._is_agent_turn_running() or self._is_command_running() or self.query_one(InputBar).input.disabled:
            return
        notifications = self.agent.engine.drain_worker_notifications()
        if not notifications:
            return
        chat = self.query_one(ChatLog)
        for notification in notifications:
            chat.add_message("assistant", f"[worker 通知]\n{notification}")
        self.query_one(ProgressPanel).update_agent(self.agent)
        self.query_one(StatusBar).update_agent(self.agent)
        self.query_one(WelcomeBanner).update_agent(self.agent)

    async def _agent_task(self, text: str, mention_text: str | None = None) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, partial(self._drive_turn, text, mention_text))
        except Exception as exc:
            self.query_one(ChatLog).add_message("assistant", f"[错误] {exc}")
        finally:
            if self._assistant_stream_task is not None:
                try:
                    await self._assistant_stream_task
                except Exception:
                    pass
            self._stop_thinking()
            self.query_one(InputBar).set_busy(False)
            self.query_one(InputBar).focus_input()
            self._turn_count += 1
            status = self.query_one(StatusBar)
            status.finish_turn()
            status.update_turns(self._turn_count)
            self.query_one(WelcomeBanner).update_turns(self._turn_count)
            self._refresh_runtime_identity()
            usage = (getattr(self.agent, "last_prompt_metadata", {}) or {}).get(
                "context_usage"
            ) or {}
            status.update_context_usage(usage)
            self.query_one(WelcomeBanner).update_context_usage(usage)
            self._after_turn_steers()

    def _refresh_runtime_identity(self) -> None:
        self.query_one(WelcomeBanner).update_agent(self.agent)
        self.query_one(StatusBar).update_agent(self.agent)
        self.query_one(ProgressPanel).update_agent(self.agent)
        input_bar = self.query_one(InputBar)
        if hasattr(self.agent, "refresh_skills"):
            self.agent.refresh_skills()
        input_bar.set_provider_profiles(provider_profiles_for_agent(self.agent))
        input_bar.set_skills(getattr(self.agent, "skills", {}) or {})

    def _advance_activity(self) -> None:
        self.query_one(StatusBar).tick()
        self.query_one(ThinkingIndicator).advance()
        self.query_one(WelcomeBanner).advance_activity()
        self.query_one(ProgressPanel).update_agent(self.agent)

    def _drive_turn(self, text: str, mention_text: str | None = None) -> None:
        for event in self.agent.engine.run_turn(text, mention_text=mention_text):
            try:
                self.call_from_thread(self._handle_runtime_event, dict(event))
            except RuntimeError:
                return

    async def _memory_scope_startup_prompt_task(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._run_memory_scope_startup_prompt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.query_one(ChatLog).add_message("assistant", f"[错误] {exc}")
        finally:
            self._memory_scope_startup_task = None

    def _run_memory_scope_startup_prompt(self) -> None:
        mark_memory_scope_startup_prompted(self.agent)
        choice = self._ask_user_callback(
            "为这个新 session 选择长期记忆范围",
            [label for _, label in MEMORY_SCOPE_STARTUP_CHOICES],
        )
        mode = _memory_scope_mode_from_choice(choice)
        if not mode:
            self._post_memory_scope_notice("已保留默认记忆范围：project。")
            return
        if mode == "project":
            self._post_memory_scope_notice("已使用默认记忆范围：project。")
            return
        if mode == "selected":
            selected_ids = self._prompt_for_memory_scope_sessions()
            if not selected_ids:
                self._post_memory_scope_notice("未选择 session，已保留默认记忆范围：project。")
                return
            self.agent.set_memory_scope("selected", session_ids=selected_ids)
        else:
            self.agent.set_memory_scope(mode)
        self._post_memory_scope_notice(self.agent.memory_scope_text())

    def _prompt_for_memory_scope_sessions(self) -> list[str]:
        choices = self._memory_scope_session_choices()
        if not choices:
            self._post_memory_scope_notice("没有已保存 session 可选，已保留默认记忆范围：project。")
            return []
        event = threading.Event()
        decision = {"session_ids": []}
        try:
            self.call_from_thread(
                self._show_memory_session_prompt,
                "选择要作为长期记忆召回点的 session",
                choices,
                event,
                decision,
            )
        except RuntimeError:
            return []
        event.wait()
        return [str(item) for item in decision.get("session_ids", []) if str(item)]

    def _memory_scope_session_choices(self, limit: int = 8) -> list[dict]:
        current_id = str(self.agent.session.get("id", ""))
        choices = []
        for row in self.agent.session_store.list_sessions():
            session_id = str(row.get("id", "")).strip()
            if not session_id or session_id == current_id:
                continue
            topic = clip_cells(str(row.get("topic", "") or "Untitled"), 36)
            updated = str(row.get("updated_at", "") or "-")
            label = f"{topic} · {session_id} · {updated}"
            choices.append({"id": session_id, "label": label})
            if len(choices) >= limit:
                break
        return choices

    def _show_memory_session_prompt(
        self,
        question: str,
        choices: list[dict],
        event: threading.Event,
        decision: dict,
    ) -> None:
        prompt = MemoryScopeSessionPrompt(question, choices)
        self._memory_session_prompt = prompt
        self._memory_session_decision = (event, decision)
        self._sync_input_lock()
        chat = self.query_one(ChatLog)
        chat.mount(prompt)
        chat.call_after_refresh(chat.scroll_end, animate=False)

    def _resolve_memory_session_scope(self, session_ids: list[str]) -> None:
        if self._memory_session_decision is None:
            return
        event, decision = self._memory_session_decision
        decision["session_ids"] = list(session_ids or [])
        event.set()
        if self._memory_session_prompt is not None:
            self._memory_session_prompt.remove()
        self._memory_session_prompt = None
        self._memory_session_decision = None
        self._sync_input_lock()

    def _post_memory_scope_notice(self, content: str) -> None:
        try:
            self.call_from_thread(self._add_assistant_message, content)
            self.call_from_thread(self._refresh_runtime_identity)
        except RuntimeError:
            return

    def _handle_runtime_event(self, event: dict) -> None:
        event_type = str(event.get("type", ""))
        if event_type == "model_requested":
            self._reset_model_stream()
            attempts = event.get("attempts", 0)
            tool_steps = event.get("tool_steps", 0)
            detail = f"第 {attempts} 次请求 · 已用 {tool_steps} 步"
            self.query_one(ThinkingIndicator).start_phase("思考中", detail)
            self.query_one(WelcomeBanner).advance_activity(detail)
            return
        if event_type == "model_delta":
            delta = str(event.get("content", ""))
            self._append_model_stream_delta(delta)
            # 只累计字数；动画和重绘交给 0.3s 定时器，不按 token 速度刷新。
            self.query_one(ThinkingIndicator).add_output(delta)
            self.query_one(WelcomeBanner).advance_activity(f"接收模型输出 {event.get('total_chars', 0)} 字符")
            return
        if event_type == "model_parsed":
            kind = event.get("kind", "")
            if kind in {"tool", "tools"}:
                self._discard_model_stream()
            self.query_one(StatusBar).record_model_usage(event.get("input_tokens"), event.get("output_tokens"))
            detail = f"模型返回 {kind}"
            self.query_one(ThinkingIndicator).set_detail(detail)
            self.query_one(WelcomeBanner).advance_activity(detail)
            return
        if event_type == "tool_call":
            name = str(event.get("name", ""))
            args = event.get("args") if isinstance(event.get("args"), dict) else {}
            detail = f"运行 {name}"
            source = "预读 @ 文件" if event.get("source") == "mention" else ""
            self.query_one(ThinkingIndicator).start_phase(detail, source)
            self.query_one(WelcomeBanner).advance_activity(detail)
            card = self.query_one(ChatLog).add_tool_call(name, args, expanded=self._tool_cards_expanded)
            self._running_tool_cards.append(card)
            return
        if event_type == "tool_result":
            if event.get("name") == "critic":  # 审查也是一次模型调用；只累计输出，上下文占用仍看主模型
                self.query_one(StatusBar).record_model_usage(None, event.get("output_tokens"))
            self._finish_tool_card(event)
            self.query_one(ProgressPanel).update_agent(self.agent)
            self.query_one(ThinkingIndicator).start_phase("思考中", "工具完成")
            self.query_one(WelcomeBanner).advance_activity("工具完成，继续思考")
            return
        if event_type == "steer_delivered":
            self._mark_steers_delivered(event.get("ids"))
            return
        if event_type == "turn_finished":
            self._last_stop_reason = str(event.get("stop_reason", ""))
            return
        if event_type == "worker_notification":
            self.query_one(ChatLog).add_message(
                "assistant", f"[worker 通知]\n{event.get('content', '')}"
            )
            return
        if event_type in {"assistant_preamble", "retry", "runtime_notice", "final", "stop"}:
            content = str(event.get("content", ""))
            fork_target = str(event.get("event_id", "")) if event_type == "final" else ""
            if event_type == "retry" and content.startswith("Your previous response could not be executed."):
                self._queue_retry_notice(content)
            else:
                self._queue_assistant_stream(content, fork_target=fork_target)
            return

    def _finish_tool_card(self, event: dict) -> None:
        name = str(event.get("name", ""))
        card = None
        # 并行工具先全部发出 tool_call，结果再按调用顺序提交，所以取最早的那张同名卡片。
        for candidate in self._running_tool_cards:
            if candidate.tool_name == name and candidate.status == "running":
                card = candidate
                break
        if card is None:
            card = self.query_one(ChatLog).add_tool_call(name, {})
        else:
            self._running_tool_cards.remove(card)
        metadata = (
            event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        )
        content = str(event.get("content", ""))
        status = str(metadata.get("tool_status", "ok") or "ok")
        duration_ms = event.get("duration_ms")
        if status in {"error", "rejected", "partial_success"}:
            card.set_error(content, duration_ms=duration_ms)
        else:
            card.set_success(content, duration_ms=duration_ms)

    def _stop_thinking(self) -> None:
        timer = getattr(self, "_thinking_timer", None)
        if timer is not None:
            timer.stop()
            self._thinking_timer = None
        self.query_one(ThinkingIndicator).hide()
        self.query_one(WelcomeBanner).set_activity(False, "就绪")

    def _queue_retry_notice(self, content: str) -> None:
        signature = _retry_signature(content)
        if signature == self._last_retry_signature and self._last_retry_widget is not None:
            self._last_retry_count += 1
            self._last_retry_content = content
            self._last_retry_widget.update_content(f"{content}\n\n（重复 {self._last_retry_count} 次）")
            return
        self._last_retry_signature = signature
        self._last_retry_content = content
        self._last_retry_count = 1
        self._last_retry_widget = self.query_one(ChatLog).add_message("assistant", content)

    def _queue_assistant_stream(self, content: str, fork_target: str = "") -> None:
        content = str(content or "")
        self._last_retry_widget = None
        self._last_retry_content = ""
        self._last_retry_signature = ""
        self._last_retry_count = 0
        if self._assistant_stream_task is not None and not self._assistant_stream_task.done():
            self._assistant_stream_task.cancel()
        self._assistant_stream_task = None
        if self._model_stream_widget is not None:
            widget = self._model_stream_widget
            self._reset_model_stream()
            if fork_target:
                widget.remove()
                self.query_one(ChatLog).add_message("assistant", content, fork_target=fork_target)
            else:
                widget.update_content(content)
                self.query_one(ChatLog).scroll_end(animate=False)
            return
        self.query_one(ChatLog).add_message("assistant", content, fork_target=fork_target)

    def _append_model_stream_delta(self, delta: str) -> None:
        if not delta:
            return
        self._model_stream_content += delta
        preview = _model_stream_preview(
            self._model_stream_content,
            legacy=not bool(
                getattr(self.agent.model_client, "supports_native_tools", False)
            ),
        )
        if not preview:
            return
        if self._model_stream_widget is not None and len(preview) - len(self._model_stream_rendered) < 80 and "\n" not in preview[len(self._model_stream_rendered):]:
            return
        self._model_stream_rendered = preview
        if self._model_stream_widget is None:
            self._model_stream_widget = self.query_one(ChatLog).add_message(
                "assistant", preview
            )
        else:
            self._model_stream_widget.update_content(preview)
            self.query_one(ChatLog).scroll_end(animate=False)

    def _reset_model_stream(self) -> None:
        self._model_stream_widget = None
        self._model_stream_content = ""
        self._model_stream_rendered = ""

    def _discard_model_stream(self) -> None:
        if self._model_stream_widget is not None:
            self._model_stream_widget.remove()
        self._reset_model_stream()

    async def _stream_assistant_message(self, content: str) -> None:
        content = str(content or "")
        widget = self.query_one(ChatLog).add_message("assistant", "")
        if not content:
            return
        chunk_size = 12
        delay = 0.012
        for index in range(0, len(content), chunk_size):
            widget.update_content(content[: index + chunk_size])
            self.query_one(ChatLog).scroll_end(animate=False)
            await asyncio.sleep(delay)
        widget.update_content(content)
        self.query_one(ChatLog).scroll_end(animate=False)

    def _approval_callback(self, name: str, args: dict) -> bool:
        event = threading.Event()
        decision = {"approved": False}
        try:
            self.call_from_thread(self._show_confirm, name, args, event, decision)
        except RuntimeError:
            return False
        event.wait()
        return bool(decision.get("approved", False))

    def _show_confirm(
        self, name: str, args: dict, event: threading.Event, decision: dict
    ) -> None:
        prompt = ConfirmPrompt(name, format_tool_args(name, args), preview=confirm_preview(name, args))
        self._confirm_prompt = prompt
        self._confirm_args = (name, dict(args or {}))
        self._confirm_decision = (event, decision)
        self._sync_input_lock()
        chat = self.query_one(ChatLog)
        chat.mount(prompt)
        chat.call_after_refresh(chat.scroll_end, animate=False)

    def _resolve_confirm(self, approved: bool) -> None:
        if self._confirm_decision is None:
            return
        event, decision = self._confirm_decision
        decision["approved"] = bool(approved)
        event.set()
        if self._confirm_prompt is not None:
            self._confirm_prompt.remove()
        self._confirm_prompt = None
        self._confirm_decision = None
        self._confirm_args = None
        self._sync_input_lock()

    def _ask_user_callback(self, question: str, choices: list[str]) -> str:
        event = threading.Event()
        decision = {"answer": ""}
        try:
            self.call_from_thread(
                self._show_ask_user, question, choices, event, decision
            )
        except RuntimeError:
            return ""
        event.wait()
        return str(decision.get("answer", ""))

    def _show_ask_user(
        self, question: str, choices: list[str], event: threading.Event, decision: dict
    ) -> None:
        prompt = AskUserPrompt(question, choices)
        self._ask_user_prompt = prompt
        self._ask_user_decision = (event, decision)
        self._sync_input_lock()
        chat = self.query_one(ChatLog)
        chat.mount(prompt)
        chat.call_after_refresh(chat.scroll_end, animate=False)

    def _resolve_ask_user(self, answer: str) -> None:
        if self._ask_user_decision is None:
            return
        event, decision = self._ask_user_decision
        decision["answer"] = str(answer)
        event.set()
        if self._ask_user_prompt is not None:
            self._ask_user_prompt.remove()
        self._ask_user_prompt = None
        self._ask_user_decision = None
        self._sync_input_lock()


def _session_switch_message(command_text: str, command_output: str, session_id: str) -> str:
    command = str(command_text or "").strip()
    if command.startswith("/fork"):
        return command_output or f"已创建分支会话 {session_id}"
    if command.startswith("/rollback"):
        return command_output or f"已回滚并创建分支会话 {session_id}"
    return f"已恢复会话 {session_id}"


def _memory_scope_mode_from_choice(choice: str) -> str:
    value = str(choice or "").strip()
    for mode, label in MEMORY_SCOPE_STARTUP_CHOICES:
        if value == label or value.lower() == mode:
            return mode
    return ""


def _copy_to_system_clipboard(text: str) -> None:
    """Best-effort copy for terminals that don't accept Textual's OSC 52 clipboard."""
    command: list[str] | None = None
    if sys.platform == "darwin" and shutil.which("pbcopy"):
        command = ["pbcopy"]
    elif shutil.which("wl-copy"):
        command = ["wl-copy"]
    elif shutil.which("xclip"):
        command = ["xclip", "-selection", "clipboard"]
    elif shutil.which("xsel"):
        command = ["xsel", "--clipboard", "--input"]
    if command is None:
        return
    try:
        subprocess.run(
            command,
            input=text,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1,
            check=False,
        )
    except Exception:
        return


def _retry_signature(content: str) -> str:
    text = str(content or "")
    if "Offending output preview:" in text:
        text = text.split("Offending output preview:", 1)[0]
    if " Return one or more valid" in text:
        text = text.split(" Return one or more valid", 1)[0]
    return " ".join(text.split())


def _model_stream_preview(content: str, *, legacy: bool = False) -> str:
    text = str(content or "")
    if not legacy:
        return text
    marker = "<final>"
    if marker in text:
        body = text.split(marker, 1)[1]
        if "</final>" in body:
            body = body.split("</final>", 1)[0]
        return body
    if "<tool" in text:
        return ""
    return text
