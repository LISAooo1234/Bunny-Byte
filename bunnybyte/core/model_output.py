"""Parser for BunnyByte's text model protocol."""

import json
import re


def parse(raw):
    raw = str(raw)
    stripped = _strip_protocol_fence(raw).lstrip()
    if stripped.startswith("<tool"):
        parsed = parse_tool_blocks(stripped)
        if isinstance(parsed, str):
            return "retry", retry_notice(parsed)
        if parsed:
            return _tool_kind(parsed)
        return "retry", retry_notice("tool payload must be valid JSON or supported XML")

    if stripped.startswith("<final>"):
        return "final", extract(stripped, "final")

    if not raw.strip():
        return "retry", retry_notice("empty response")
    return "retry", retry_notice("missing leading <tool> or <final> protocol tag")


def _strip_protocol_fence(raw):
    text = str(raw or "").strip()
    match = re.fullmatch(r"```(?:xml|html|text)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else str(raw)


def retry_notice(problem=None):
    detail = f" Problem: {problem}." if problem else ""
    return (
        "Your previous response could not be executed."
        f"{detail} Return one or more valid <tool> calls, or one <final> answer."
    )


def normalize_tool_payload(payload):
    if isinstance(payload, list):
        if not payload:
            return "tool JSON list must not be empty"
        normalized = []
        for item in payload:
            parsed = normalize_tool_payload(item)
            if isinstance(parsed, str):
                return parsed
            normalized.extend(parsed)
        return normalized
    if not isinstance(payload, dict) or "name" not in payload:
        return "tool JSON must be an object with name and args"
    args = payload.get("args", {})
    if not isinstance(args, dict):
        return "tool args must be an object"
    return [{"name": payload["name"], "args": args}]


def parse_tool_blocks(raw):
    tools = []
    errors = []
    for match in re.finditer(
        r"<tool\b(?P<attrs>[^>]*)>(?P<body>.*?)</tool>", str(raw), flags=re.DOTALL
    ):
        attrs = parse_attrs(match.group("attrs"))
        if attrs.get("name", "").strip():
            parsed_xml = parse_xml_tool_match(match)
            if parsed_xml:
                tools.append(parsed_xml)
            continue
        body = match.group("body").strip()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            errors.append("tool payload must be valid JSON or supported XML")
            continue
        parsed_json = normalize_tool_payload(payload)
        if isinstance(parsed_json, str):
            errors.append(parsed_json)
            continue
        tools.extend(parsed_json)
    if tools:
        return tools
    if errors:
        return errors[0]
    return []


def _tool_kind(tools):
    if len(tools) == 1:
        return "tool", tools[0]
    return "tools", tools


def parse_xml_tools(raw):
    tools = []
    for match in re.finditer(
        r"<tool\b(?P<attrs>[^>]*)>(?P<body>.*?)</tool>", str(raw), flags=re.DOTALL
    ):
        parsed = parse_xml_tool_match(match)
        if parsed:
            tools.append(parsed)
    return tools


def parse_xml_tool(raw):
    match = re.search(
        r"<tool\b(?P<attrs>[^>]*)>(?P<body>.*?)</tool>", str(raw), flags=re.DOTALL
    )
    if not match:
        return None
    return parse_xml_tool_match(match)


def parse_xml_tool_match(match):
    attrs = parse_attrs(match.group("attrs"))
    body = match.group("body")
    name = attrs.get("name", "").strip()
    if not name:
        return None
    args = {key: value for key, value in attrs.items() if key != "name"}
    for tag in ("content", "old_text", "new_text"):
        value = extract_raw(body, tag)
        if value is not None:
            args[tag] = value
    if name == "write_file" and "content" not in args and body.strip():
        args["content"] = body
    return {"name": name, "args": args}


def parse_attrs(text):
    attrs = {}
    for key, value in re.findall(
        r'([A-Za-z_][A-Za-z0-9_-]*)="(.*?)"', text, flags=re.DOTALL
    ):
        attrs[key] = value
    return attrs


def extract(text, tag):
    match = re.search(rf"<{tag}>(.*?)</{tag}>", text, flags=re.DOTALL)
    if not match:
        return text.strip()
    return match.group(1).strip()


def extract_raw(text, tag):
    match = re.search(rf"<{tag}>(.*?)</{tag}>", text, flags=re.DOTALL)
    if not match:
        return None
    return match.group(1)
