from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

ToolHandler = Callable[[dict[str, Any], Any], Awaitable[Any] | Any]


@dataclass
class ToolResult:
    text: str = ""
    data: Any = None
    content_blocks: list[dict[str, Any]] = field(default_factory=list)

    def preview(self) -> str:
        if self.text:
            return self.text
        if self.data is not None:
            return str(self.data)
        if self.content_blocks:
            return f"[多模态结果 {len(self.content_blocks)} blocks]"
        return ""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    timeout_s: float = 30.0
    idempotent: bool = True
    retry_count: int = 0
    output_schema: dict[str, Any] | None = None

    async def execute(self, arguments: dict[str, Any], ctx: Any = None) -> str | ToolResult:
        result = self.handler(arguments, ctx)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, ToolResult):
            return result
        if isinstance(result, str):
            return result
        return str(result)
    #  发给LLM的只有 name、description、parameters，其他属性不发给LLM
    def to_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
