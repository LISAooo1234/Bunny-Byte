"""救回写成文字的工具调用（provider 原生工具调用模式）。

原生工具模式下，回复里没有 tool_calls 时整段文字会被当成最终回答。模型偶尔把调用写进正文：DeepSeek V4
写 `name{json}`（deepseek-ai/DeepSeek-V3#1244）或 DSML `<｜DSML｜invoke name=…>`，Qwen 系写
`<tool_call>{json}</tool_call>` 或 `<function=x><parameter=k>`，也有模型模仿 `<tool>` / 历史记录里的
`[tool:x] {json}`。以前回合就此结束，用户看到的“回答”是一段标记。现在：
- 标记在回复末尾、不在 ``` 代码块里、工具名在本次请求实际提供的工具里 → 按工具调用执行，前面的文字作为前言；
- 只有纯读取的 read_file / list_files / search 直接执行；其他工具（run_shell、write_file、remember、agent、
  mcp__* 等）只在有人审批时执行（`--approval ask`、主 agent、不是 `bunny -p`，不需要审批的工具也单独问一次），
  auto / never / 无人值守 / 子 agent 下退回模型，让它用原生接口重新调用；
- 标记和本会话里某条工具结果或用户消息中的文字相同（可能是文件里的注入被模型照抄）、工具不可用、参数解析
  不了、整条回复是裸 JSON、模仿历史记录格式 → 不执行，退回重试；每轮最多退回 2 次，之后按回答收下；
- 用户这条消息自己就带这类标记时（在问格式、贴了例子）不处理，原样当回答。
"""

import json
import re
from dataclasses import dataclass, field

from .workspace import clip

MAX_RETRIES = 2
RECEIPT_RUNS = 20
RETRY_PREFIX = "Your previous response could not be executed."
USER_MARKERS = ("<tool", "DSML", "<invoke", "<function", "[tool:", "<function_calls")
ARG_KEYS = ("arguments", "args", "parameters", "input")
MIN_ECHO_CHARS = 16
MAX_CANDIDATES = 256  # 只试离结尾最近的这么多个起点：标记必须在回复末尾，长回复里成千上万个 `<tool` 不能拖慢每次调用
DECODER = json.JSONDecoder()
FENCE_RE = re.compile(r"```.*?(?:```|\Z)|~~~.*?(?:~~~|\Z)|(?m:^(?: {4}|\t|[ \t]*>).*$)", re.DOTALL)
DIRECT_TOOLS = frozenset({"read_file", "list_files", "search"})  # 只读、没有副作用：写成文字也直接执行

D = r"(?:[｜|]{1,2}\s*DSML\s*[｜|]{1,2}\s*)?"  # ｜DSML｜、｜｜DSML｜｜ 、||DSML|| ，或者没有（裸 <invoke>）
DSML_START = re.compile(rf"<{D}(?:function_calls|tool_calls|calls|invoke)\b")
DSML_WRAP_OPEN = re.compile(rf"\s*<{D}(?:function_calls|tool_calls|calls)\s*>")
DSML_INVOKE = re.compile(rf'\s*<{D}invoke\s+name="([^"]+)"\s*>')
DSML_PARAM = re.compile(
    rf'\s*<{D}parameter\s+name="([^"]+)"(?:\s+string="(true|false)")?\s*>(.*?)</{D}parameter\s*>', re.DOTALL
)
DSML_INVOKE_CLOSE = re.compile(rf"\s*(?:</{D}invoke\s*>)?")
DSML_WRAP_CLOSE = re.compile(rf"\s*(?:</{D}(?:function_calls|tool_calls|calls)\s*>)?")
HERMES_START = re.compile(r"<tool_call>(?!\s*<function=)")
HERMES_OPEN = re.compile(r"\s*<tool_call>\s*")
HERMES_CLOSE = re.compile(r"\s*(?:</tool_call>)?")
QWEN_START = re.compile(r"(?:<tool_call>\s*)?<function=")
QWEN_FUNC = re.compile(r"\s*(?:<tool_call>\s*)?<function=([^>\s]+)>")
QWEN_PARAM = re.compile(r"\s*<parameter=([^>\s]+)>")
QWEN_VALUE_END = re.compile(r"</parameter>|<parameter=|</function>|</tool_call>|\Z")
QWEN_CLOSE = re.compile(r"\s*(?:</function>)?\s*(?:</tool_call>)?")
BUNNY_START = re.compile(r'<tool(?=\s+name="|\s*>\s*[{\[])')
BUNNY_BLOCK = re.compile(r'\s*<tool\b(?P<attrs>(?:[^>"]|"[^"]*")*)>(?P<body>.*?)</tool>', re.DOTALL)
LABEL_START = re.compile(r"(?m)^[ \t]*\[tool:([\w.\-]+)\][ \t]*(?=\{)")
CLOSERS = ("</tool_call>", "</function>", "</parameter>", "invoke>", "calls>", "</tool>", "}")


