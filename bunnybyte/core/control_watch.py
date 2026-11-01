"""控制面守卫的 shell 部分：run_shell / 有风险的 MCP 工具前后比对一份固定的文件清单。

shell 写文件的办法太多（`>>`、`tee`、`sed -i`、`git config`、`pre-commit install`……），解析命令只能挡住一部分，
所以这里不猜命令，而是在命令前后对一份固定清单做 stat（内容只在 stat 变了时重读，小文件才缓存）：
仓库里影子快照看不见的（`.git/config` 的危险键、`.git/hooks/`、`.bunnybyte/{rules,skills}`、根目录的 agent
配置，常被 gitignore），和工作区外的（shell 启动脚本、`~/.gitconfig`、`~/.ssh`、Bunny Byte 的全局配置和
信任记录、其他 agent 的全局配置、`~/Library/LaunchAgents`）。

命令改了其中的文件：用户这一轮点名过的照常保留；warn 模式只提示；否则问人“保留这些改动吗”（带 diff），
拒绝或没人能问（`bunny -p`、`--approval never`）就恢复原内容（新建的删掉）。恢复前先把改后的内容备份进
本轮的 run 目录，再 stat 一次：等待确认期间文件又被别人改过就不恢复、只报告。仓库里快照看得见的规则文件
（子目录的 AGENTS.md、`skills/*/SKILL.md`）由 `report_turn_paths` 在回合末尾按本轮改动列出来（`/undo` 能撤）。
"""

from __future__ import annotations

import json
import os
import re
import stat
import threading
from pathlib import Path

from .control_plane import LABELS, classify, control_mode, exempt, mentioned, record, request_texts, _change
from .secret_vault import execute_masked
from .turn_diff import unified

MAX_CACHED_BYTES = 256 * 1024
MAX_DIR_ENTRIES = 2000
MAX_DIFF_LINES = 30
# Claude Code 随时在写这些文件，恢复旧内容可能冲掉它（或你）在命令运行期间的改动：只报告
REPORT_ONLY = ("~/.claude.json", "~/.claude/")
LARGE_FILES = {"~/.claude.json": 8 * 1024 * 1024}  # 重度用户的 ~/.claude.json 常过 256KB，读不了就只能按整份比较
GIT_HOOK_NAMES = (
    "applypatch-msg", "pre-applypatch", "post-applypatch", "pre-commit", "pre-merge-commit", "prepare-commit-msg",
    "commit-msg", "post-commit", "pre-rebase", "post-checkout", "post-merge", "pre-push", "pre-receive", "update",
    "post-receive", "post-update", "push-to-checkout", "pre-auto-gc", "post-rewrite", "sendemail-validate", "fsmonitor-watchman",
)
ROOT_FILES = (
    ".bunnybyte.toml", "AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursorrules", ".clinerules", ".windsurfrules", ".mcp.json",
    ".claude/settings.json", ".claude/settings.local.json", ".cursor/mcp.json", ".gemini/settings.json", ".codex/config.toml",
    ".vscode/settings.json", ".vscode/tasks.json", ".git/config",
)
ROOT_DIRS = (".git/hooks", ".bunnybyte/rules", ".bunnybyte/skills", ".claude/commands", ".claude/agents", ".claude/skills", ".claude/hooks", ".cursor/rules")
HOME_FILES = (
    (".bashrc", "shell_rc"), (".bash_profile", "shell_rc"), (".bash_login", "shell_rc"), (".profile", "shell_rc"),
    (".zshrc", "shell_rc"), (".zshenv", "shell_rc"), (".zprofile", "shell_rc"), (".zlogin", "shell_rc"),
    (".config/fish/config.fish", "shell_rc"), (".gitconfig", "git_config"), (".config/git/config", "git_config"),
    (".ssh/authorized_keys", "ssh"), (".ssh/config", "ssh"), (".config/bunnybyte/config.toml", "config"),
    (".bunnybyte.toml", "config"), (".env", "config"), (".claude.json", "config"), (".claude/settings.json", "config"),
    (".claude/CLAUDE.md", "rules"), (".codex/config.toml", "config"), (".codex/AGENTS.md", "rules"),
    (".gemini/settings.json", "config"), (".cursor/mcp.json", "config"),
)
HOME_DIRS = ((".claude/commands", "config"), (".claude/agents", "config"), ("Library/LaunchAgents", "launch"))
TRUST_FILES = ("trusted_hooks.json", "trusted_mcp.json", "trusted_endpoints.json")
# .git/config 里只看会让 git 执行别的程序的键：`git remote add`、`push -u` 写的那些不算
GIT_SECTION_RE = re.compile(r'^\s*\[\s*([A-Za-z0-9.-]+)(?:\s+"([^"]*)")?\s*\]')
GIT_KEY_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9-]*)\s*(?:=\s*(.*))?$")
DANGEROUS_CORE_KEYS = frozenset({"hookspath", "fsmonitor", "sshcommand", "pager", "editor", "askpass", "gitproxy", "worktree"})


