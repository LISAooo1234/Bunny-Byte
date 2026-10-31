"""依赖守卫：模型要装的新包先过一遍“拼错 / 仿冒 / 刚注册”的检查。

在 PermissionChecker 里、审批之前运行，看 run_shell 的安装命令、下载即运行的命令（npx / uvx 等，
包已在本地 node_modules 里时不算）、`pip install -r` 引用的文件，和 write_file / patch_file 对
requirements*.txt、pyproject.toml、package.json 的改动：
- 名字在内置热门名单里、项目已经声明过、或在 `[deps] allow` 里 → 放行，不联网；
- 和热门包只差一个字符（`reqeusts` → `requests`）或第一次发布不到 30 天 → 可疑；
- 公共仓库里没有、或查询失败 → 只在工具结果后面附一句提示（装不上就是装不上，没有危害）；
  npx / uvx 后面常常只是可执行文件名或全局工具，这两种情况不提示。
“项目已经声明”按会话里第一次工具调用之前的依赖文件算：agent 自己用 shell 往 requirements.txt 里
追加一行，不能让这个名字变成“已声明”。
`mode = "guard"`（默认）时可疑的包必须有人确认，`--approval auto` 也一样；没人能确认（`bunny -p`、
子 agent、`--approval never`、非终端）就拒绝并告诉模型原因。`warn` 只提示，`off` 关闭。
"""

import re
import sys
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

from .dependency_extract import NPM, PYPI, Requested, dependency_file_kind, from_command, from_file_change, names_in_file, normalize
from .dependency_runners import (
    MISSING,
    committed_text,
    files_written_by,
    local_node_package,
    read_workspace_file,
    requirement_files,
)
from .dependency_names import is_popular, lookalike
from .dependency_registry import RegistryLookup, npm_lookup_base, private_index, project_index_urls
from .transcript_fence import match_patch

DEPS_MODES = ("off", "warn", "guard")
SUSPICIOUS = ("typosquat", "new", "unverifiable")
INSTALLER_HEADS = frozenset({"pip", "pip3", "uv", "uvx", "pipx", "python", "python3", "poetry", "pdm"})
REGISTRY_LABEL = {PYPI: "PyPI", NPM: "npm"}
LOCK_NAME_RE = re.compile(r'^name = "([^"]+)"', re.M)


@dataclass(frozen=True)
class DepsConfig:
    mode: str = "guard"
    lookup: bool = True
    allow: tuple = ()
    problems: list = field(default_factory=list, compare=False)


def resolve_deps_config(values, *, mode=None, trusted=None):
    """`values` 是合并后的配置，`trusted` 是只来自用户自己（全局配置 / --config）的那份。

    仓库自带的 `.bunnybyte.toml` 只能把守卫调得更严：克隆来的仓库不能替用户关掉它。
    """
    merged, problems = _parse((values or {}).get("deps"))
    if trusted is not None:
        base, base_problems = _parse((trusted or {}).get("deps"))
        problems = base_problems + problems
        if DEPS_MODES.index(merged.mode) < DEPS_MODES.index(base.mode):
            problems.append(f"项目配置不能把 [deps].mode 从 {base.mode} 调成 {merged.mode}，已忽略")
            merged = replace(merged, mode=base.mode)
        if base.lookup and not merged.lookup:
            problems.append("项目配置不能关闭 [deps].lookup，已忽略")
            merged = replace(merged, lookup=True)
        merged = replace(merged, allow=tuple(dict.fromkeys(base.allow + merged.allow)))  # 项目的 allow 是追加，不是替换
    if mode:
        merged = replace(merged, mode=mode)
    return replace(merged, problems=problems)


def _parse(section):
    problems = []
    if section is None:
        return DepsConfig(), problems
    if not isinstance(section, dict):
        return DepsConfig(), ["[deps] 应该是一个表，已忽略"]
    mode = str(section.get("mode", "guard"))
    if mode not in DEPS_MODES:
        problems.append(f"[deps].mode 只能是 {' / '.join(DEPS_MODES)}，已按 guard 处理")
        mode = "guard"
    allow = section.get("allow", [])
    if not isinstance(allow, list):
        problems.append("[deps].allow 应该是包名列表，已忽略")
        allow = []
    return DepsConfig(mode=mode, lookup=section.get("lookup", True) is not False, allow=tuple(str(item) for item in allow)), problems


@dataclass
class DependencyCheck:
    findings: list  # 每项 dict：ecosystem / name / verdict / similar / created / detail
    suspicious: list

    def notes(self):
        return [finding_text(item) for item in self.findings]


