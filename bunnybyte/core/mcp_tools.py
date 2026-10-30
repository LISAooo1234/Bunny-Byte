"""把 MCP 服务器的工具注册成 Bunny Byte 的工具：mcp__<服务器>__<工具>，默认需要审批、前后有工作区快照。"""

from __future__ import annotations

import atexit
import json
import re
import threading
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from ..tools.base import RegisteredTool
from .mcp_client import McpError, McpServer

TOOL_PREFIX = "mcp__"
MAX_TOOL_NAME = 64
MAX_DESCRIPTION = 300  # 工具描述进 prompt 前缀，一个 40 个工具的服务器不该挤掉规则和工作区信息
MAX_TOOLS_PER_SERVER = 40
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_-]")


@dataclass
class ServerState:
    spec: object
    status: str = "stopped"  # running / failed / stopped
    error: str = ""
    tools: list = field(default_factory=list)  # 服务器原样返回的工具定义
    client: McpServer | None = None
    exposed: dict = field(default_factory=dict)  # 暴露给模型的名字 -> 服务器里的工具名


class McpManager:
    """一个进程里所有 MCP 连接；主 agent 和 worker 共用。"""

    def __init__(self, config, root, *, env_allowlist=(), log_dir=None, server_factory=McpServer):
        self.config = config
        self.env_allowlist = tuple(env_allowlist)
        self.root = Path(root)
        self.log_dir = Path(log_dir) if log_dir else self.root / ".bunnybyte" / "mcp"
        self.server_factory = server_factory
        self.states: dict[str, ServerState] = {}
        self._closed = False
        atexit.register(self.close)

    def start(self, specs=None):
        """并行启动服务器并列出工具。失败的服务器记下原因，不抛异常，agent 照常能用。"""
        specs = list(self.config.servers if specs is None else specs)
        threads = []
        for spec in specs:
            state = ServerState(spec=spec)
            self.states[spec.name] = state
            thread = threading.Thread(target=self._start_one, args=(state,), daemon=True)
            thread.start()
            threads.append((thread, state))
        for thread, state in threads:
            thread.join(state.spec.startup_timeout * 2 + 5)
            if thread.is_alive() and state.status != "running":
                state.status, state.error = "failed", "启动超时"
        return [state for _, state in threads]

    def _start_one(self, state):
        spec = state.spec
        cwd = Path(spec.cwd).expanduser() if spec.cwd else self.root
        if not cwd.is_absolute():
            cwd = self.root / cwd
        client = self.server_factory(
            spec.name, spec.command, spec.args, env=spec.child_env(self.env_allowlist), cwd=str(cwd),
            log_path=self.log_dir / f"{spec.name}.log",
        )
        try:
            client.start(timeout=spec.startup_timeout)
            tools = client.list_tools(timeout=spec.startup_timeout)
        except (McpError, OSError) as exc:
            client.close()
            state.status, state.error = "failed", str(exc)
            return
        if state.status == "failed" or self._closed:  # 主线程已经按启动超时放弃了它
            client.close()
            return
        state.tools, state.client, state.status = tools, client, "running"

    def running(self):
        return [state for state in self.states.values() if state.status == "running"]

    def failures(self):
        return [state for state in self.states.values() if state.status == "failed"]

    def close(self):
        if self._closed:
            return
        self._closed = True
        for state in self.states.values():
            if state.client is not None:
                state.client.close()
                state.status = "stopped" if state.status == "running" else state.status


def register_mcp_tools(agent, manager):
    """把已启动服务器的工具加进 agent.tools，重建工具档位并刷新 prompt 前缀。返回新增的工具名。"""
    agent.mcp = manager
    added = []
    taken = set(agent.tools)
    # 按服务器名、工具名排序注册：前缀里的工具顺序不随哪个服务器先启动完而变，prompt 缓存才能命中
    for state in sorted(manager.running(), key=lambda item: item.spec.name):
        read_only = set(state.spec.read_only_tools)
        tools = sorted(state.tools, key=lambda item: str(item.get("name", "")))
        if len(tools) > MAX_TOOLS_PER_SERVER:
            state.error = f"服务器提供了 {len(tools)} 个工具，只注册前 {MAX_TOOLS_PER_SERVER} 个"
            tools = tools[:MAX_TOOLS_PER_SERVER]
        for tool in tools:
            remote = str(tool.get("name", ""))
            name = state.exposed.get(remote) or _unique_name(state.spec.name, remote, taken)
            state.exposed[remote] = name
            taken.add(name)
            safe = "*" in read_only or remote in read_only
            input_schema = native_input_schema(tool.get("inputSchema"))
            agent.tools[name] = RegisteredTool(
                name=name,
                schema=compact_schema(input_schema),
                description=_description(tool),
                risky=not safe,
                runner=partial(_call_tool, agent, state, remote),
                parallel_safe=safe,
                json_schema=input_schema,
            )
            added.append(name)
    if added:
        from .tool_profiles import build_tool_profiles

        agent.tool_profiles = build_tool_profiles(agent.tools)
        agent.refresh_prefix(force=True)
    return added


def _call_tool(agent, state, remote, args):
    client = state.client
    if client is None or not client.alive:
        raise McpError(f"MCP 服务器 {state.spec.name} 不可用：{state.error or '已断开'}")
    result = client.call_tool(
        remote, args, timeout=state.spec.timeout, should_abort=lambda: bool(getattr(agent, "abort_requested", False))
    )
    text = render_result(result)
    if result.get("isError"):
        raise McpError(text)
    return text