def watch_targets(agent):
    """[(绝对路径, 显示名, 类别, 是不是目录)]：仓库里的、用户 HOME 下的、Bunny Byte 的信任记录。"""
    root, home = Path(agent.root), _home()
    targets = [(root / name, name, classify(name) or "config", False) for name in ROOT_FILES]
    targets += [(root / name, name, classify(name + "/x") or "config", True) for name in ROOT_DIRS]
    common = git_common_dir(root)
    if common is not None:  # worktree / 子模块：.git 是个文件，真正的 config 和 hooks 在别处
        targets += [(common / "config", ".git/config", "git_config", False), (common / "hooks", ".git/hooks", "git_hook", True)]
    for base in {root / ".git"} | ({common} if common is not None else set()):
        targets += [(base / "hooks" / name, f".git/hooks/{name}", "git_hook", False) for name in GIT_HOOK_NAMES]
    targets += [(home / name, f"~/{name}", category, False) for name, category in HOME_FILES]
    targets += [(home / name, f"~/{name}", category, True) for name, category in HOME_DIRS]
    base = Path(os.environ["BUNNYBYTE_HOME"]).expanduser() if os.environ.get("BUNNYBYTE_HOME") else home / ".bunnybyte"
    shown = str(base).replace(str(home), "~", 1)
    targets += [(base / name, f"{shown}/{name}", "trust", False) for name in TRUST_FILES]
    targets.append((base / "skills", f"{shown}/skills", "skills", True))
    seen, unique = set(), []
    for item in targets:
        key = os.path.normcase(str(item[0]))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _home():
    return Path.home()  # 测试里换成临时目录：跑测试的 shell 命令不能去比对、恢复开发者自己的 ~/.zshrc


def git_common_dir(root):
    """`.git` 是文件（`gitdir: …`）时返回 git 的公共目录；普通仓库返回 None。不起 git 进程。"""
    marker = Path(root) / ".git"
    if not marker.is_file():
        return None
    try:
        text = marker.read_text(encoding="utf-8", errors="replace").strip()
        if not text.startswith("gitdir:"):
            return None
        gitdir = Path(text[len("gitdir:"):].strip())
        gitdir = gitdir if gitdir.is_absolute() else (Path(root) / gitdir)
        common = gitdir / "commondir"
        if common.is_file():
            value = Path(common.read_text(encoding="utf-8").strip())
            gitdir = value if value.is_absolute() else gitdir / value
        return gitdir.resolve()
    except OSError:
        return None


def _signature(path):
    """(mtime, 大小, inode, 目标的权限, 是不是符号链接, 链接本身的 inode)：stow / chezmoi 管理的 ~/.zshrc
    是指向别处的符号链接，`echo >> ~/.zshrc` 改的是目标，只看 lstat 会漏掉。"""
    try:
        link = os.lstat(path)
    except OSError:
        return None
    try:
        info = os.stat(path)
    except OSError:
        info = link  # 断开的链接
    return (info.st_mtime_ns, info.st_size, info.st_ino, info.st_mode, stat.S_ISLNK(link.st_mode), link.st_ino)