def check_tool_call(agent, tool, args):
    """这次工具调用要新装的包里有问题的那些；没有新包或守卫关闭时返回 None。"""
    config = getattr(agent, "deps_config", None) or DepsConfig()
    if config.mode == "off":
        return None
    baseline = declared_baseline(agent)  # 在 agent 第一次能改文件之前定下来
    if tool.name not in {"run_shell", "write_file", "patch_file"}:
        return None
    extraction = _requested(agent, tool.name, args or {})
    if extraction is None:
        return None
    skip = baseline | {(eco, normalize(eco, name)) for eco in (PYPI, NPM) for name in config.allow}
    written = files_written_by(str(args.get("command", "")), INSTALLER_HEADS) if tool.name == "run_shell" else set()
    from_files, unverifiable = _from_requirement_files(agent, extraction, written)
    candidates = [
        item for item in extraction.packages + from_files
        if (item.ecosystem, normalize(item.ecosystem, item.name)) not in skip
        and not (item.runner and item.ecosystem == NPM and local_node_package(agent.root, item.name, item.bin_name))
    ]
    findings = unverifiable + _judge(agent, config, [item for item in candidates if not is_popular(item.ecosystem, item.name)], extraction)
    if not findings:
        return None
    return DependencyCheck(findings, [item for item in findings if item["verdict"] in SUSPICIOUS])


def declared_baseline(agent):
    """项目在这个会话开始时已经声明 / 锁定的依赖。

    第一次检查时读盘并记进会话（`--resume` 接着用，不会把上次 agent 追加的名字当成已声明）；子 agent 用父 agent 的那份。
    """
    baseline = getattr(agent, "dependency_baseline", None)
    if baseline is None:
        session = getattr(agent, "session", None)
        saved = session.get("dependency_baseline") if isinstance(session, dict) else None
        if isinstance(saved, list):
            baseline = frozenset(tuple(item) for item in saved if isinstance(item, (list, tuple)) and len(item) == 2)
        else:
            baseline = frozenset(declared_names(agent.root))
            if isinstance(session, dict):
                session["dependency_baseline"] = sorted(list(item) for item in baseline)
        agent.dependency_baseline = baseline
    return baseline


def _from_requirement_files(agent, extraction, written):
    """`pip install -r extra.txt`：文件里的包都算这次要装的。

    文件已经提交进 git 的那部分（HEAD 里就有的名字）算项目自己声明的；同一条命令里先写再装的文件、
    工作区外 / URL / 太大读不了的文件，守卫看不到会装什么，算可疑（有人时问，没人时拒绝）。
    """
    found, unverifiable = [], []
    for path, text in requirement_files(read_workspace_file(agent), extraction.requirement_files):
        if text is None or str(PurePosixPath(path)) in written:
            reason = "这条命令会先改写它再安装" if text is not None else "在工作区外、是网址或超过 256KB"
            unverifiable.append({"ecosystem": PYPI, "name": f"-r {path}", "verdict": "unverifiable", "similar": "",
                                 "created": "", "detail": reason, "runner": False})
            continue
        if text == MISSING:
            continue
        parsed = names_in_file(path, text)
        extraction.index_urls += parsed.index_urls  # 文件里写着私有索引：内部包名不发到公网
        committed = names_in_file(path, committed_text(agent.root, path) or "").packages
        known = {normalize(item.ecosystem, item.name) for item in committed}
        found += [Requested(item.ecosystem, item.name, f"-r {path}") for item in parsed.packages
                  if normalize(item.ecosystem, item.name) not in known]
    return found, unverifiable


def _requested(agent, name, args):
    if name == "run_shell":
        return from_command(str(args.get("command", "")))
    raw = str(args.get("path", ""))
    if not dependency_file_kind(raw):
        return None
    try:
        path = agent.path(raw)
        before = path.read_text(encoding="utf-8") if path.is_file() else ""
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if name == "write_file":
        after = str(args.get("content", ""))
    else:
        old, new = match_patch(before, str(args.get("old_text", "")), str(args.get("new_text", "")))
        if not old or old not in before:
            return None  # patch 本身会失败，不必检查
        after = before.replace(old, new, 1)
    return from_file_change(Path(raw).as_posix(), before, after)


