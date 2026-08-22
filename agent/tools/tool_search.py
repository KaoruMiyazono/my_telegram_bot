from __future__ import annotations

import json
from typing import Any

from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry


def register_tool_search(registry: ToolRegistry) -> None:
    """Register the Meta tool that discovers and unlocks deferred schemas."""

    async def handler(arguments: dict[str, Any], ctx: Any = None) -> str:
        query = str(arguments.get("query") or "").strip()
        top_k = max(1, min(int(arguments.get("top_k", 5)), 10))
        visible = _visible_tool_names(ctx)
        if not query:
            return json.dumps(
                {
                    "matched": [],
                    "unlocked": [],
                    "already_loaded": [],
                    "tip": "query不能为空，请描述需要的功能",
                },
                ensure_ascii=False,
            )

        if query.lower().startswith("select:"):
            payload = _select_tools(registry, query[7:], visible)
        else:
            matched = registry.search(query, top_k=top_k, excluded_names=visible)
            payload = {
                "matched": matched,
                "unlocked": [str(item["name"]) for item in matched],
                "already_loaded": [],
            }

        unlocked = [
            name
            for name in payload["unlocked"]
            if registry.has_tool(name) and name not in visible
        ]
        payload["unlocked"] = unlocked
        if unlocked:
            _unlock_on_current_turn(ctx, registry, unlocked)
            payload["next_action"] = (
                "unlocked中的工具Schema已加入当前Turn。下一步直接调用需要的工具，"
                "不要再次调用tool_search。"
            )
        elif "tip" not in payload:
            payload["tip"] = "没有新的工具被解锁，请更换功能关键词"
        return json.dumps(payload, ensure_ascii=False)

    registry.register(
        Tool(
            name="tool_search",
            description=(
                "搜索工具目录并把匹配工具解锁到当前Turn。需要某种能力但对应工具Schema"
                "不可见时调用；知道准确名称可使用select:工具名。解锁后直接调用目标工具。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "功能关键词，或select:工具名；可用逗号选择多个工具",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "关键词搜索返回数量，默认5，最大10",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 10,
                    },
                },
                "required": ["query"],
            },
            handler=handler,
        ),
        risk="read-only",
        always_on=True,
        search_hint="搜索工具 发现能力 加载工具",
        source_type="builtin",
        source_name="tool_catalog",
        tier="meta",
        preloadable=False,
    )


def _select_tools(
    registry: ToolRegistry,
    names_text: str,
    visible: set[str],
) -> dict[str, Any]:
    requested = list(
        dict.fromkeys(name.strip() for name in names_text.split(",") if name.strip())
    )
    already_loaded: list[str] = []
    missing: list[str] = []
    unlocked: list[str] = []
    matched: list[dict[str, Any]] = []
    for name in requested:
        if name in visible:
            already_loaded.append(name)
            continue
        meta = registry.get_metadata(name)
        if not registry.has_tool(name) or meta is None or meta.tier == "meta":
            missing.append(name)
            continue
        unlocked.append(name)
        matched.extend(registry.search(name, top_k=1, excluded_names=visible))
    result: dict[str, Any] = {
        "matched": matched,
        "unlocked": unlocked,
        "already_loaded": already_loaded,
    }
    tips: list[str] = []
    if already_loaded:
        tips.append("已加载可直接调用: " + ", ".join(already_loaded))
    if missing:
        tips.append("未找到或不可解锁: " + ", ".join(missing))
    if tips:
        result["tip"] = "; ".join(tips)
    return result


def _visible_tool_names(ctx: Any) -> set[str]:
    schemas = getattr(ctx, "tools", None)
    if not isinstance(schemas, list):
        return set()
    return {
        str(schema.get("function", {}).get("name", ""))
        for schema in schemas
        if isinstance(schema, dict) and schema.get("function", {}).get("name")
    }


def _unlock_on_current_turn(ctx: Any, registry: ToolRegistry, names: list[str]) -> None:
    if ctx is None or not isinstance(getattr(ctx, "tools", None), list):
        return
    visible = _visible_tool_names(ctx)
    for schema in registry.get_schemas(set(names)):
        name = str(schema.get("function", {}).get("name", ""))
        if name and name not in visible:
            ctx.tools.append(schema)
            visible.add(name)