class _Broken(ValueError):
    pass


@dataclass
class Found:
    format: str
    start: int
    calls: list = field(default_factory=list)
    problem: str = ""
    retry_only: bool = False


def _call(name, args):
    if not isinstance(name, str) or not name.strip():
        raise _Broken("tool call has no tool name")
    if isinstance(args, str):  # OpenAI 风格：arguments 是 JSON 字符串，要再解一次
        try:
            args = json.loads(args) if args.strip() else {}
        except ValueError:
            raise _Broken("tool arguments are not valid JSON") from None
    if not isinstance(args, dict):
        raise _Broken("tool arguments must be a JSON object")
    return {"name": name.strip(), "args": args}


def _from_object(obj):
    if isinstance(obj, dict) and isinstance(obj.get("function"), dict):
        obj = obj["function"]
    if not isinstance(obj, dict):
        raise _Broken("tool call JSON must be an object")
    key = next((key for key in ARG_KEYS if key in obj), None)
    return _call(obj.get("name"), obj.get(key, {}) if key else {})


def _decode(text, pos):
    try:
        return DECODER.raw_decode(text, pos)
    except ValueError:
        raise _Broken("tool call JSON is not valid") from None


def _hermes_at(text, pos):
    calls = []
    while match := HERMES_OPEN.match(text, pos):
        obj, pos = _decode(text, match.end())
        calls.append(_from_object(obj))
        pos = HERMES_CLOSE.match(text, pos).end()
    return calls, pos


def _param_text(value):
    stripped = value.strip()
    if "\n" not in stripped:
        return stripped
    value = value[1:] if value.startswith("\n") else value
    return value[:-1] if value.endswith("\n") else value


def _qwen_at(text, pos):
    calls = []
    while match := QWEN_FUNC.match(text, pos):
        name, pos, args = match.group(1), match.end(), {}
        while param := QWEN_PARAM.match(text, pos):
            end = QWEN_VALUE_END.search(text, param.end())
            args[param.group(1)] = _param_text(text[param.end() : end.start()])
            pos = end.end() if end.group(0) == "</parameter>" else end.start()
        pos = QWEN_CLOSE.match(text, pos).end()
        calls.append(_call(name, args))
    return calls, pos


def _dsml_value(value, string_flag):
    if string_flag == "false":
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value if string_flag == "true" else _param_text(value)


def _dsml_at(text, pos):
    if wrap := DSML_WRAP_OPEN.match(text, pos):
        pos = wrap.end()
    calls = []
    while match := DSML_INVOKE.match(text, pos):
        name, pos, args = match.group(1), match.end(), {}
        while param := DSML_PARAM.match(text, pos):
            args[param.group(1)] = _dsml_value(param.group(3), param.group(2))
            pos = param.end()
        pos = DSML_INVOKE_CLOSE.match(text, pos).end()
        calls.append(_call(name, args))
    if not calls:
        raise _Broken("tool call markup has no invoke block")
    return calls, DSML_WRAP_CLOSE.match(text, pos).end()


def _bunny_at(text, pos):
    from .model_output import parse_attrs, parse_xml_tool_match

    if text.find("</tool>", pos) < 0:
        raise _Broken("tool call markup is not closed")  # 先查闭合标签：没有的话属性正则会扫到文末
    calls = []
    while match := BUNNY_BLOCK.match(text, pos):
        if parse_attrs(match.group("attrs")).get("name", "").strip():
            calls.append(parse_xml_tool_match(match))
        else:
            body = match.group("body").strip()
            obj, end = _decode(body, 0)
            if body[end:].strip():
                raise _Broken("tool call JSON is not valid")
            calls.extend(_from_object(item) for item in (obj if isinstance(obj, list) else [obj]))
        pos = match.end()
    if not calls:
        raise _Broken("tool call markup is not closed")
    return calls, pos