def render_result(result):
    parts = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind == "resource":
            resource = item.get("resource") or {}
            if "text" in resource:
                parts.append(f"[资源 {resource.get('uri', '')}]\n{resource['text']}")
            else:
                parts.append(f"[二进制资源 {resource.get('uri', '')}，未显示]")
        elif kind == "resource_link":
            parts.append(f"[资源链接 {item.get('uri', '')}] {item.get('name', '')}".rstrip())
        elif kind in {"image", "audio"}:
            size = len(str(item.get("data", ""))) * 3 // 4
            parts.append(f"[{kind} {item.get('mimeType', '')}，约 {size} 字节，未显示]")
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False, indent=2))
    return "\n".join(parts).strip() or "(no output)"


def native_input_schema(schema):
    """把服务器的 JSON Schema 收拾成各家 provider 都收的样子；整份工具列表每次请求都发，一个坏 schema 会让所有回合报错。

    顶层必须是 object 且不能有 oneOf/anyOf/allOf（Anthropic 拒收），数组必须有 items（OpenAI 拒收），
    $schema / $ref / $defs 这类引用去掉；收拾不了的属性降级成不限类型的 {}。
    """
    schema = dict(schema) if isinstance(schema, dict) else {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    cleaned = {"type": "object", "properties": {str(key): _clean_property(value, 0) for key, value in properties.items()}}
    required = [str(item) for item in schema.get("required") or [] if isinstance(schema.get("required"), list) and str(item) in cleaned["properties"]]
    if required:
        cleaned["required"] = required
    if isinstance(schema.get("description"), str):
        cleaned["description"] = schema["description"]
    return cleaned


_DROPPED_KEYS = {"$schema", "$ref", "$defs", "definitions", "$id", "$comment"}
_COMBINATORS = ("oneOf", "anyOf", "allOf")


def _clean_property(spec, depth):
    if not isinstance(spec, dict) or depth > 6:
        return {}
    if "$ref" in spec:
        return {"description": str(spec.get("description", ""))} if spec.get("description") else {}
    cleaned = {}
    for key, value in spec.items():
        if key in _DROPPED_KEYS:
            continue
        if key in _COMBINATORS:
            options = [_clean_property(item, depth + 1) for item in value] if isinstance(value, list) else []
            if options and all(options):
                cleaned[key] = options
            # 有一个分支收拾成了 {}（不限类型）就整个去掉这个约束，不能悄悄把类型收窄
        elif key == "properties" and isinstance(value, dict):
            cleaned[key] = {str(name): _clean_property(item, depth + 1) for name, item in value.items()}
        elif key == "items":
            cleaned[key] = _clean_property(value, depth + 1) if isinstance(value, dict) else {}
        else:
            cleaned[key] = value
    kinds = cleaned.get("type") if isinstance(cleaned.get("type"), list) else [cleaned.get("type")]
    if "array" in kinds and "items" not in cleaned:
        cleaned["items"] = {}
    return cleaned


def validate_mcp_args(agent, name, args):
    """必填参数要在；文本协议（XML 属性）传来的都是字符串，按 schema 把数字 / 布尔值转回去。"""
    tool = agent.tools.get(name)
    schema = getattr(tool, "json_schema", None) or {}
    properties = schema.get("properties") or {}
    missing = [key for key in schema.get("required") or [] if key not in args]
    if missing:
        raise ValueError(f"missing required argument(s): {', '.join(missing)}")
    for key, value in list(args.items()):
        spec = properties.get(key)
        if isinstance(spec, dict) and isinstance(value, str):
            args[key] = _coerce(value, spec.get("type"), key)


def _coerce(value, kind, key):
    kinds = kind if isinstance(kind, list) else [kind]
    if "string" in kinds or not kind:
        return value
    text = value.strip()
    if "integer" in kinds and re.fullmatch(r"-?\d+", text):
        return int(text)
    if "number" in kinds and re.fullmatch(r"-?\d+(\.\d+)?([eE][-+]?\d+)?", text):
        return float(text)
    if "boolean" in kinds and text.lower() in {"true", "false"}:
        return text.lower() == "true"
    if ("array" in kinds or "object" in kinds) and text[:1] in "[{":
        try:
            return json.loads(text)
        except ValueError:
            pass
    raise ValueError(f"argument {key} should be {'/'.join(str(item) for item in kinds)}, got {value!r}")


def compact_schema(schema):
    """文本协议 prompt 里那一行用的简写：path: str, depth: int?"""
    required = set(schema.get("required") or [])
    compact = {}
    for key, spec in (schema.get("properties") or {}).items():
        type_name = _compact_type(spec if isinstance(spec, dict) else {})
        compact[str(key)] = type_name if key in required else f"{type_name}?"
    return compact


def _compact_type(spec):
    kind = spec.get("type")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), "str")
    if kind == "array":
        item_type = _compact_type(spec.get("items") if isinstance(spec.get("items"), dict) else {})
        return f"list[{item_type}]"
    return {"string": "str", "integer": "int", "number": "float", "boolean": "bool", "object": "dict"}.get(kind, "str")


def _description(tool):
    text = " ".join(str(tool.get("description") or tool.get("title") or "").split())
    if len(text) > MAX_DESCRIPTION:
        text = text[: MAX_DESCRIPTION - 1] + "…"
    return f"[MCP] {text}".rstrip()


def _unique_name(server, remote, taken):
    base = f"{TOOL_PREFIX}{server}__{_UNSAFE_NAME.sub('_', remote) or 'tool'}"[:MAX_TOOL_NAME]
    name, index = base, 2
    while name in taken:
        suffix = f"_{index}"
        name = base[: MAX_TOOL_NAME - len(suffix)] + suffix
        index += 1
    return name
