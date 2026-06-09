"""Testing helpers for deterministic BunnyByte runtime checks."""

from .providers.base import ModelResult, ModelToolCall


class ScriptedModelClient:
    def __init__(self, outputs, supports_native_tools=False, native_tool_protocol="openai"):
        self.outputs = list(outputs)
        self.prompts = []
        self.tool_batches = []
        self.supports_prompt_cache = False
        self.supports_native_tools = bool(supports_native_tools)
        self.native_tool_protocol = native_tool_protocol
        self.last_completion_metadata = {}
        self.deterministic_scripted = True

    def complete(self, prompt, max_new_tokens, **kwargs):
        self.prompts.append(prompt)
        self.tool_batches.append(kwargs.get("tools"))
        if not getattr(self, "last_completion_metadata", None):
            self.last_completion_metadata = {}
        if not self.outputs:
            raise RuntimeError("scripted model ran out of outputs")
        output = self.outputs.pop(0)
        if isinstance(output, BaseException):
            raise output
        if isinstance(output, ModelResult):
            self.last_completion_metadata = dict(output.metadata or {})
            if output.tool_calls:
                self.last_completion_metadata["tool_calls"] = list(output.tool_calls)
            return output.text
        if isinstance(output, ModelToolCall):
            self.last_completion_metadata = {"tool_calls": [output]}
            return ""
        if isinstance(output, list) and all(
            isinstance(item, ModelToolCall) for item in output
        ):
            self.last_completion_metadata = {"tool_calls": list(output)}
            return ""
        if "tool_calls" in self.last_completion_metadata:
            self.last_completion_metadata = {}
        return output

    def complete_result(self, prompt, max_new_tokens, **kwargs):
        text = self.complete(prompt, max_new_tokens, **kwargs)
        return ModelResult(
            text=text,
            metadata=dict(self.last_completion_metadata),
            tool_calls=list(self.last_completion_metadata.get("tool_calls", [])),
        )
