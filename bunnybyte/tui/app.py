from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import threading
from functools import partial

from textual.actions import SkipAction
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.events import Key

from ..commands.slash import resolve_command
from ..cli import HELP_DETAILS, handle_repl_command
from ..core.workspace import clip
from .widgets import (
    AskUserPrompt,
    ChatLog,
    ConfirmPrompt,
    InputBar,
    ProgressPanel,
    StatusBar,
    ThinkingIndicator,
    ToolCard,
    WelcomeBanner,
    format_tool_args,
)


BUNNYBYTE_TUI_CSS = """
Screen {
    layout: vertical;
    background: #0f1117;
}
"""


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
        Binding("ctrl+l", "clear_screen", "Clear"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, agent, **kwargs) -> None:
        super().__init__(**kwargs)
        self.agent = agent
        self._turn_count = 0
        self._running_tool_cards: list[ToolCard] = []
        self._confirm_prompt: ConfirmPrompt | None = None
        self._confirm_decision: tuple[threading.Event, dict] | None = None
        self._ask_user_prompt: AskUserPrompt | None = None
        self._ask_user_decision: tuple[threading.Event, dict] | None = None
        self._assistant_stream_task: asyncio.Task | None = None
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

    def on_mount(self) -> None:
        self.query_one(StatusBar).update_agent(self.agent)
        self.query_one(WelcomeBanner).update_agent(self.agent)
        self.query_one(ProgressPanel).update_agent(self.agent)
        self.query_one(InputBar).focus_input()
        self.set_interval(0.5, self._drain_idle_worker_notifications)

    def on_unmount(self) -> None:
        if self._previous_approve is not None:
            self.agent.approve = self._previous_approve
        self.agent.ask_user_callback = self._previous_ask_user

    def action_clear_screen(self) -> None:
        self.query_one(ChatLog).clear_messages()

    def action_copy_selected_text(self) -> None:
        selection = self.screen.get_selected_text()
        if selection is None:
            raise SkipAction()
        self.copy_to_clipboard(selection)
        _copy_to_system_clipboard(selection)

    def action_submit_input(self) -> None:
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
        selected_command = None
        if text.startswith("/") and " " not in text[1:] and resolve_command(text) is None:
            selected_command = bar.selected_slash_suggestion()
        if selected_command and bar.complete_slash_suggestion():
            text = bar.input.value.strip()
            if selected_command.requires_arguments:
                return
        bar.history.append(text)
        bar.history_index = len(bar.history)
        bar.input.value = ""
        if text.startswith("/"):
            self.query_one(ChatLog).add_message("user", text)
            bar.hide_slash_suggestions()
            self._handle_command(text)
            return
        self.query_one(ChatLog).add_message("user", text)
        self._run_agent(text)

    def on_key(self, event: Key) -> None:
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
            bar.hide_slash_suggestions()
            event.prevent_default()
        elif event.key == "up":
            bar.history_prev()
            event.prevent_default()
        elif event.key == "down":
            bar.history_next()
            event.prevent_default()

    def _handle_command(self, text: str) -> None:
        if text.strip() == "/dream":
            self._run_command_in_executor(text)
            return
        self._handle_command_result(text, from_thread=False)

    def _run_command_in_executor(self, text: str) -> None:
        self.query_one(InputBar).set_busy(True)
        self.query_one(WelcomeBanner).set_activity(True, f"running {text.strip()}")
        self.query_one(ThinkingIndicator).show()
        self.query_one(ThinkingIndicator).set_detail(f"running {text.strip()}")
        self._thinking_timer = self.set_interval(0.3, self._advance_activity)
        asyncio.create_task(self._command_task(text))

    async def _command_task(self, text: str) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, partial(self._handle_command_result, text, from_thread=True)
            )
        except Exception as exc:
            self.query_one(ChatLog).add_message("assistant", f"[Error] {exc}")
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
                run_ui(self._render_session_history)
            else:
                run_ui(self._add_assistant_message, output)
            run_ui(self._refresh_runtime_identity)
            return
        run_ui(
            self._add_assistant_message,
            f"Unknown command. Use /help.\n\n{HELP_DETAILS}",
        )

    def _add_assistant_message(self, content: str) -> None:
        self.query_one(ChatLog).add_message("assistant", content)

    def _command_switched_session(
        self, text: str, previous_session_id: str, current_session_id: str
    ) -> bool:
        if not text.strip().startswith("/resume"):
            return False
        return bool(current_session_id and current_session_id != previous_session_id)

    def _render_session_history(self) -> None:
        chat = self.query_one(ChatLog)
        chat.clear_messages()
        for item in self.agent.session.get("history", []):
            role = str(item.get("role", ""))
            content = str(item.get("content", ""))
            if role in {"user", "assistant"}:
                chat.add_message(role, content)
            elif role == "tool":
                name = str(item.get("name", "tool") or "tool")
                args = item.get("args") if isinstance(item.get("args"), dict) else {}
                chat.add_tool_history(name, args, content)
        session_id = str(self.agent.session.get("id", ""))
        chat.add_message("assistant", f"resumed session {session_id}")

    def _history_tool_summary(self, item: dict) -> str:
        name = str(item.get("name", "tool") or "tool")
        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        content = clip(str(item.get("content", "")), 120)
        if content:
            return f"{format_tool_args(name, args)} -> {content}"
        return format_tool_args(name, args)

    def _run_agent(self, text: str) -> None:
        self.query_one(InputBar).set_busy(True)
        self.query_one(WelcomeBanner).set_activity(True, "thinking")
        self.query_one(ThinkingIndicator).show()
        self._thinking_timer = self.set_interval(0.3, self._advance_activity)
        asyncio.create_task(self._agent_task(text))

    def _drain_idle_worker_notifications(self) -> None:
        if self.query_one(InputBar).input.disabled:
            return
        notifications = self.agent.engine.drain_worker_notifications()
        if not notifications:
            return
        chat = self.query_one(ChatLog)
        for notification in notifications:
            chat.add_message("assistant", f"[worker notification]\n{notification}")
        self.query_one(ProgressPanel).update_agent(self.agent)
        self.query_one(StatusBar).update_agent(self.agent)
        self.query_one(WelcomeBanner).update_agent(self.agent)

    async def _agent_task(self, text: str) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, partial(self._drive_turn, text))
        except Exception as exc:
            self.query_one(ChatLog).add_message("assistant", f"[Error] {exc}")
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
            status.update_turns(self._turn_count)
            self.query_one(WelcomeBanner).update_turns(self._turn_count)
            self._refresh_runtime_identity()
            usage = (getattr(self.agent, "last_prompt_metadata", {}) or {}).get(
                "context_usage"
            ) or {}
            status.update_context_usage(usage)
            self.query_one(WelcomeBanner).update_context_usage(usage)

    def _refresh_runtime_identity(self) -> None:
        self.query_one(WelcomeBanner).update_agent(self.agent)
        self.query_one(StatusBar).update_agent(self.agent)
        self.query_one(ProgressPanel).update_agent(self.agent)

    def _advance_activity(self) -> None:
        self.query_one(ThinkingIndicator).advance()
        self.query_one(WelcomeBanner).advance_activity()
        self.query_one(ProgressPanel).update_agent(self.agent)

    def _drive_turn(self, text: str) -> None:
        for event in self.agent.engine.run_turn(text):
            try:
                self.call_from_thread(self._handle_runtime_event, dict(event))
            except RuntimeError:
                return

    def _handle_runtime_event(self, event: dict) -> None:
        event_type = str(event.get("type", ""))
        if event_type == "model_requested":
            self._reset_model_stream()
            attempts = event.get("attempts", 0)
            tool_steps = event.get("tool_steps", 0)
            detail = f"model request {attempts}, tools {tool_steps}"
            self.query_one(ThinkingIndicator).set_detail(detail)
            self.query_one(WelcomeBanner).advance_activity(detail)
            return
        if event_type == "model_delta":
            self._append_model_stream_delta(str(event.get("content", "")))
            detail = f"receiving model output {event.get('total_chars', 0)} chars"
            self.query_one(ThinkingIndicator).set_detail(detail)
            self.query_one(WelcomeBanner).advance_activity(detail)
            return
        if event_type == "model_parsed":
            kind = event.get("kind", "")
            if kind in {"tool", "tools"}:
                self._discard_model_stream()
            detail = f"model returned {kind}"
            self.query_one(ThinkingIndicator).set_detail(detail)
            self.query_one(WelcomeBanner).advance_activity(detail)
            return
        if event_type == "tool_call":
            name = str(event.get("name", ""))
            args = event.get("args") if isinstance(event.get("args"), dict) else {}
            detail = f"running {name}"
            self.query_one(ThinkingIndicator).set_detail(detail)
            self.query_one(WelcomeBanner).advance_activity(detail)
            card = self.query_one(ChatLog).add_tool_call(name, args)
            self._running_tool_cards.append(card)
            return
        if event_type == "tool_result":
            self._finish_tool_card(event)
            self.query_one(ProgressPanel).update_agent(self.agent)
            self.query_one(ThinkingIndicator).set_detail("thinking after tool")
            self.query_one(WelcomeBanner).advance_activity("thinking after tool")
            return
        if event_type == "worker_notification":
            self.query_one(ChatLog).add_message(
                "assistant", f"[worker notification]\n{event.get('content', '')}"
            )
            return
        if event_type in {"assistant_preamble", "retry", "runtime_notice", "final", "stop"}:
            content = str(event.get("content", ""))
            if event_type == "retry" and content.startswith("Your previous response could not be executed."):
                self._queue_retry_notice(content)
            else:
                self._queue_assistant_stream(content)
            return

    def _finish_tool_card(self, event: dict) -> None:
        name = str(event.get("name", ""))
        card = None
        for candidate in reversed(self._running_tool_cards):
            if candidate.tool_name == name and candidate.status == "running":
                card = candidate
                break
        if card is None:
            card = self.query_one(ChatLog).add_tool_call(name, {})
        metadata = (
            event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        )
        content = str(event.get("content", ""))
        status = str(metadata.get("tool_status", "ok") or "ok")
        if status in {"error", "rejected", "partial_success"}:
            card.set_error(content)
        else:
            card.set_success(content)

    def _stop_thinking(self) -> None:
        timer = getattr(self, "_thinking_timer", None)
        if timer is not None:
            timer.stop()
            self._thinking_timer = None
        self.query_one(ThinkingIndicator).hide()
        self.query_one(WelcomeBanner).set_activity(False, "ready")

    def _queue_retry_notice(self, content: str) -> None:
        signature = _retry_signature(content)
        if signature == self._last_retry_signature and self._last_retry_widget is not None:
            self._last_retry_count += 1
            self._last_retry_content = content
            self._last_retry_widget.update_content(f"{content}\n\n(repeated {self._last_retry_count} times)")
            return
        self._last_retry_signature = signature
        self._last_retry_content = content
        self._last_retry_count = 1
        self._last_retry_widget = self.query_one(ChatLog).add_message("assistant", content)

    def _queue_assistant_stream(self, content: str) -> None:
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
            widget.update_content(content)
            self.query_one(ChatLog).scroll_end(animate=False)
            return
        self.query_one(ChatLog).add_message("assistant", content)

    def _append_model_stream_delta(self, delta: str) -> None:
        if not delta:
            return
        self._model_stream_content += delta
        preview = _model_stream_preview(self._model_stream_content)
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
        prompt = ConfirmPrompt(name, format_tool_args(name, args))
        self._confirm_prompt = prompt
        self._confirm_decision = (event, decision)
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


def _model_stream_preview(content: str) -> str:
    text = str(content or "")
    marker = "<final>"
    if marker in text:
        body = text.split(marker, 1)[1]
        if "</final>" in body:
            body = body.split("</final>", 1)[0]
        return body
    if "<tool" in text:
        return ""
    return text
