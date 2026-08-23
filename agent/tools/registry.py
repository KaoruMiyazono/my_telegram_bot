from __future__ import annotations

import json
import re
from dataclasses import dataclass
from collections.abc import Sequence
from typing import Any, Literal

from agent.tools.base import Tool
from agent.tools.discovery import ToolDiscoveryState

ToolTier = Literal["meta", "core", "extended"]


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
    # 工具可见性层级；三层共享同一个 ToolRuntime。
    tier: ToolTier = "extended"
    # 是否允许通过 Session LRU 在下一轮小范围预加载。
    preloadable: bool = True
    # 运行时可用开关；禁用工具不会暴露、搜索或执行。
    enabled: bool = True


class ToolRegistry:
    """Akashic-style registry for builtin and plugin tools."""

    def __init__(
        self,
        *,
        initial_max_tools: int = 8,
        initial_schema_char_budget: int = 12_000,
        session_lru_size: int = 4,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        self._metadata: dict[str, ToolMeta] = {}
        self.initial_max_tools = max(1, int(initial_max_tools))
        self.initial_schema_char_budget = max(256, int(initial_schema_char_budget))
        self.discovery = ToolDiscoveryState(capacity=max(0, int(session_lru_size)))

    def register(
        self,
        tool: Tool,
        *,
        risk: str = "read-only",
        always_on: bool = False,
        search_hint: str | None = None,
        source_type: str = "builtin",
        source_name: str = "",
        tier: ToolTier | None = None,
        preloadable: bool = True,
        enabled: bool = True,
    ) -> None:
        resolved_tier: ToolTier = tier or ("meta" if always_on else "extended")
        self._tools[tool.name] = tool
        self._metadata[tool.name] = ToolMeta(
            risk=risk,
            always_on=always_on,
            search_hint=search_hint,
            source_type=source_type,
            source_name=source_name,
            tier=resolved_tier,
            preloadable=preloadable,
            enabled=enabled,
        )

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)
        self._metadata.pop(name, None)
        self.discovery.forget_tool(name)

    def replace_source_tools(
        self,
        *,
        source_type: str,
        source_name: str,
        tools: Sequence[tuple[Tool, str]],
    ) -> tuple[str, ...]:
        """Atomically replace every tool published by one runtime source.

        A complete candidate catalog is validated in copies first. Readers
        therefore observe either the old MCP generation or the new one, never
        a half-updated tools/list response.
        """

        old_names = {
            name
            for name, meta in self._metadata.items()
            if meta.source_type == source_type and meta.source_name == source_name
        }
        new_names = [tool.name for tool, _ in tools]
        if len(set(new_names)) != len(new_names):
            raise ValueError(f"Duplicate tool names from source {source_name}")
        collisions = (set(new_names) - old_names) & set(self._tools)
        if collisions:
            raise ValueError(
                "Tool name collision: " + ", ".join(sorted(collisions))
            )

        next_tools = dict(self._tools)
        next_metadata = dict(self._metadata)
        for name in old_names:
            next_tools.pop(name, None)
            next_metadata.pop(name, None)
        for tool, risk in tools:
            next_tools[tool.name] = tool
            next_metadata[tool.name] = ToolMeta(
                risk=risk,
                always_on=False,
                search_hint=tool.description,
                source_type=source_type,
                source_name=source_name,
                tier="extended",
                preloadable=True,
                enabled=True,
            )

        self._tools = next_tools
        self._metadata = next_metadata
        for name in old_names - set(new_names):
            self.discovery.forget_tool(name)
        return tuple(new_names)

    def unregister_source(self, *, source_type: str, source_name: str) -> tuple[str, ...]:
        """Atomically remove all tools owned by a source."""

        names = tuple(
            name
            for name, meta in self._metadata.items()
            if meta.source_type == source_type and meta.source_name == source_name
        )
        if not names:
            return ()
        next_tools = dict(self._tools)
        next_metadata = dict(self._metadata)
        for name in names:
            next_tools.pop(name, None)
            next_metadata.pop(name, None)
        self._tools = next_tools
        self._metadata = next_metadata
        for name in names:
            self.discovery.forget_tool(name)
        return names

    def has_tool(self, name: str) -> bool:
        meta = self._metadata.get(name)
        return name in self._tools and bool(meta is None or meta.enabled)

    def get_tool(self, name: str) -> Tool | None:
        if not self.has_tool(name):
            return None
        return self._tools.get(name)

    def get_metadata(self, name: str) -> ToolMeta | None:
        return self._metadata.get(name)

    def get_registered_names(self) -> set[str]:
        return set(self._tools.keys())

    def get_schemas(self, names: set[str] | None = None) -> list[dict[str, Any]]:
        selected = [
            (name, tool)
            for name, tool in self._tools.items()
            if self._metadata.get(name, ToolMeta()).enabled
        ]
        if names is not None:
            selected = [(name, tool) for name, tool in selected if name in names]
        return [tool.to_schema() for _, tool in selected]

    #  选工具 1.meta 2.根据query选出来的 3.热点工具 4.这些工具附带的
    def get_initial_schemas(self, *, query: str, session_key: str) -> list[dict[str, Any]]:
        """Project a bounded Meta + relevant Core + Session LRU tool set."""

        ordered: list[str] = []
        for name, meta in self._metadata.items():
            if meta.enabled and (meta.tier == "meta" or meta.always_on):
                _append_unique(ordered, name)

        matched_core = self.search(
            query,
            top_k=self.initial_max_tools,
            tiers={"core"},
            excluded_names=set(ordered),
        )
        matched_sources: set[tuple[str, str]] = set()
        for item in matched_core:
            _append_unique(ordered, str(item["name"]))
            matched_sources.add(
                (str(item.get("source_type") or ""), str(item.get("source_name") or ""))
            )

        # Core tools from the same source often form a protocol pair, such as
        # web_search -> web_fetch. Keep that small workflow intact; the final
        # count/character budget below still caps the projected schemas.
        for name, meta in self._metadata.items():
            if (
                meta.enabled
                and meta.tier == "core"
                and (meta.source_type, meta.source_name) in matched_sources
            ):
                _append_unique(ordered, name)

        for name in self.discovery.get_preloaded(session_key):
            meta = self._metadata.get(name)
            if meta is not None and meta.enabled and meta.preloadable:
                _append_unique(ordered, name)

        return self._bounded_schemas(ordered)

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        excluded_names: set[str] | None = None,
        tiers: set[ToolTier] | None = None,
    ) -> list[dict[str, Any]]:
        """Search the enabled non-meta catalog using deterministic keyword scoring."""

        query = str(query or "").strip()
        if not query:
            return []
        excluded = set(excluded_names or ()) | {"tool_search"}
        terms = _query_terms(query)
        ranked: list[tuple[int, int, dict[str, Any]]] = []
        for order, (name, tool) in enumerate(self._tools.items()):
            meta = self._metadata.get(name, ToolMeta())
            if (
                name in excluded
                or not meta.enabled
                or meta.tier == "meta"
                or (tiers is not None and meta.tier not in tiers)
            ):
                continue
            haystack = " ".join(
                [name, tool.description, meta.search_hint or "", meta.source_name]
            ).lower()
            score = _catalog_score(query.lower(), terms, name.lower(), haystack)
            if score <= 0:
                continue
            ranked.append(
                (
                    -score,
                    order,
                    {
                        "name": name,
                        "summary": tool.description,
                        "why_matched": _why_matched(terms, haystack),
                        "risk": meta.risk,
                        "tier": meta.tier,
                        "always_on": meta.always_on,
                        "preloadable": meta.preloadable,
                        "source_type": meta.source_type,
                        "source_name": meta.source_name,
                    },
                )
            )
        ranked.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in ranked[: max(1, min(int(top_k), 10))]]

    def remember_tool_use(self, session_key: str, tool_name: str) -> None:
        meta = self._metadata.get(tool_name)
        if (
            meta is None
            or not meta.enabled
            or not meta.preloadable
            or meta.tier == "meta"
            or meta.always_on
        ):
            return
        self.discovery.remember(session_key, tool_name)

    def _bounded_schemas(self, ordered_names: list[str]) -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        used_chars = 0
        for name in ordered_names:
            tool = self.get_tool(name)
            if tool is None:
                continue
            schema = tool.to_schema()
            schema_chars = len(json.dumps(schema, ensure_ascii=False, sort_keys=True))
            meta = self._metadata.get(name, ToolMeta())
            mandatory = meta.tier == "meta" or meta.always_on
            if not mandatory and (
                len(schemas) >= self.initial_max_tools
                or used_chars + schema_chars > self.initial_schema_char_budget
            ):
                continue
            schemas.append(schema)
            used_chars += schema_chars
        return schemas

    async def execute(self, name: str, arguments: dict[str, Any], ctx: Any = None) -> Any:
        tool = self._tools.get(name)
        if tool is None or not self.has_tool(name):
            return f"工具 '{name}' 不存在"
        return await tool.execute(arguments, ctx)


def _append_unique(items: list[str], name: str) -> None:
    if name and name not in items:
        items.append(name)


def _query_terms(query: str) -> list[str]:
    chunks = [part for part in re.split(r"[^\w\u4e00-\u9fff]+", query.lower()) if part]
    terms: list[str] = []
    for chunk in chunks:
        _append_unique(terms, chunk)
        if re.fullmatch(r"[\u4e00-\u9fff]{3,}", chunk):
            for index in range(len(chunk) - 1):
                _append_unique(terms, chunk[index : index + 2])
    return terms


def _catalog_score(query: str, terms: list[str], name: str, haystack: str) -> int:
    if query == name or query == f"select:{name}":
        return 1000
    score = 0
    if query in haystack:
        score += 100
    for term in terms:
        if term == name:
            score += 80
        elif term in name:
            score += 40
        elif term in haystack:
            score += max(3, min(20, len(term) * 3))
    return score


def _why_matched(terms: list[str], haystack: str) -> str:
    matched = [term for term in terms if term in haystack]
    return "关键词: " + ", ".join(matched[:5]) if matched else "名称或功能描述匹配"
