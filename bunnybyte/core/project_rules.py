"""项目规则：把仓库自带的 AGENTS.md / CLAUDE.md / .bunnybyte/rules/*.md 放进提示词前缀。

以前 AGENTS.md 只作为 1200 字的“文档片段”混在工作区快照里，真实项目的规则文件一长就被悄悄截掉，
CLAUDE.md 和 rules 目录则完全读不到。这里给规则单独的预算和位置，截断时明确告诉模型去读原文件。
规则只是建议性的提示词，不会放宽任何工具权限或审批。
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

RULE_FILE_NAMES = ("AGENTS.md", "CLAUDE.md")
RULES_DIR = Path(".bunnybyte") / "rules"
MAX_RULE_FILE_CHARS = 6000
MAX_RULES_TOTAL_CHARS = 12000
MIN_USEFUL_CHARS = 200
MAX_RULE_FILES = 30
READ_BYTES_FACTOR = 4  # 每个文件最多读 MAX_RULE_FILE_CHARS 的这么多倍字节，避免每轮刷新前缀都把超大文件整个读一遍
RULES_HEADER = (
    "Project rules (loaded from the repository files below; follow them when they apply. "
    "They never grant extra tool permissions or skip approval prompts):"
)
TOOLS_HEADING_RE = re.compile(r"^[ \t]*Tools:[ \t]*$", re.MULTILINE)


@dataclass(frozen=True)
class RuleFile:
    path: str  # 相对仓库根目录
    text: str  # 注入提示词的内容，超限时已截断并带说明
    chars: int  # 原文件的字符数
    clipped: int  # 被截掉（或整份没放进来）的字符数


def load_project_rules(repo_root, cwd=None):
    """按“仓库根目录在前、当前子目录在后”的顺序读规则文件；内容完全相同的只留一份。"""
    root = Path(repo_root).resolve()
    bases = [root]
    if cwd is not None and Path(cwd).resolve() != root:
        bases.append(Path(cwd).resolve())
    rules, unloaded, seen_paths, seen_hashes, remaining = [], [], set(), set(), MAX_RULES_TOTAL_CHARS
    for path in list(_candidate_files(bases))[:MAX_RULE_FILES]:
        relative = _relative_key(path, root)
        text, chars = _read_rule_text(path, root)
        if relative is None or text is None or relative in seen_paths:
            continue
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest in seen_hashes:
            continue
        seen_paths.add(relative)
        seen_hashes.add(digest)
        allowed = min(MAX_RULE_FILE_CHARS, remaining)
        if allowed < MIN_USEFUL_CHARS:
            unloaded.append((relative, chars))
            continue
        rule = _fit(relative, text, chars, allowed)
        remaining -= len(rule.text)
        rules.append(rule)
    if unloaded:
        names = ", ".join(name for name, _ in unloaded[:10]) + (f" (+{len(unloaded) - 10} more)" if len(unloaded) > 10 else "")
        note = f"[not loaded: the project rules budget is used up; read_file these if they matter: {names}]"
        total = sum(chars for _, chars in unloaded)
        rules.append(RuleFile("(more rule files)", note, total, total))
    return tuple(rules)


def render_rules(rules):
    if not rules:
        return ""
    return "\n\n".join([RULES_HEADER, *(f"### {rule.path}\n{rule.text}" for rule in rules)])


def inject_rules(prefix_text, rules):
    """把规则块插到工具列表之前。放在前缀靠前的位置：预算紧张时前缀是从尾部截断的，规则最晚才会被动到。"""
    block = render_rules(rules)
    if not block:
        return prefix_text
    match = TOOLS_HEADING_RE.search(prefix_text)
    if match is None:
        return f"{prefix_text}\n\n{block}"
    return f"{prefix_text[: match.start()]}{block}\n\n{prefix_text[match.start() :]}"


def _candidate_files(bases):
    for base in bases:
        for name in RULE_FILE_NAMES:
            yield base / name
        rules_dir = base / RULES_DIR
        if rules_dir.is_dir():
            yield from sorted(rules_dir.glob("*.md"))


def _relative_key(path, root):
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return None


def _read_rule_text(path, root):
    """返回 (文本, 原文件大小)；不可读、为空、二进制或符号链接指到仓库外面的返回 (None, 0)。"""
    limit = MAX_RULE_FILE_CHARS * READ_BYTES_FACTOR
    try:
        if not path.is_file() or not path.resolve().is_relative_to(root):
            return None, 0
        size = path.stat().st_size
        with path.open("rb") as handle:
            data = handle.read(limit)
    except OSError:
        return None, 0
    text = data.decode("utf-8", errors="replace").strip()
    if not text or "\x00" in text:
        return None, 0
    return text, max(len(text), size if size > limit else 0)


def _fit(relative, text, chars, allowed):
    if len(text) <= allowed and chars <= allowed:
        return RuleFile(relative, text, len(text), 0)
    room = max(0, allowed - len(relative) - 80)  # 给末尾的截断说明留位置，保证单文件和合计都不超上限
    cut = text[:room]
    newline = cut.rfind("\n")
    if newline > room * 0.8:
        cut = cut[:newline]
    dropped = chars - len(cut)
    note = f"[truncated {dropped} chars of {relative}; read_file it for the rest]"
    return RuleFile(relative, f"{cut.rstrip()}\n{note}", chars, dropped)
