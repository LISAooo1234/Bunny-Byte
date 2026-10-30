"""一轮改动的 unified diff：从影子快照的两个 tree 读出内容，供 /diff、bunny replay --diff 和独立审查共用。"""

from __future__ import annotations

import difflib

from .shadow_snapshots import _LOCK, ShadowError, ShadowStore

BINARY_SNIFF = 8000
MAX_FILE_DIFF_LINES = 400
MAX_TOTAL_DIFF_LINES = 1500
MAX_COMPARE_LINES = 20_000  # difflib 是平方级的：4 万行的 lockfile 一次要十几秒
MAX_LINE_CHARS = 400  # 压缩过的单行 bundle 不能把整个文件塞进输出


def text_or_none(data):
    """bytes -> str；None 或二进制内容返回 None。"""
    if data is None:
        return None
    if b"\0" in data[:BINARY_SNIFF]:
        return None
    return data.decode("utf-8", "replace")


def _too_large(*texts):
    return any(text is not None and text.count("\n") > MAX_COMPARE_LINES for text in texts)


def _texts(old, new, mask):
    """解码并遮蔽整份文件内容：密钥规则按整行 / 整块（PEM）匹配，必须在切成 diff 行之前做。"""
    old_text, new_text = text_or_none(old), text_or_none(new)
    if mask is not None:
        old_text = mask(old_text) if old_text else old_text
        new_text = mask(new_text) if new_text else new_text
    return old_text, new_text


def unified(path, old, new, mask=None):
    """一个文件的 unified diff 文本；old/new 是 bytes，None 表示文件不存在。"""
    old_text, new_text = _texts(old, new, mask)
    if (old is not None and old_text is None) or (new is not None and new_text is None):
        return f"--- {path}\n(binary file changed)\n"
    if old is not None and old == new:
        return f"--- {path}\n(only file mode changed)\n"
    if _too_large(old_text, new_text):
        return f"--- {path}\n(file too large to diff: more than {MAX_COMPARE_LINES} lines)\n"
    lines = difflib.unified_diff(
        (old_text or "").splitlines(keepends=True),
        (new_text or "").splitlines(keepends=True),
        fromfile="/dev/null" if old is None else f"a/{path}",
        tofile="/dev/null" if new is None else f"b/{path}",
        n=3,
    )
    return "".join(_clip_line(line.rstrip("\n")) + "\n" for line in lines)


def _clip_line(line):
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + f" …(该行还有 {len(line) - MAX_LINE_CHARS} 字符)"


def tree_changes(store, before_tree, after_tree):
    """两个 tree 之间每个改动文件的 (状态, 路径, 旧内容, 新内容)，按路径排序。"""
    changes = store.diff(before_tree, after_tree)
    wanted = [item[key] for item in changes for key in ("old_oid", "oid") if set(item[key]) != {"0"}]
    with _LOCK:
        blobs = store._read_blobs(wanted) if wanted else {}
    result = []
    for item in sorted(changes, key=lambda change: change["path"]):
        old = None if item["status"] == "A" else blobs.get(item["old_oid"])
        new = None if item["status"] == "D" else blobs.get(item["oid"])
        result.append((item["status"], item["path"], old, new))
    return result


def file_stats(status, path, old, new):
    """--stat 一行需要的数字：新增 / 删除行数，二进制文件记为 None。"""
    old_text, new_text = text_or_none(old), text_or_none(new)
    binary = (old is not None and old_text is None) or (new is not None and new_text is None)
    if binary or _too_large(old_text, new_text):
        return {"status": status, "path": path, "added": None, "removed": None, "binary": binary}
    added = removed = 0
    for line in difflib.unified_diff((old_text or "").splitlines(), (new_text or "").splitlines(), n=0, lineterm=""):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return {"status": status, "path": path, "added": added, "removed": removed, "binary": False}


def render_changes(changes, *, mask=None, max_file_lines=MAX_FILE_DIFF_LINES, max_total_lines=MAX_TOTAL_DIFF_LINES):
    """把 tree_changes 的结果拼成 diff 文本；单文件和总行数都有上限，超出时写明省略了多少。

    `mask` 先作用于整份文件内容（见 `_texts`），再作用于 diff 文本兜底；都在截断之前，半行密钥漏不出去。
    """
    parts, total, omitted = [], 0, []
    for status, path, old, new in changes:
        budget = min(max_file_lines, max_total_lines - total)
        if budget <= 0:
            omitted.append(path)
            continue
        text = unified(path, old, new, mask)
        lines = (mask(text) if mask else text).splitlines()
        if len(lines) > budget:
            lines = lines[:budget] + [f"... ({path} 的 diff 还有 {len(lines) - budget} 行未显示)"]
        parts.extend(lines)
        total += len(lines)
    if omitted:
        parts.append(f"... (另有 {len(omitted)} 个文件的 diff 未显示：{'、'.join(omitted[:8])}{' 等' if len(omitted) > 8 else ''})")
    return "\n".join(parts)


def filter_paths(changes, prefixes):
    """只保留路径等于或位于某个前缀目录下的文件。"""
    if not prefixes:
        return changes
    cleaned = [prefix.removeprefix("./").strip("/") for prefix in prefixes]
    if any(prefix in ("", ".") for prefix in cleaned):
        return changes  # `/diff .` = 整个仓库
    return [item for item in changes if any(item[1] == p or item[1].startswith(p + "/") for p in cleaned if p)]


def current_turn_entry(agent):
    """当前回合在 undo_log 里的记录（回合中第一次快照时创建），没有就返回 None。"""
    turn_id = str(getattr(agent, "current_turn_id", "") or "")
    if not turn_id:
        return None
    for entry in reversed(agent.session.get("undo_log") or []):
        if entry.get("turn_id") == turn_id:
            return entry
    return None


def turn_changes_record(agent):
    """写进 report.json 的 `changes`：本轮前后的快照 tree 和改动文件，回合结束后仍能 diff。"""
    entry = current_turn_entry(agent)
    if entry is None or entry.get("before_tree") == entry.get("after_tree"):
        return {}
    store = ShadowStore.for_root(agent.root)
    try:
        paths = store.diff_paths(entry["before_tree"], entry["after_tree"]) if store.available else []
    except (ShadowError, OSError):
        paths = []
    return {"before_tree": entry["before_tree"], "after_tree": entry["after_tree"], "paths": paths[:200]}