def _named_json_at(name):
    def parse(text, pos):
        calls = []
        while text.startswith(name, pos):
            brace = text.index("{", pos + len(name))
            args, pos = _decode(text, brace)
            calls.append(_call(name, args))
            pos = re.compile(r"\s*").match(text, pos).end()
        return calls, pos

    return parse


def _label_at(text, pos):
    calls = []
    while match := LABEL_START.match(text, pos):
        args, pos = _decode(text, match.end())
        calls.append(_call(match.group(1), args))
        pos = re.compile(r"\s*").match(text, pos).end()
    return calls, pos


def _masked(text):
    """代码块里的内容换成不会匹配任何标记的字符，长度不变：块里的例子不执行，块本身仍算“后面还有内容”。"""
    return FENCE_RE.sub(lambda match: "\x00" * len(match.group(0)), text)


def _candidates(text, tool_names):
    yield from ((m.start(), "dsml", _dsml_at) for m in DSML_START.finditer(text))
    yield from ((m.start(), "hermes", _hermes_at) for m in HERMES_START.finditer(text))
    yield from ((m.start(), "qwen_xml", _qwen_at) for m in QWEN_START.finditer(text))
    yield from ((m.start(), "bunny_tag", _bunny_at) for m in BUNNY_START.finditer(text))
    yield from ((m.start(), "history_label", _label_at) for m in LABEL_START.finditer(text))
    if tool_names:
        names = "|".join(re.escape(name) for name in sorted(tool_names, key=len, reverse=True))
        # 只认行首或句末之后（DeepSeek 报告里是“……指标。\nname{…}”）；“可以用 read_file {…}”这类句中提到的不算
        pattern = re.compile(rf"(?:^[ \t]*|(?<=[。！？；：])|(?<=[.!?;:][ \t]))({names})[ \t]*(?=\{{)", re.MULTILINE)
        yield from ((m.start(1), "name_json", _named_json_at(m.group(1))) for m in pattern.finditer(text))


def find(text, tool_names=()):
    """回复末尾的文字工具调用；没有就返回 None。"""
    raw = str(text or "").rstrip()
    masked = _masked(raw)  # 只用来找起点、判断后面还有没有正文；参数一律从原文解析（代码块可能就在参数里）
    tool_names = set(tool_names)
    whole = raw.strip()
    if whole[:1] in "{[" and whole:
        found = _bare_json(whole, raw.find(whole[:1]), tool_names)
        if found:
            return found
    broken = None
    candidates = sorted(_candidates(masked, tool_names), key=lambda item: item[0])[-MAX_CANDIDATES:]
    for index, (start, fmt, parser) in enumerate(candidates):
        try:
            calls, end = parser(raw, start)
        except (_Broken, ValueError) as exc:
            if broken is None and index == len(candidates) - 1 and _own_line(raw, start) and raw.endswith(CLOSERS):
                broken = Found(fmt, start, problem=str(exc) if isinstance(exc, _Broken) else "tool call markup is not valid")
            continue
        if calls and not masked[end:].strip():
            if fmt == "history_label" and not all(call["name"] in tool_names for call in calls):
                continue
            return Found(fmt, start, calls, retry_only=fmt == "history_label")
    return broken


def _own_line(text, start):
    """坏掉的标记只有自成一行时才退回：正文里顺带提到 `<tool_call>`、最后恰好以 `}` 结尾的回答照常收下。"""
    return not text[text.rfind("\n", 0, start) + 1 : start].strip()


def _bare_json(whole, start, tool_names):
    try:
        obj, end = DECODER.raw_decode(whole)
    except ValueError:
        return None
    if whole[end:].strip():
        return None
    items = obj if isinstance(obj, list) else [obj]
    if not items or not all(
        isinstance(item, dict) and item.get("name") in tool_names and any(key in item for key in ARG_KEYS) for item in items
    ):  # 名字不是当前工具的（例如用户要的工具定义示例）就是普通 JSON 回答
        return None
    try:
        calls = [_from_object(item) for item in items]
    except _Broken:
        return None
    return Found("bare_json", start, calls, retry_only=True)  # 整条回复是 JSON 也可能是用户要的示例：只退回，不执行


def _compact(text):
    return re.sub(r"\s+", "", str(text or ""))


def _echoed(markup, history):
    needle = _compact(markup)
    if len(needle) < MIN_ECHO_CHARS:
        return False
    return any(
        item.get("role") in {"tool", "user"} and needle in _compact(item.get("content", ""))
        for item in history
    )


