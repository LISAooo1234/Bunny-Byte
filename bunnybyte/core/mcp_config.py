"""[mcp.servers.<name>] 配置：解析、校验，以及仓库自带服务器的信任闸门（和项目钩子同一套规矩）。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from .hooks import load_trust_store, trust_project_hooks

TRUST_ENV = "BUNNYBYTE_TRUST_MCP"
# 启动时读一次：仓库的 .env 在这之后才加载，所以克隆来的仓库没法靠 .env 给自己授信
STARTUP_TRUST_ENV = os.environ.get(TRUST_ENV, "").strip().lower() in {"1", "true", "yes"}
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")  # 不许出现 "__"：它是 mcp__<服务器>__<工具> 的分隔符
ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
KNOWN_KEYS = {"command", "args", "env", "cwd", "timeout", "startup_timeout", "read_only_tools", "enabled"}


@dataclass(frozen=True)
class McpServerSpec:
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    cwd: str = ""
    timeout: float = 60.0
    startup_timeout: float = 10.0
    read_only_tools: tuple[str, ...] = ()
    source: str = "global"

    def identity(self):
        return {
            "name": self.name, "command": self.command, "args": list(self.args), "env": [list(item) for item in self.env],
            "cwd": self.cwd, "read_only_tools": list(self.read_only_tools),
        }

    def child_env(self, allowlist, environ=None):
        """子进程只拿到 run_shell 同一份白名单里的变量 + 配置里显式写的 env，shell 里的 API key 不会顺带泄给服务器。"""
        environ = os.environ if environ is None else environ
        env = {name: environ[name] for name in allowlist if name in environ and name != "PWD"}
        for key, value in self.env:
            env[key] = ENV_REF_RE.sub(lambda match: environ.get(match.group(1), ""), value)
        return env


@dataclass(frozen=True)
class McpConfig:
    servers: tuple[McpServerSpec, ...] = ()
    pending: tuple[McpServerSpec, ...] = ()
    problems: tuple[str, ...] = ()
    project_config: str = ""
    project_fingerprint: str = ""

    def __bool__(self):
        return bool(self.servers or self.pending or self.problems)

    def with_pending_trusted(self):
        return replace(self, servers=self.servers + self.pending, pending=())


@dataclass
class _Parsed:
    specs: list = field(default_factory=list)
    problems: list = field(default_factory=list)


def parse_servers(raw, *, source):
    parsed = _Parsed()
    if not raw:
        return parsed
    servers = raw.get("servers") if isinstance(raw, dict) else None
    if not isinstance(servers, dict):
        parsed.problems.append(f"[mcp] 需要写成 [mcp.servers.<名字>] 表（{source}）")
        return parsed
    for name, entry in servers.items():
        problem = _check_entry(str(name), entry)
        if problem:
            parsed.problems.append(f"mcp.servers.{name}：{problem}")
            continue
        if entry.get("enabled", True) is False:
            continue
        unknown = sorted(set(entry) - KNOWN_KEYS)
        if unknown:
            parsed.problems.append(f"mcp.servers.{name}：忽略未知字段 {', '.join(unknown)}")
        parsed.specs.append(
            McpServerSpec(
                name=str(name),
                command=str(entry["command"]),
                args=tuple(str(item) for item in entry.get("args", [])),
                env=tuple(sorted((str(key), str(value)) for key, value in (entry.get("env") or {}).items())),
                cwd=str(entry.get("cwd", "") or ""),
                timeout=float(entry.get("timeout", 60)),
                startup_timeout=float(entry.get("startup_timeout", 10)),
                read_only_tools=tuple(str(item) for item in entry.get("read_only_tools", [])),
                source=source,
            )
        )
    return parsed


def _check_entry(name, entry):
    if not NAME_RE.match(name) or "__" in name:
        return "名字只能用字母、数字、_ 和 -（最多 32 个字符，不能含连续两个 _）"
    if not isinstance(entry, dict):
        return "需要是一个表"
    if not isinstance(entry.get("command"), str) or not entry["command"].strip():
        return "缺少 command"
    if not isinstance(entry.get("args", []), list):
        return "args 需要是字符串列表"
    if not isinstance(entry.get("env", {}), dict):
        return "env 需要是表"
    if not isinstance(entry.get("read_only_tools", []), list):
        return "read_only_tools 需要是字符串列表"
    for key in ("timeout", "startup_timeout"):
        value = entry.get(key, 1)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 3600:
            return f"{key} 需要是 0–3600 之间的秒数"
    return ""


def fingerprint(specs):
    payload = json.dumps([spec.identity() for spec in specs], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def trust_store_path():
    home = os.environ.get("BUNNYBYTE_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".bunnybyte"
    return base / "trusted_mcp.json"


def trust_project_servers(config, path=None):
    trust_project_hooks(config.project_config, config.project_fingerprint, path or trust_store_path())


def load_mcp_config(global_raw, project_raw, project_config=None, *, trust_path=None):
    """合并全局与项目服务器。项目里的服务器要被信任才会启动，否则放进 pending；同名时全局优先。"""
    global_parsed = parse_servers(global_raw, source="global")
    project_parsed = parse_servers(project_raw, source="project")
    problems = global_parsed.problems + project_parsed.problems
    global_names = {spec.name for spec in global_parsed.specs}
    project_specs = []
    for spec in project_parsed.specs:
        if spec.name in global_names:
            problems.append(f"项目里的 MCP 服务器 {spec.name} 和全局配置同名，使用全局的")
            continue
        project_specs.append(spec)
    digest = fingerprint(project_specs) if project_specs else ""
    trusted = bool(project_specs) and (
        STARTUP_TRUST_ENV or load_trust_store(trust_path or trust_store_path()).get(str(project_config)) == digest
    )
    return McpConfig(
        servers=tuple(global_parsed.specs) + (tuple(project_specs) if trusted else ()),
        pending=() if trusted else tuple(project_specs),
        problems=tuple(problems),
        project_config=str(project_config or ""),
        project_fingerprint=digest,
    )
