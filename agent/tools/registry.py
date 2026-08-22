from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.tools.base import Tool


@dataclass
class ToolMeta:
    #  风险等级：read-only、read-write、dangerous
    risk: str = "read-only"
    #  是不是这个工具每次都要发给模型
    always_on: bool = False
    #  给模型的搜索提示，模型可以用这个提示来搜索工具
    search_hint: str | None = None
    #  工具来源类型：builtin、plugin 项目提供的活着插件提供的
    source_type: str = "builtin"
    #  xx提供的这个工具
    source_name: str = ""


class ToolRegistry:
    """Akashic-style registry for builtin and plugin tools."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._metadata: dict[str, ToolMeta] = {}

    def register(
        self,
        tool: Tool,
        *,
        risk: str = "read-only",
        always_on: bool = False,
        search_hint: str | None = None,
        source_type: str = "builtin",
        source_name: str = "",
    ) -> None:
        self._tools[tool.name] = tool
        self._metadata[tool.name] = ToolMeta(
            risk=risk,
            always_on=always_on,
            search_hint=search_hint,
            source_type=source_type,
            source_name=source_name,
        )

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)
        self._metadata.pop(name, None)

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def get_tool(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def get_metadata(self, name: str) -> ToolMeta | None:
        return self._metadata.get(name)

    def get_registered_names(self) -> set[str]:
        return set(self._tools.keys())

    def get_schemas(self, names: set[str] | None = None) -> list[dict[str, Any]]:
        selected = self._tools.items()
        if names is not None:
            selected = [(name, tool) for name, tool in selected if name in names]
        return [tool.to_schema() for _, tool in selected]

    async def execute(self, name: str, arguments: dict[str, Any], ctx: Any = None) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            return f"工具 '{name}' 不存在"
        return await tool.execute(arguments, ctx)
