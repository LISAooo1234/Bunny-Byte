"""Tool abstraction shared by the runtime and prompt builder."""

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    schema: dict
    description: str
    risky: bool
    runner: Callable[[dict], str]
    parallel_safe: bool = False
    json_schema: dict | None = None  # MCP 等外部工具自带的 JSON Schema，原生工具调用时原样传给 provider

    @property
    def read_only(self):
        return not self.risky

    def execute(self, args):
        result = self.runner(args)
        if isinstance(result, ToolResult):
            return result
        return ToolResult(content=str(result))

    def __getitem__(self, key):
        if key == "run":
            return self.runner
        return getattr(self, key)