def _human_reviews(agent):
    return (
        agent.approval_policy == "ask"
        and not getattr(agent, "unattended", False)
        and int(getattr(agent, "depth", 0) or 0) == 0
        and not agent.read_only
    )


def _risky(agent, name):
    tool = agent.available_tools().get(name)
    return tool is None or bool(tool["risky"])


def _needs_review(name):
    return name not in DIRECT_TOOLS


def retry_text(problem, markup):
    preview = repr(clip(re.sub(r"\s+", " ", markup).strip(), 180))
    return (
        f"{RETRY_PREFIX} Problem: {problem}. Offending output preview: {preview} "
        "Nothing ran. Call the tool through the native tool-calling interface instead of writing it in your reply, "
        "or answer normally without tool-call markup."
    )


class TextToolRescue:
    """一轮里的文字工具调用处理；退回重试的次数按轮计。"""

    def __init__(self, agent, task_state, user_message):
        self.agent = agent
        self.task_state = task_state
        self.skip = any(marker in str(user_message or "") for marker in USER_MARKERS)
        self.retries = 0
        self.offered = set()

    def classify(self, raw, native_tools):
        """返回 (kind, payload, parse_metadata)；不是文字工具调用时就是普通回答。"""
        text = str(raw or "").strip()
        if not text:
            return "retry", "Return a normal final answer or request a native tool call.", {}
        if self.skip:
            return "final", text, {}
        self.offered = {spec.get("name") for spec in native_tools or []}
        found = find(text, self.offered)
        if found is None:
            return "final", text, {}
        markup = text[found.start :].strip() if found.start < len(text) else text
        names = [call["name"] for call in found.calls]
        problem = found.problem or self._problem(found, names, markup)
        if not problem:
            problem = self._ask_human(found.calls)
        if not problem:
            preamble = text[: found.start].strip()
            self._receipt(found.format, "rescued", names)
            calls = found.calls
            payload = calls[0] if len(calls) == 1 else calls
            metadata = {"repair": "text_tool_call", "text_tool_format": found.format}
            if preamble:
                metadata["preamble"] = preamble
            return ("tool" if len(calls) == 1 else "tools"), payload, metadata
        if self.retries >= MAX_RETRIES:
            self._receipt(found.format, "accepted", names, problem)
            return "final", text, {}
        self.retries += 1
        self._receipt(found.format, "retry", names, problem)
        return "retry", retry_text(problem, markup), {"text_tool_format": found.format}

    def _problem(self, found, names, markup):
        if found.retry_only:
            return f"a tool call was written as text ({found.format}), it did not run"
        unknown = [name for name in names if name not in self.offered]
        if unknown:
            return f"a tool call was written as text and names a tool that is not available now: {', '.join(unknown)}"
        if _echoed(markup, self.agent.session.get("history", [])):
            return "a tool call was written as text and is a copy of text from a file, command output or message, so it was not run"
        if any(_needs_review(name) for name in names) and not _human_reviews(self.agent):
            return (
                f"a tool call to {', '.join(names)} was written as text; tools other than read_file, list_files "
                "and search only run from text with a human approving"
            )
        return ""

    def _ask_human(self, calls):
        """不需要审批的工具（remember、agent、todo_* 等）照常不会弹窗：写成文字的这一次单独问。高风险工具交给正常审批。"""
        for call in calls:
            name = call["name"]
            if _needs_review(name) and not _risky(self.agent, name) and not self.agent.approve(name, dict(call["args"])):
                return f"a tool call to {name} was written as text and the user did not approve running it"
        return ""

    def _receipt(self, fmt, decision, names, problem=""):
        entry = {"format": fmt, "decision": decision, "tools": names, "problem": problem}
        receipts = getattr(self.agent, "text_tool_receipts", None)
        if receipts is None:
            receipts = self.agent.text_tool_receipts = {}
        run_id = self.task_state.run_id
        if run_id not in receipts and len(receipts) >= RECEIPT_RUNS:
            receipts.pop(next(iter(receipts)))
        receipts.setdefault(run_id, []).append(entry)
        self.agent.emit_trace(self.task_state, "text_tool_call", dict(entry, status=decision))


def text_tool_receipt(agent, state):
    if state is None:
        return []
    return list((getattr(agent, "text_tool_receipts", None) or {}).get(state.run_id, []))