def _walk(directory):
    """目录下的文件（最多两层、最多 MAX_DIR_ENTRIES 个），git 自带的 *.sample 钩子不算。"""
    found = []
    try:
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
    except OSError:
        return found
    for entry in entries:
        if len(found) >= MAX_DIR_ENTRIES:
            break
        if entry.name.endswith(".sample"):
            continue
        if entry.is_dir(follow_symlinks=False):
            try:
                found += [Path(item.path) for item in os.scandir(entry.path) if not item.is_dir(follow_symlinks=False)][:20]
            except OSError:
                continue
        else:
            found.append(Path(entry.path))
    return found


def take(agent):
    """{绝对路径: (显示名, 类别, stat 签名, 内容或 None)}；内容按签名缓存，没变就不重读。"""
    cache = getattr(agent, "_control_cache", None)
    if cache is None:
        cache = agent._control_cache = {}
    state = {}
    for path, display, category, is_dir in watch_targets(agent):
        files = [(item, f"{display}/{item.relative_to(path).as_posix()}") for item in _walk(path)] if is_dir else [(path, display)]
        for item, shown in files:
            signature = _signature(item)
            if signature is None:
                state[str(item)] = (shown, category, None, None)
                continue
            cached = cache.get(str(item))
            if cached is None or cached[0] != signature:
                cached = (signature, _read(item, signature, LARGE_FILES.get(shown, MAX_CACHED_BYTES)))
                cache[str(item)] = cached
            state[str(item)] = (shown, category, signature, cached[1])
    return state


def _read(path, signature, limit=MAX_CACHED_BYTES):
    if signature[1] > limit or not os.path.isfile(path):
        return None
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def git_config_risk(data):
    """.git/config 里会执行程序的那些键；内容读不了时返回 None（按整份文件比较）。"""
    if data is None:
        return None
    risky, section, sub = [], "", ""
    for line in data.decode("utf-8", "replace").splitlines():
        line = line.split("#", 1)[0].split(";", 1)[0] if not line.strip().startswith("[") else line
        header = GIT_SECTION_RE.match(line)
        if header:
            section, sub = header.group(1).lower(), (header.group(2) or "")
            if "." in section:
                section, sub = section.split(".", 1)
            line = line[header.end():]  # `[core] hooksPath = x` 同一行里的键 git 也认
            if not line.strip():
                continue
        match = GIT_KEY_RE.match(line)
        if not match:
            continue
        key, value = match.group(1).lower(), (match.group(2) or "").strip()
        if (
            (section == "core" and key in DANGEROUS_CORE_KEYS)
            or section in {"filter", "include", "includeif"}
            or (section == "credential" and key == "helper")
            or (section == "diff" and key in {"textconv", "command", "external"})
            or section == "pager"
            or (section == "submodule" and key == "update" and value.lstrip('"').startswith("!"))
            or (section == "gpg" and (key in {"program", "defaultkeycommand"} or sub == "ssh"))
            or (section == "uploadpack" and key == "packobjectshook")
            or (section == "alias" and value.lstrip('"').startswith("!"))
            or (section == "remote" and key in {"uploadpack", "receivepack"})
            or (section in {"merge", "sequence"} and key in {"driver", "editor", "program"})
        ):
            risky.append((section, sub, key, value))
    return sorted(risky)


def mcp_servers_view(data):
    """~/.claude.json 这种程序随时在写的状态文件：只看各处的 mcpServers / hooks。"""
    if data is None:
        return None
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    found = []

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"mcpServers", "hooks"}:
                    found.append((path + "/" + key, json.dumps(value, sort_keys=True)))
                elif isinstance(value, (dict, list)) and len(path) < 400:
                    walk(value, path + "/" + str(key))
        elif isinstance(node, list):
            for index, value in enumerate(node[:200]):
                walk(value, f"{path}/{index}")

    walk(parsed, "")
    return sorted(found)


RISK_VIEWS = {"git_config": git_config_risk, "~/.claude.json": mcp_servers_view}


