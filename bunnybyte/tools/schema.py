"""Provider-native tool schema helpers."""

from __future__ import annotations

import re


_OPTIONAL_MARKER = "?"
_DEFAULT_PATTERN = re.compile(r"^(?P<type>[^=]+)=(?P<default>.*)$")


def tool_parameters_schema(schema: dict) -> dict:
    """Convert Bunny Byte's compact tool schema to JSON Schema parameters."""
    properties = {}
    required = []
    for name, spec in (schema or {}).items():
        spec_text = str(spec or "str")
        optional = spec_text.endswith(_OPTIONAL_MARKER)
        if optional:
            spec_text = spec_text[: -len(_OPTIONAL_MARKER)]
        default_match = _DEFAULT_PATTERN.match(spec_text)
        default = None
        has_default = False
        if default_match:
            spec_text = default_match.group("type")
            default = default_match.group("default")
            has_default = True
        json_type = _json_type(spec_text)
        property_schema = {"type": json_type}
        if json_type == "array":
            property_schema["items"] = {"type": "string"}
        if has_default:
            property_schema["default"] = _coerce_default(default, json_type)
        properties[str(name)] = property_schema
        if not optional and not has_default:
            required.append(str(name))
    parameters = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        parameters["required"] = required
    return parameters


def native_tool_specs(tools: dict, protocol: str) -> list[dict]:
    """Render registered tools as Anthropic or OpenAI native tool specs."""
    rendered = []
    for name in sorted(tools):
        tool = tools[name]
        parameters = tool_parameters_schema(tool.schema)
        description = str(tool.description or "")
        if protocol == "anthropic":
            rendered.append(
                {
                    "name": name,
                    "description": description,
                    "input_schema": parameters,
                }
            )
        else:
            rendered.append(
                {
                    "type": "function",
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                }
            )
    return rendered


def _json_type(spec_type: str) -> str:
    normalized = str(spec_type or "str").strip().lower()
    if normalized.startswith("list"):
        return "array"
    if normalized in {"int", "integer"}:
        return "integer"
    if normalized in {"float", "number"}:
        return "number"
    if normalized in {"bool", "boolean"}:
        return "boolean"
    if normalized in {"dict", "object"}:
        return "object"
    return "string"


def _coerce_default(value: str, json_type: str):
    text = str(value)
    if json_type == "array":
        return []
    if json_type == "integer":
        try:
            return int(text)
        except ValueError:
            return 0
    if json_type == "number":
        try:
            return float(text)
        except ValueError:
            return 0.0
    if json_type == "boolean":
        return text.lower() in {"1", "true", "yes"}
    return text.strip("'\"")
