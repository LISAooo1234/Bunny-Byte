"""`bunny doctor`: environment self-check with actionable fixes.

每一项检查都返回一个 DoctorCheck，失败时附带可直接执行的修复建议。
检查过程不会打印 API key，只报告“是否已配置”和脱敏后的指纹。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import (
    DEFAULT_CONFIG_PATH,
    find_project_config,
    resolve_project_sandbox_config,
    resolve_provider_config,
)
from .config.fallback import resolve_fallback_configs, resolve_fallback_names
from .core.project_rules import load_project_rules
from .features.sandbox.checker import SandboxChecker
from .providers.base import complete_model
from .providers.errors import ProviderError, sanitize_url

STATUS_ICONS = {"ok": "✓", "warn": "!", "fail": "✗", "skip": "-"}
PING_PROMPT = "Reply with exactly: pong"


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    detail: str
    hint: str = ""


def mask_secret(value):
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return f"…{text[-4:]}"


def check_python(version_info=None):
    version = version_info or sys.version_info
    text = f"{version[0]}.{version[1]}.{version[2]}"
    if tuple(version[:2]) < (3, 10):
        return DoctorCheck("Python", "fail", text, "需要 Python 3.10 及以上版本。")
    return DoctorCheck("Python", "ok", text)


def check_config_files(start, config_path=None):
    checks = []
    paths = (
        [("指定配置", Path(config_path).expanduser())]
        if config_path
        else [("全局配置", DEFAULT_CONFIG_PATH), ("项目配置", find_project_config(start))]
    )
    found = False
    for label, path in paths:
        if path is None or not Path(path).exists():
            checks.append(DoctorCheck(label, "skip", "未找到"))
            continue
        found = True
        checks.append(DoctorCheck(label, "ok", str(path)))
    if not found:
        checks.append(
            DoctorCheck(
                "配置文件",
                "warn",
                "没有任何 BunnyByte 配置文件，只能依赖环境变量。",
                "运行 `bunny setup` 创建全局配置，或复制 .bunnybyte.toml.example 为 .bunnybyte.toml。",
            )
        )
    return checks


def check_provider(start, provider=None, config_path=None):
    """Resolve the provider profile; returns (checks, config or None)."""
    try:
        config = resolve_provider_config(provider, start=start, config_path=config_path)
    except ValueError as exc:
        return [DoctorCheck("Provider", "fail", str(exc), "检查配置文件中的 provider / protocol 字段。")], None
    checks = [
        DoctorCheck("Provider", "ok", f"{config.name} (protocol={config.protocol})"),
        DoctorCheck("模型", "ok" if config.model else "fail", config.model or "未配置", "" if config.model else "在 provider 配置中设置 model。"),
        DoctorCheck(
            "Base URL",
            "ok" if config.base_url else "fail",
            sanitize_url(config.base_url) or "未配置",
            "" if config.base_url else "在 provider 配置中设置 base_url。",
        ),
    ]
    if config.api_key:
        checks.append(DoctorCheck("API key", "ok", f"已配置 ({mask_secret(config.api_key)})"))
    else:
        checks.append(
            DoctorCheck(
                "API key",
                "fail",
                "未配置",
                "运行 `bunny setup`，或设置环境变量 BUNNYBYTE_API_KEY。",
            )
        )
    return checks, config


def check_fallback(start, config, config_path=None):
    """报告备用 provider 链；没有配置时不输出任何检查项。"""
    if config is None:
        return []
    names = resolve_fallback_names(config.name, start=start, config_path=config_path)
    if not names:
        return []
    usable = resolve_fallback_configs(config.name, start=start, config_path=config_path)
    usable_names = {item.name for item in usable}
    skipped = [name for name in names if name not in usable_names]
    detail = " -> ".join(f"{item.name} ({item.model})" for item in usable) or "无可用备用"
    if skipped:
        return [
            DoctorCheck(
                "备用 Provider",
                "warn",
                f"{detail}；已忽略配置不完整的：{', '.join(skipped)}",
                "为这些 provider 配置 api_key / base_url / model，否则故障切换不会使用它们。",
            )
        ]
    return [DoctorCheck("备用 Provider", "ok", detail)]


def check_connectivity(config, client_factory):
    """Send one tiny request to the provider and report latency."""
    if config is None or not config.api_key or not config.base_url or not config.model:
        return DoctorCheck("连通性", "skip", "provider 配置不完整，跳过网络检查。")
    started = time.monotonic()
    try:
        client = client_factory(config)
        result = complete_model(client, PING_PROMPT, 16)
    except ProviderError as exc:
        return DoctorCheck("连通性", "fail", _provider_error_detail(exc), _provider_error_hint(exc))
    except Exception as exc:  # noqa: BLE001 - doctor must never crash
        return DoctorCheck("连通性", "fail", f"{type(exc).__name__}: {exc}", "检查网络、代理或 base_url。")
    elapsed_ms = int((time.monotonic() - started) * 1000)
    reply = " ".join(str(result.text or "").split())[:40]
    detail = f"{elapsed_ms} ms，模型回复：{reply or '(空)'}"
    if not reply:
        return DoctorCheck("连通性", "warn", detail, "接口可达但返回为空，确认模型名是否正确。")
    return DoctorCheck("连通性", "ok", detail)


def _provider_error_detail(exc):
    status = f"HTTP {exc.http_status} " if exc.http_status is not None else ""
    return f"{status}{exc.code}: {str(exc)[:160]}"


def _provider_error_hint(exc):
    if exc.http_status in {401, 403}:
        return "API key 无效或无权限，重新运行 `bunny setup` 更新 key。"
    if exc.http_status == 404:
        return "接口路径或模型名不存在，检查 base_url 与 model。"
    if exc.http_status == 429:
        return "触发限流或余额不足，稍后重试或检查账户额度。"
    if exc.http_status is not None and exc.http_status >= 500:
        return "服务端错误，通常是 provider 临时故障，稍后重试。"
    return "检查网络、代理设置和 base_url 是否可访问。"


def check_workspace(start, run=subprocess.run):
    root = Path(start).resolve()
    checks = []
    git_root = _git(run, root, ["rev-parse", "--show-toplevel"])
    if git_root:
        checks.append(DoctorCheck("Git 仓库", "ok", git_root))
    else:
        checks.append(
            DoctorCheck(
                "Git 仓库",
                "warn",
                "当前目录不是 git 仓库",
                "建议在 git 仓库中使用，便于审查和回滚 agent 的改动：`git init`。",
            )
        )
    data_dir = Path(git_root or root) / ".bunnybyte"
    writable_base = data_dir if data_dir.exists() else data_dir.parent
    if _is_writable(writable_base):
        checks.append(DoctorCheck("运行数据目录", "ok", str(data_dir)))
    else:
        checks.append(DoctorCheck("运行数据目录", "fail", f"{writable_base} 不可写", "检查目录权限。"))
    checks.extend(_secret_exposure_checks(run, Path(git_root) if git_root else None))
    return checks


def check_project_rules(start, run=subprocess.run):
    cwd = Path(start).resolve()
    root = Path(_git(run, cwd, ["rev-parse", "--show-toplevel"]) or cwd)
    if not root.is_dir():
        root = cwd
    rules = load_project_rules(root, cwd)
    if not rules:
        return DoctorCheck(
            "项目规则",
            "skip",
            "未找到 AGENTS.md / CLAUDE.md / .bunnybyte/rules/*.md",
            "在仓库根目录写一份 AGENTS.md（构建/测试命令、代码约定），agent 每轮都会读到。",
        )
    detail = "、".join(f"{rule.path}（{rule.chars} 字）" for rule in rules)
    clipped = [rule for rule in rules if rule.clipped]
    if not clipped:
        return DoctorCheck("项目规则", "ok", detail)
    names = "、".join(f"{rule.path} 少读了 {rule.clipped} 字" for rule in clipped)
    return DoctorCheck(
        "项目规则",
        "warn",
        f"{detail}；{names}",
        "规则文件超出预算，后面的内容模型要自己 read_file 才看得到；精简它，或把细则拆进 .bunnybyte/rules/*.md。",
    )


def _secret_exposure_checks(run, git_root):
    if git_root is None:
        return []
    checks = []
    for name in (".bunnybyte.toml", ".env"):
        path = git_root / name
        if not path.exists():
            continue
        if _git(run, git_root, ["ls-files", "--error-unmatch", name]):
            checks.append(
                DoctorCheck(
                    f"{name} 泄露风险",
                    "fail",
                    f"{name} 已被 git 跟踪，可能包含 API key",
                    f"运行 `git rm --cached {name}` 并加入 .gitignore，然后轮换已暴露的 key。",
                )
            )
        elif not _git(run, git_root, ["check-ignore", name]):
            checks.append(
                DoctorCheck(
                    f"{name} 泄露风险",
                    "warn",
                    f"{name} 未被 .gitignore 忽略",
                    f"把 `{name}` 加入 .gitignore，避免误提交 API key。",
                )
            )
    return checks


def check_sandbox(start, config_path=None, which=shutil.which):
    try:
        config = resolve_project_sandbox_config(start=start, config_path=config_path)
    except ValueError as exc:
        return DoctorCheck("Sandbox", "fail", str(exc), "修正 [sandbox] 配置。")
    backend_path = SandboxChecker(which).backend_path(config.backend)
    if config.mode == "off":
        detail = "已关闭（run_shell 直接在本机执行）"
        hint = "" if not backend_path else "检测到 bubblewrap，可设置 [sandbox] mode = \"best_effort\" 隔离 shell。"
        return DoctorCheck("Sandbox", "ok", detail, hint)
    if backend_path:
        return DoctorCheck("Sandbox", "ok", f"mode={config.mode}, backend={backend_path}")
    status = "fail" if config.mode == "required" else "warn"
    return DoctorCheck(
        "Sandbox",
        status,
        f"mode={config.mode}，但未找到 bubblewrap",
        "Linux 上安装 bubblewrap（如 `apt install bubblewrap`）；macOS 暂不支持该后端。",
    )


def check_tools(which=shutil.which):
    checks = []
    if which("git"):
        checks.append(DoctorCheck("git", "ok", which("git")))
    else:
        checks.append(DoctorCheck("git", "warn", "未安装", "安装 git 以启用 /undo 撤销和快速的工作区快照（没有 git 时回退到逐文件哈希，工具调用会变慢）。"))
    if which("rg"):
        checks.append(DoctorCheck("ripgrep", "ok", which("rg")))
    else:
        checks.append(
            DoctorCheck("ripgrep", "warn", "未安装，search 将回退到较慢的 Python 实现", "安装 ripgrep：`brew install ripgrep` 或 `apt install ripgrep`。")
        )
    return checks


def run_doctor(
    start=".",
    *,
    provider=None,
    config_path=None,
    offline=False,
    client_factory=None,
    which=shutil.which,
    run=subprocess.run,
):
    checks = [check_python()]
    checks.extend(check_config_files(start, config_path))
    provider_checks, config = check_provider(start, provider, config_path)
    checks.extend(provider_checks)
    checks.extend(check_fallback(start, config, config_path))
    if offline:
        checks.append(DoctorCheck("连通性", "skip", "--offline，跳过网络检查。"))
    elif client_factory is not None:
        checks.append(check_connectivity(config, client_factory))
    checks.extend(check_workspace(start, run=run))
    checks.append(check_project_rules(start, run=run))
    checks.append(check_sandbox(start, config_path, which=which))
    checks.extend(check_tools(which=which))
    return checks


def doctor_exit_code(checks):
    return 1 if any(check.status == "fail" for check in checks) else 0


def render_doctor_text(checks):
    width = max((_display_width(check.name) for check in checks), default=0)
    lines = ["Bunny Byte doctor", ""]
    for check in checks:
        name = check.name + " " * (width - _display_width(check.name))
        lines.append(f" {STATUS_ICONS.get(check.status, '?')} {name}  {check.detail}")
        if check.hint and check.status != "ok":
            lines.append(f"   {' ' * width}  → {check.hint}")
        elif check.hint:
            lines.append(f"   {' ' * width}  提示：{check.hint}")
    counts = {status: sum(1 for check in checks if check.status == status) for status in STATUS_ICONS}
    lines.extend(["", f"通过 {counts['ok']} · 警告 {counts['warn']} · 失败 {counts['fail']} · 跳过 {counts['skip']}"])
    lines.append("一切就绪，运行 `bunny` 开始。" if not counts["fail"] else "请先按 → 提示修复失败项。")
    return "\n".join(lines)


def render_doctor_json(checks):
    return json.dumps(
        {"ok": doctor_exit_code(checks) == 0, "checks": [asdict(check) for check in checks]},
        ensure_ascii=False,
        indent=2,
    )


def _display_width(text):
    # 中文字符在终端里占两列，按显示宽度对齐。
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in text)


def _git(run, cwd, args):
    try:
        result = run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5)
    except Exception:  # noqa: BLE001
        return ""
    if getattr(result, "returncode", 1) != 0:
        return ""
    return str(result.stdout or "").strip() or "yes"


def _is_writable(path):
    path = Path(path)
    return path.exists() and path.is_dir() and os.access(path, os.W_OK)