def changed_entries(agent, before, after):
    changes = []
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path), after.get(path)
        old_sig, new_sig = old[2] if old else None, new[2] if new else None
        if old_sig == new_sig:
            continue
        old_data, new_data = (old[3] if old else None), (new[3] if new else None)
        if old_sig is not None and new_sig is not None and old_data is not None and old_data == new_data:
            continue  # 只是 touch / 原样重写
        display, category = (new or old)[0], (new or old)[1]
        risk = RISK_VIEWS.get(category if category == "git_config" else display)
        if risk is not None:  # `git init`、`git config user.name`、Claude Code 更新自己的状态都不算
            old_risk = risk(old_data) if old_sig else []
            new_risk = risk(new_data) if new_sig else []
            if old_risk is not None and old_risk == new_risk:
                continue
        status = "created" if old_sig is None else ("deleted" if new_sig is None else "modified")
        changes.append({"path": path, "display": display, "category": category, "status": status, "old": old, "new": new})
    return changes


def execute_watched(agent, tool, args):
    """tool_executor 调用：执行工具，命令改了控制面文件时按模式处理；给模型的提示由 `pop_notes` 取走。"""
    agent._control_pending_notes = []
    if not _watched(agent, tool):
        return execute_masked(agent, tool, args)
    before = take(agent)
    try:
        result = execute_masked(agent, tool, args)
    except Exception as exc:  # 超时 / 出错的命令照样可能已经改了文件
        notes = settle(agent, tool, before)
        if notes:
            exc.args = (f"{exc}\n\n" + "\n".join(notes),)
        raise
    except BaseException:  # Ctrl-C：照样检查一遍，没人问就恢复
        settle(agent, tool, before)
        raise
    agent._control_pending_notes = settle(agent, tool, before)
    return result


def pop_notes(agent):
    notes = list(getattr(agent, "_control_pending_notes", None) or [])
    agent._control_pending_notes = []
    return notes


def _watched(agent, tool):
    if control_mode(agent) == "off" or exempt(agent):
        return False
    return tool.name == "run_shell" or (tool.name.startswith("mcp__") and getattr(tool, "risky", False))


def watch_command(agent, tool_name, run):
    """运行时自己跑的命令（验证闸门、/goal 的检查）：模型写的测试 / conftest 也可能去改 ~/.zshrc。返回 (结果, 提示)。"""
    if control_mode(agent) == "off" or exempt(agent):
        return run(), []
    before = take(agent)
    result = run()
    return result, settle(agent, _Named(tool_name), before)


class _Named:
    def __init__(self, name):
        self.name = name


def settle(agent, tool, before):
    changes = changed_entries(agent, before, take(agent))
    if not changes:
        return []
    texts = request_texts(agent)
    keep = [item for item in changes if mentioned(item["display"], texts)]
    rest = [item for item in changes if item not in keep]
    records = [_change(item["display"], item["category"], item["status"], "mentioned") for item in keep]
    notes = []
    if rest:
        ask = [item for item in rest if not item["display"].startswith(REPORT_ONLY)]
        decision = _decide(agent, tool, ask) if ask else "reported"
        for item in rest:
            outcome = "reported" if item not in ask else (decision if decision != "restore" else restore(agent, item))
            records.append(_change(item["display"], item["category"], item["status"], outcome))
        notes.append(model_note(rest, records[len(keep):]))
    record(agent, tool.name, "shell", records)
    return notes


def _decide(agent, tool, changes):
    """keep 的结果：noted（warn）/ confirmed（人同意保留）/ reported（技能进化正在后台写技能）/ restore。"""
    if control_mode(agent) == "warn":
        return "noted"
    if _skill_writer_running(agent) and all(item["category"] == "skills" and not item["display"].startswith("~") for item in changes):
        return "reported"  # 后台的技能进化可能正在写 .bunnybyte/skills，不能把它的写入算到这条命令头上恢复掉
    from .dependency_guard import human_confirmer

    confirm = human_confirmer(agent)
    if confirm is None:
        return "restore"
    note = "控制面守卫：这条命令改了会改变 agent 行为的文件，允许 = 保留改动，拒绝 = 恢复原内容\n" + preview(agent, changes)
    try:
        return "confirmed" if confirm(tool.name, {"command": "保留下面这些改动？", "guard_note": note}) else "restore"
    except Exception:  # 弹窗出错按拒绝处理
        return "restore"


