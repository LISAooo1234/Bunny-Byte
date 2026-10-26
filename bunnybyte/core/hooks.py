"""钩子配置：`[[hooks.pre_tool]]` / `[[hooks.post_tool]]` / `[[hooks.on_final]]` 以及项目钩子的信任记录。

这里只处理“配置长什么样、谁说了算”：解析与校验、项目钩子的信任指纹、信任记录文件。
执行钩子的子进程逻辑在 hook_runner.py，回合收尾时的 on_final 闸门在 hook_gate.py。

信任规则：全局配置（~/.config/bunnybyte/config.toml）和 `--config` 指定的文件由用户自己写，直接生效；
仓库里的 `.bunnybyte.toml` 可能来自别人，里面的钩子在用户确认前一律不运行（见 trust_project_hooks）。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from pathlib import Path

EVENTS = ("pre_tool", "post_tool", "on_final")
DEFAULT_TIMEOUT = 10
MAX_TIMEOUT = 300
DEFAULT_FINAL_ROUNDS = 2
MAX_FINAL_ROUNDS = 5
ON_ERROR_MODES = ("allow", "block")
TRUST_ENV = "BUNNYBYTE_TRUST_HOOKS"
# 进程启动时就读下来：之后 load_project_env 会把仓库自带的 .env 灌进 os.environ，不能让克隆来的仓库给自己授信
STARTUP_TRUST_ENV = os.environ.get(TRUST_ENV, "").strip().lower() in {"1", "true", "yes"}
KNOWN_ENTRY_KEYS = {"command", "match", "timeout", "on_error", "only_if_changed"}


@dataclass(frozen=True)
class HookSpec:
    event: str
    command: str
    match: tuple[str, ...] = ("*",)
    timeout: int = DEFAULT_TIMEOUT
    on_error: str = "allow"
    source: str = "global"  # global | project
    only_if_changed: bool = False  # 仅 on_final：这一轮没改过文件就不跑（纯问答不必每次跑测试）

    def matches(self, tool_name):
        return any(fnmatchcase(str(tool_name), pattern) for pattern in self.match)

    def identity(self):
        return [self.event, self.command, list(self.match), self.timeout, self.on_error, self.only_if_changed]


@dataclass(frozen=True)
class HookSet:
    specs: tuple[HookSpec, ...] = ()
    pending: tuple[HookSpec, ...] = ()  # 项目里写了、但还没被信任所以不会运行的钩子
    problems: tuple[str, ...] = ()
    max_final_rounds: int = DEFAULT_FINAL_ROUNDS
    project_config: str = ""
    project_fingerprint: str = ""

    def for_event(self, event, tool_name=""):
        return [
            spec
            for spec in self.specs
            if spec.event == event and (event == "on_final" or spec.matches(tool_name))
        ]

    def has(self, event):
        return any(spec.event == event for spec in self.specs)

    def with_pending_trusted(self):
        return replace(self, specs=self.specs + self.pending, pending=())

    def __bool__(self):
        return bool(self.specs or self.pending or self.problems)


def parse_hooks(raw, *, source):
    """把 `hooks` 表解析成 (specs, problems, max_final_rounds)。写错的条目会被跳过并说明原因，不会让启动失败。"""
    specs: list[HookSpec] = []
    problems: list[str] = []
    rounds = DEFAULT_FINAL_ROUNDS
    if not isinstance(raw, dict):
        return specs, ["[hooks] 必须是一个表"], rounds
    for key, value in raw.items():
        if key == "max_final_rounds":
            rounds, problem = _parse_rounds(value)
            if problem:
                problems.append(problem)
        elif key in EVENTS:
            entries = value if isinstance(value, list) else [value]
            for index, entry in enumerate(entries, 1):
                spec, problem = _parse_entry(key, index, entry, source)
                if problem:
                    problems.append(problem)
                if spec:
                    specs.append(spec)
        else:
            problems.append(f"[hooks.{key}] 不是已知事件（可用：{', '.join(EVENTS)}）")
    return specs, problems, rounds


def _parse_rounds(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_FINAL_ROUNDS:
        return DEFAULT_FINAL_ROUNDS, f"hooks.max_final_rounds 需要 0 到 {MAX_FINAL_ROUNDS} 的整数，已按 {DEFAULT_FINAL_ROUNDS} 处理"
    return value, ""


def _parse_entry(event, index, entry, source):
    label = f"[[hooks.{event}]] 第 {index} 条"
    if not isinstance(entry, dict):
        return None, f"{label}必须是一个表"
    unknown = sorted(set(entry) - KNOWN_ENTRY_KEYS)
    if unknown:
        return None, f"{label}有不认识的字段：{', '.join(unknown)}（可用：{', '.join(sorted(KNOWN_ENTRY_KEYS))}），已跳过"
    command = entry.get("command")
    if not isinstance(command, str) or not command.strip():
        return None, f"{label}缺少 command，已跳过"
    timeout = entry.get("timeout", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= MAX_TIMEOUT:
        return None, f"{label}的 timeout 需要 1 到 {MAX_TIMEOUT} 秒的整数，已跳过"
    on_error = entry.get("on_error", "allow")
    if on_error not in ON_ERROR_MODES:
        return None, f"{label}的 on_error 只能是 {' 或 '.join(ON_ERROR_MODES)}，已跳过"
    match = _parse_match(entry.get("match", "*"))
    if match is None:
        return None, f"{label}的 match 需要字符串（用 | 分隔多个工具名，支持 * 通配），已跳过"
    only_if_changed = entry.get("only_if_changed", False)
    if not isinstance(only_if_changed, bool) or (only_if_changed and event != "on_final"):
        return None, f"{label}的 only_if_changed 只能是 true/false，并且只用于 on_final，已跳过"
    return HookSpec(event, command.strip(), match, timeout, on_error, source, only_if_changed), ""


def _parse_match(value):
    if isinstance(value, str):
        parts = [part.strip() for part in value.replace(",", "|").split("|")]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        parts = [item.strip() for item in value]
    else:
        return None
    parts = tuple(part for part in parts if part)
    return parts or None


def fingerprint(specs, rounds=None):
    payload = json.dumps(
        {"hooks": [spec.identity() for spec in specs], "max_final_rounds": rounds}, ensure_ascii=False, sort_keys=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def trust_store_path():
    home = os.environ.get("BUNNYBYTE_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".bunnybyte"
    return base / "trusted_hooks.json"


def load_trust_store(path=None):
    try:
        data = json.loads(Path(path or trust_store_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _is_trusted(project_config, digest, path):
    if STARTUP_TRUST_ENV:
        return True
    return load_trust_store(path).get(str(project_config)) == digest


def trust_project_hooks(project_config, digest, path=None):
    """把当前项目钩子的指纹（HookSet.project_fingerprint）记下来；之后钩子配置一变，信任就失效。"""
    target = Path(path or trust_store_path())
    store = load_trust_store(target)
    store[str(project_config)] = digest
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=".trusted_hooks.", dir=target.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(store, stream, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(temp_name, target)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def load_hooks(global_raw, project_raw, project_config=None, *, trust_path=None):
    """合并全局与项目钩子。项目钩子只有在被信任时才进入 specs，否则放在 pending 里等用户确认。"""
    global_specs, problems, rounds = parse_hooks(global_raw or {}, source="global")
    project_specs: list[HookSpec] = []
    digest = ""
    project_rounds = None
    if project_raw:
        project_specs, project_problems, parsed_rounds = parse_hooks(project_raw, source="project")
        problems += project_problems
        if isinstance(project_raw, dict) and "max_final_rounds" in project_raw:
            project_rounds = parsed_rounds
        digest = fingerprint(project_specs, project_rounds) if project_specs else ""
    trusted = bool(project_specs) and _is_trusted(project_config, digest, trust_path)
    if trusted and project_rounds is not None:
        rounds = project_rounds  # 项目写的轮次上限只在它的钩子被信任之后才算数，否则克隆来的仓库能关掉用户自己的全局闸门
    return HookSet(
        specs=tuple(global_specs) + (tuple(project_specs) if trusted else ()),
        pending=() if trusted else tuple(project_specs),
        problems=tuple(problems),
        max_final_rounds=rounds,
        project_config=str(project_config or ""),
        project_fingerprint=digest,
    )