def _judge(agent, config, packages, extraction):
    findings, unknown = [], {PYPI: [], NPM: []}
    for item in packages:
        similar = lookalike(item.ecosystem, item.name)
        findings.append({"ecosystem": item.ecosystem, "name": item.name, "verdict": "typosquat" if similar else "",
                         "similar": similar, "created": "", "detail": "", "runner": item.runner})
        unknown[item.ecosystem].append(findings[-1])
    for ecosystem, items in unknown.items():
        if not items:
            continue
        urls = extraction.index_urls + project_index_urls(agent.root)
        index = private_index(ecosystem, agent.root, urls) if config.lookup else ""
        verdicts = {}
        if config.lookup and not index:
            verdicts = _lookup(agent).check(ecosystem, [item["name"] for item in items])
        for item in items:
            verdict = verdicts.get(normalize(ecosystem, item["name"]))
            if verdict is None:
                item["detail"] = f"配置了私有索引 {index}，不去公共仓库查" if index else ""
                continue
            item["created"] = verdict["created"]
            if verdict["status"] == "missing" and item["verdict"] == "typosquat":
                item["detail"] = "公共仓库里暂时没有这个名字，随时可能被人抢注"  # 拼错的名字照样拦：注册只要一分钟
            elif verdict["status"] == "missing":
                item["verdict"], item["detail"] = "missing", verdict["detail"]  # 装不上，没有危害
            elif verdict["status"] == "new":
                item["verdict"], item["detail"] = "new", verdict["detail"]
            elif verdict["status"] == "unchecked" and not item["verdict"]:
                item["verdict"], item["detail"] = "unchecked", verdict["detail"]
    # npx tsc / uvx 某个全局工具：“仓库里没有”多半只是可执行文件名，不提示；拼错和刚注册照样拦
    return [item for item in findings if item["verdict"] and not (item["runner"] and item["verdict"] not in SUSPICIOUS)]


def _lookup(agent):
    lookup = getattr(agent, "dependency_lookup", None)
    if lookup is None:
        cache = Path.home() / ".bunnybyte" / "dependency_cache.json"  # 注册表的事实是全局的，仓库里的缓存可能被预先写好
        lookup = agent.dependency_lookup = RegistryLookup(cache_path=cache, npm_base=npm_lookup_base(agent.root))
    return lookup


def declared_names(root):
    """项目已经声明 / 锁定的依赖：`pip install foo` 时 foo 已经在 requirements 里就不必再问。"""
    root = Path(root)
    names = set()
    files = [root / "pyproject.toml", root / "package.json", *sorted(root.glob("requirements*.txt")), *sorted(root.glob("requirements/*.txt"))]
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for item in from_file_change(path.relative_to(root).as_posix(), "", text).packages:
            names.add((item.ecosystem, normalize(item.ecosystem, item.name)))
    for lock in ("uv.lock", "poetry.lock", "pdm.lock"):
        try:
            names |= {(PYPI, normalize(PYPI, name)) for name in LOCK_NAME_RE.findall((root / lock).read_text(encoding="utf-8"))}
        except (OSError, UnicodeDecodeError):
            continue
    names |= {(NPM, name.lower()) for name in _package_lock_names(root / "package-lock.json")}
    return names


def _package_lock_names(path):
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return set()
    names = {key.rsplit("node_modules/", 1)[-1] for key in (data.get("packages") or {}) if "node_modules/" in key}
    return names | set(data.get("dependencies") or {})


def finding_text(item):
    name, registry = item["name"], REGISTRY_LABEL[item["ecosystem"]]
    if item.get("runner") and item["verdict"] in SUSPICIOUS:
        return _base_text(item, name, registry) + "；这条命令会下载并立刻运行它"
    return _base_text(item, name, registry)


def _base_text(item, name, registry):
    if item["verdict"] == "unverifiable":
        return f"没法检查 `{name}` 里要装的包（{item['detail']}）"
    if item["verdict"] == "typosquat":
        text = f"{name} 不在常用包名单里，和热门包 {item['similar']} 只差一个字符，可能是拼错或仿冒包"
        return text + (f"（{item['detail']}）" if item["detail"] else "")
    if item["verdict"] == "new":
        return f"{name} 在 {registry} 上{item['detail']}（{item['created']}），刚注册的包可能是抢注的"
    if item["verdict"] == "missing":
        hint = f"，要装的可能是 {item['similar']}" if item["similar"] else ""
        return f"{registry} 上没有 {name}（{item['detail']}），安装会失败，名字可能是编出来的{hint}"
    return f"没能确认 {name} 是否可信（{item['detail'] or '查询失败'}）"


@dataclass
class GuardVerdict:
    decision: object = None  # 不是 None 时直接作为权限结果
    approval_args: dict = None  # 普通审批弹窗用的参数（带上依赖提示）
    notes: tuple = ()  # 附在工具结果后面给模型看的提示