def _skill_writer_running(agent):
    threads = getattr(getattr(agent, "skill_evolution", None), "_review_threads", None) or []
    return any(isinstance(item, threading.Thread) and item.is_alive() for item in threads)


def preview(agent, changes):
    lines = []
    for item in changes:
        old, new = item["old"][3] if item["old"] else None, item["new"][3] if item["new"] else None
        lines.append(f"{item['display']}（{LABELS.get(item['category'], item['category'])}，{STATUS_TEXT[item['status']]}）")
        if old is not None or new is not None:
            lines.extend(unified(item["display"], old, new, mask=agent.redact_text).splitlines()[2:MAX_DIFF_LINES])
    return "\n".join(lines)


STATUS_TEXT = {"created": "新建", "modified": "修改", "deleted": "删除"}


def restore(agent, item):
    """恢复命令之前的内容；恢复前确认文件还是命令刚改完的样子，并把改后的内容备份进 run 目录。"""
    path, old, new = Path(item["path"]), item["old"], item["new"]
    current = _signature(path)
    if current != (new[2] if new else None):
        return "changed_again"
    if old and old[2] is not None and old[3] is None:
        return "reported"  # 太大没缓存原内容，恢复不了
    try:
        if new and new[3] is not None:
            _backup(agent, item["display"], new[3])
        if old is None or old[2] is None:
            if path.is_file() or path.is_symlink():
                path.unlink()
        else:
            if path.is_symlink() and not old[2][4]:
                path.unlink()  # 命令把普通文件换成了链接：去掉链接再写回
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(old[3])  # 原来就是链接时写回链接的目标，链接本身保留
            os.chmod(path, old[2][3] & 0o7777)
    except OSError:
        return "reported"
    agent._control_cache.pop(str(path), None)
    return "restored"


def _backup(agent, display, data):
    state = getattr(agent, "current_task_state", None)
    if state is None:
        return
    directory = agent.run_store.artifacts_dir(state) / "control-backup"
    directory.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", display.lstrip("~/")) or "file"
    (directory / name).write_bytes(data)


def model_note(changes, records):
    parts = []
    for item, entry in zip(changes, records):
        outcome = {
            "restored": "已恢复原内容", "confirmed": "用户确认保留", "noted": "warn 模式，已保留",
            "reported": "已保留，提醒用户检查", "changed_again": "已保留，提醒用户检查",
        }.get(entry["decision"], entry["decision"])
        parts.append(f"{item['display']}（{LABELS.get(item['category'], item['category'])}，{STATUS_TEXT[item['status']]}，{outcome}）")
    advice = "" if all(entry["decision"] == "confirmed" for entry in records) else (
        "这些文件会改变 agent 之后的行为，用户这一轮没有提到它们。不要再换办法修改；确实需要的话，在最终回答里说明，让用户自己改。"
    )
    return f"[control-plane guard] 命令改了 {'；'.join(parts)}。{advice}".rstrip()


def report_turn_paths(agent, task_state):
    """回合末尾：快照看得见的控制面文件（子目录的 AGENTS.md、skills/*/SKILL.md）本轮被 shell 改过的，列出来。"""
    if control_mode(agent) == "off":
        return
    seen = {change["path"] for entry in (getattr(agent, "control_receipts", None) or {}).get(task_state.run_id, []) for change in entry["changes"]}
    texts = None
    found = []
    for path in task_state.changed_paths:
        category = classify(path)
        if not category or category == "memory" or path in seen:
            continue
        texts = request_texts(agent) if texts is None else texts
        if not mentioned(path, texts):
            found.append(_change(path, category, "modified", "reported"))
    if found:
        record(agent, "run_shell", "turn", found)
