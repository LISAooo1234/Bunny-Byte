"""`@path` 文件提及：把用户消息里点名的文件在第一次模型调用前读进来。

`修一下 @calc.py 里的 bug` —— 运行时先替模型执行一次 read_file，结果像普通工具调用一样
进入历史、读取台账和 prior-read 凭证，模型不用再花一步去读，也可以直接 patch_file。

只解析用户亲手输入的文字（`mention_text`），不解析粘贴块、stdin、worker/skill/dream 的提示词：
那些文本里常有 `@decorator`、`@@` hunk 或模型写的路径，不该触发静默读取。
"""

import re
import time
from dataclasses import replace

from .engine_helpers import _commit_tool_record, _execute_tool

MAX_MENTIONS = 5
MENTION_READ_END = 2000
MAX_MENTION_BYTES = 256 * 1024

# `@` 前面是字母数字或路径字符时不算提及，这样 `a@b.com`、`foo@v1`、`@@` 不会被误认；
# 中文里常不加空格（`看看@calc.py`），所以汉字后面的 `@` 也算。
_MENTION_RE = re.compile(
    r'(?<![A-Za-z0-9_.\-/@\\])@(?!@)(?:"(?P<quoted>[^"\n]+)"|(?P<bare>[^\s"，。！？；：、（）【】《》「」『』]+))'
)
# 句尾标点不属于路径：`看看 @a.py.`、`(@b.py)`、`@c.py,`。
_TRAILING_PUNCT = ",;:!?)]}>'\"`"
# `@calc.py里的bug`：整段找不到时，退回到第一个汉字之前再试一次。
_CJK_RE = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff]")
# 像路径的提及找不到时提示一句：带 `/`，或以常见源码/文档扩展名结尾。
# `@decorator`、`@app.route`、`@v1.2.3`、`@user.name` 这类不提示。
_PATH_LIKE_RE = re.compile(
    r"/|\.(?:py|pyi|js|jsx|ts|tsx|mjs|cjs|go|rs|java|kt|c|h|cc|cpp|hpp|cs|rb|php|swift|sh|"
    r"md|rst|txt|toml|yaml|yml|json|ini|cfg|html|css|scss|sql|vue|lock|log|csv)$",
    re.IGNORECASE,
)
# 写进历史的预读结果带上来源，不然模型看到“用户消息 + 工具结果”会以为这一轮已经做过事了。
PRELOAD_NOTE = "(runtime pre-read: the user mentioned @{path} in this message; this is context, no work has been done yet in this turn)\n"


def parse_mentions(text):
    """返回用户消息里按出现顺序去重的候选路径（还没检查文件是否存在）。"""
    found = []
    for match in _MENTION_RE.finditer(str(text or "")):
        raw = match.group("quoted") or match.group("bare") or ""
        if match.group("bare"):
            raw = raw.rstrip(_TRAILING_PUNCT)
            # `@a.py.` 的句点；但保留 `@.env`、`@..` 这类以点开头/结尾的路径。
            while raw.endswith(".") and len(raw) > 1 and not raw.endswith(".."):
                raw = raw[:-1]
        raw = raw.strip()
        if raw and raw not in found:
            found.append(raw)
    return found


def resolve_mentions(agent, text, limit=MAX_MENTIONS):
    """把候选路径解析成工具调用，返回 (payloads, notices)。

    工作区外、被忽略目录里的路径静默跳过；像路径却找不到、二进制、太大、超出数量的给出提示。
    """
    payloads, notices, seen = [], [], set()
    for raw in parse_mentions(text):
        path = _resolve(agent, raw)
        if path is None:
            continue
        if path in seen or _ignored(agent, path):
            continue
        if not path.exists():
            if _PATH_LIKE_RE.search(raw):
                notices.append(f"没有找到 @{raw}，按普通文字发送。")
            continue
        if len(payloads) >= limit:
            notices.append(f"一条消息最多预读 {limit} 个 @ 文件，@{raw} 及之后的没有预读。")
            break
        relative = path.relative_to(agent.root).as_posix() or "."
        if path.is_dir():
            payloads.append({"name": "list_files", "args": {"path": relative}})
        elif path.is_file():
            problem = _unreadable(path)
            if problem:
                notices.append(f"@{raw} {problem}，没有预读。")
                continue
            payloads.append({"name": "read_file", "args": {"path": relative, "start": 1, "end": MENTION_READ_END}})
        else:
            continue
        seen.add(path)
    return payloads, notices


def preload_mentions(engine, task_state, user_message, mention_text):
    """在第一次模型调用前执行 @ 提及的读取。

    走和模型工具调用相同的执行/提交路径（历史、读取台账、事件、trace 都一样），
    但不记入 task_state.tool_steps，不占 max_steps，也不影响“还没用工具就说要继续”的检测。
    """
    agent = engine.runtime
    payloads, notices = resolve_mentions(agent, mention_text)
    for notice in notices:
        agent.session_event_bus.emit("runtime_notice", {"run_id": task_state.run_id, "content": notice, "source": "mention"})
        yield {"type": "runtime_notice", "run_id": task_state.run_id, "content": notice}
    if not payloads:
        return
    agent.emit_trace(task_state, "mentions_preloaded", {"tools": payloads, "notices": notices})
    for payload in payloads:
        if agent.abort_requested:
            return
        name, args = payload["name"], payload["args"]
        started_at = time.monotonic()
        agent.session_event_bus.emit(
            "tool_started", {"run_id": task_state.run_id, "tool_name": name, "args": args, "source": "mention"}
        )
        yield {"type": "tool_call", "run_id": task_state.run_id, "name": name, "args": args, "source": "mention"}
        record = _execute_tool(agent, name, args, started_at)
        record = replace(record, result=PRELOAD_NOTE.format(path=args["path"]) + record.result)
        for event in _commit_tool_record(engine, task_state, user_message, record):
            if event.get("type") == "tool_result":
                event = {**event, "source": "mention"}
            yield event


def _resolve(agent, raw):
    candidates = [raw]
    # `@calc.py里的bug` → `calc.py`；`@说明.md里的` → 逐个去掉末尾汉字，直到找到存在的文件。
    cjk = _CJK_RE.search(raw)
    if cjk and cjk.start() > 0:
        candidates.append(raw[: cjk.start()])
    trimmed = raw
    while trimmed and _CJK_RE.match(trimmed[-1]):
        trimmed = trimmed[:-1]
        if trimmed and trimmed not in candidates:
            candidates.append(trimmed)
    resolved = None
    for candidate in candidates:
        try:
            resolved = agent.path(candidate)
        except (ValueError, OSError):
            return None
        if resolved.exists():
            return resolved
    return resolved


def _ignored(agent, path):
    from .workspace import IGNORED_PATH_NAMES

    try:
        parts = path.relative_to(agent.root).parts
    except ValueError:
        return True
    return any(part in IGNORED_PATH_NAMES for part in parts)


def _unreadable(path):
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            head = handle.read(8192)
    except OSError as exc:
        return f"读取失败（{exc.strerror or exc}）"
    if size > MAX_MENTION_BYTES:
        return f"有 {size // 1024} KB，超过 {MAX_MENTION_BYTES // 1024} KB 上限"
    if b"\x00" in head:
        return "是二进制文件"
    return ""
