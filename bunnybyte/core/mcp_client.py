"""最小的 MCP stdio 客户端：按行分隔的 JSON-RPC 2.0，只用 initialize / tools/list / tools/call。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "bunnybyte", "version": "0"}
MAX_TOOL_PAGES = 20
ABORT_POLL_SECONDS = 0.1


class McpError(RuntimeError):
    pass


class McpServer:
    """一个 MCP 子进程连接。所有方法都可以从多个线程调用；写 stdin 有锁，响应按 id 分发。"""

    def __init__(self, name, command, args=(), *, env=None, cwd=None, log_path=None):
        self.name = name
        self.command = command
        self.args = list(args)
        self.env = dict(env or {})
        self.cwd = cwd
        self.log_path = Path(log_path) if log_path else None
        self.process = None
        self.server_info = {}
        self._next_id = 0
        self._pending = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._reader = None
        self._closed = False
        self._log_handle = None

    @property
    def alive(self):
        return self.process is not None and self.process.poll() is None and not self._closed

    def start(self, timeout=20.0):
        stderr = subprocess.DEVNULL
        if self.log_path is not None:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                self._log_handle = self.log_path.open("ab")
                stderr = self._log_handle
            except OSError:
                self.log_path = None
        try:
            self.process = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
                cwd=self.cwd,
                env=self.env,
                start_new_session=True,  # 自己的进程组，关闭时连同它拉起的子进程一起收掉
            )
        except OSError as exc:
            self._close_log()
            raise McpError(f"无法启动 `{self.command}`：{exc}") from exc
        self._reader = threading.Thread(target=self._read_loop, name=f"mcp-{self.name}", daemon=True)
        self._reader.start()
        try:
            result = self.request(
                "initialize",
                {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
                timeout=timeout,
            )
        except McpError:
            self.close()
            raise
        self.server_info = dict(result.get("serverInfo") or {})
        self.notify("notifications/initialized")
        return result

    def list_tools(self, timeout=20.0):
        tools, cursor = [], None
        for _ in range(MAX_TOOL_PAGES):
            params = {"cursor": cursor} if cursor else {}
            result = self.request("tools/list", params, timeout=timeout)
            tools.extend(item for item in result.get("tools") or [] if isinstance(item, dict) and item.get("name"))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name, arguments, *, timeout=60.0, should_abort=None):
        return self.request(
            "tools/call", {"name": name, "arguments": dict(arguments or {})}, timeout=timeout, should_abort=should_abort
        )

    def notify(self, method, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def request(self, method, params=None, *, timeout=20.0, should_abort=None):
        if not self.alive:
            raise McpError(self._dead_message())
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            slot = {"event": threading.Event(), "response": None}
            self._pending[request_id] = slot
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
            deadline = time.monotonic() + float(timeout)
            while not slot["event"].wait(ABORT_POLL_SECONDS):
                if should_abort is not None and should_abort():
                    self._cancel(request_id, "用户中止")
                    raise McpError(f"{method} 已中止")
                if not self.alive:
                    raise McpError(self._dead_message())
                if time.monotonic() >= deadline:
                    self._cancel(request_id, "timeout")
                    raise McpError(f"{method} 超时（{timeout:g} 秒）")
        finally:
            with self._lock:
                self._pending.pop(request_id, None)
        response = slot["response"]
        if response is None:  # 读线程在进程退出时唤醒了所有等待者
            raise McpError(self._dead_message())
        if "error" in response:
            error = response.get("error") or {}
            raise McpError(f"{method} 失败：{error.get('message', error)}")
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def close(self, grace=1.0):
        if self._closed:
            return
        self._closed = True
        process = self.process
        if process is not None:
            if process.poll() is None:
                try:
                    process.stdin.close()
                    process.wait(timeout=grace)
                except (OSError, subprocess.TimeoutExpired, ValueError):
                    pass
            _kill_group(process)  # 服务器自己退了，它拉起的子进程也可能还在：整个进程组一起收
        self._wake_pending()
        self._close_log()

    def _send(self, message):
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                self.process.stdin.write(data)
                self.process.stdin.flush()
            except (OSError, ValueError, AttributeError) as exc:
                raise McpError(f"{self._dead_message()}（{exc}）") from exc

    def _cancel(self, request_id, reason):
        try:
            self.notify("notifications/cancelled", {"requestId": request_id, "reason": reason})
        except McpError:
            pass

    def _read_loop(self):
        stream = self.process.stdout
        for raw in iter(stream.readline, b""):
            try:
                message = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue  # 有的服务器会往 stdout 打日志，跳过非 JSON 行
            if isinstance(message, dict):
                self._dispatch(message)
        self._wake_pending()

    def _dispatch(self, message):
        if "method" in message:
            if "id" in message:  # 服务器发来的请求：只回 ping，其余能力（sampling、roots…）不支持
                reply = {"jsonrpc": "2.0", "id": message["id"]}
                if message["method"] == "ping":
                    reply["result"] = {}
                else:
                    reply["error"] = {"code": -32601, "message": "method not supported by client"}
                try:
                    self._send(reply)
                except McpError:
                    pass
            return
        request_id = message.get("id")
        if not isinstance(request_id, (int, str)):
            return
        with self._lock:
            slot = self._pending.get(request_id)
        if slot is not None:
            slot["response"] = message
            slot["event"].set()

    def _wake_pending(self):
        with self._lock:
            slots = list(self._pending.values())
        for slot in slots:
            slot["event"].set()

    def _dead_message(self):
        code = self.process.poll() if self.process is not None else None
        suffix = f"，退出码 {code}" if code is not None else ""
        hint = f"，日志 {self.log_path}" if self.log_path else ""
        return f"MCP 服务器 {self.name} 已断开{suffix}{hint}"

    def _close_log(self):
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None


def _kill_group(process):
    """先 SIGTERM 整个进程组给它们收尾的机会，1 秒后 SIGKILL 还活着的。进程组已经空了就什么也不做。"""
    for sig, wait in ((signal.SIGTERM, 1.0), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(process.pid, sig)
        except (OSError, ProcessLookupError):
            return
        try:
            process.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            continue
        if sig == signal.SIGTERM:
            time.sleep(0.05)  # 组长退了，组里别的进程可能还没处理完 SIGTERM；下一轮 SIGKILL 兜底