def guard_permission(agent, tool, args, allow, deny):
    """PermissionChecker 调用：`allow(reason)` / `deny(reason, event, message)` 构造权限结果。"""
    check = check_tool_call(agent, tool, args)
    if check is None:
        return GuardVerdict(approval_args=args)
    config = getattr(agent, "deps_config", None) or DepsConfig()
    note = "依赖守卫：" + "；".join(check.notes())
    # 复制一份：permission_decision 事件里记的还是原参数。默认的 runtime.approve 会把中文转成 \uXXXX，不给它看提示
    approval_args = dict(args, dependency_note=note) if not _default_approve(agent) else args
    if not check.suspicious or config.mode == "warn":
        record_check(agent, tool.name, check, "noted")
        return GuardVerdict(approval_args=approval_args, notes=(f"[dependency guard] {note}",))
    confirm = human_confirmer(agent)
    if confirm is None:
        record_check(agent, tool.name, check, "blocked")
        return GuardVerdict(decision=deny("dependency_blocked", "dependency_guard", deny_message(tool.name, check)))
    if confirm(tool.name, dict(args, dependency_note=note)):  # 这一次确认同时算普通审批，不会再弹第二次
        record_check(agent, tool.name, check, "confirmed")
        confirmed = {(item["ecosystem"], normalize(item["ecosystem"], item["name"])) for item in check.suspicious}
        agent.dependency_baseline = declared_baseline(agent) | confirmed  # 确认过的包，之后 `pip install -r` 不再问
        return GuardVerdict(decision=allow("dependency_confirmed"), notes=(f"[dependency guard] 用户确认后执行。{note}",))
    record_check(agent, tool.name, check, "denied")
    message = f"error: the user rejected {tool.name}: {'；'.join(finding_text(item) for item in check.suspicious)}"
    return GuardVerdict(decision=deny("dependency_denied", "dependency_guard", message))


def human_confirmer(agent):
    """能真正问到人的审批函数；问不到人时返回 None（这时可疑的包只能拒绝）。"""
    if getattr(agent, "unattended", False) or agent.depth > 0 or agent.approval_policy == "never" or agent.read_only:
        return None
    if getattr(agent, "dependency_confirm", None) is not None:
        return agent.dependency_confirm  # TUI 的审批弹窗
    if agent.approval_policy == "ask":
        # 默认的 runtime.approve 把参数转成 ASCII JSON，中文提示会变成 \uXXXX，换成能读的提问
        return _stdin_confirm if _default_approve(agent) else agent.approve
    if threading.current_thread() is threading.main_thread() and sys.stdin is not None and sys.stdin.isatty():
        return _stdin_confirm
    return None


def _default_approve(agent):
    return getattr(agent.approve, "__func__", None) is getattr(type(agent), "approve", None)


def _stdin_confirm(name, args):
    try:
        answer = input(f"{args.get('dependency_note', '')}\n仍然执行 {name}？[y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def deny_message(tool_name, check):
    hints = [f"如果是拼错，请改用 {item['similar']}" for item in check.suspicious if item["similar"]]
    return (
        f"error: dependency guard blocked {tool_name}: " + "；".join(finding_text(item) for item in check.suspicious)
        + "。没有人能确认这次安装，所以没有执行。" + ("；".join(hints) + "。" if hints else "")
        + "确实需要这个包时请让用户自己安装，或把它加进 .bunnybyte.toml 的 [deps] allow。"
    )


def record_check(agent, tool_name, check, decision):
    """写进 trace（`bunny replay` 显示）和本轮的依赖回执（report.json / `bunny -p` JSON）。"""
    entry = {"tool_name": tool_name, "decision": decision, "findings": [dict(item) for item in check.findings]}
    agent.session_event_bus.emit("dependency_check", entry)
    state = getattr(agent, "current_task_state", None)
    if state is None:
        return
    receipts = getattr(agent, "dependency_receipts", None)
    if receipts is None:
        receipts = agent.dependency_receipts = {}
    if state.run_id not in receipts and len(receipts) >= 20:
        receipts.pop(next(iter(receipts)))  # 只留最近的运行
    receipts.setdefault(state.run_id, []).append(entry)
    agent.emit_trace(state, "dependency_check", dict(entry, status=decision))


def dependency_receipt(agent, state):
    if state is None:
        return []
    return list((getattr(agent, "dependency_receipts", None) or {}).get(state.run_id, []))
