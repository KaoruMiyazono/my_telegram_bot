from __future__ import annotations

import json
from typing import Any

from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry
from memory.engine import (
    ExplicitRetrievalRequest,
    FetchMessagesRequest,
    ForgetRequest,
    MemoryScope,
    RememberRequest,
    SearchMessagesRequest,
)


def register_memory_tools(registry: ToolRegistry, memory_engine: Any) -> None:
    """Register memory tools against the shared MemoryEngine runtime."""
    from agent.pipeline.phases.before_reasoning import _TOOLS

    handlers = {
        "memorize": lambda args, ctx: _memorize(memory_engine, args, ctx),
        "recall_memory": lambda args, ctx: _recall_memory(memory_engine, args, ctx),
        "fetch_messages": lambda args, ctx: _fetch_messages(memory_engine, args),
        "search_messages": lambda args, ctx: _search_messages(memory_engine, args, ctx),
    }
    for schema in _TOOLS:
        fn = schema.get("function", {})
        name = str(fn.get("name", ""))
        handler = handlers.get(name)
        if not name or handler is None:
            continue
        registry.register(
            Tool(
                name=name,
                description=str(fn.get("description", "")),
                parameters=dict(fn.get("parameters") or {}),
                handler=handler,
            ),
            risk="read-write" if name == "memorize" else "read-only",
            always_on=True,
            source_type="builtin",
            source_name="memory",
        )


async def _memorize(memory_engine: Any, args: dict[str, Any], ctx: Any) -> str:
    scope = _scope_from_ctx(ctx)
    result = await memory_engine.remember(
        RememberRequest(
            summary=str(args.get("summary", "")).strip(),
            memory_type=str(args.get("memory_type", "")).strip(),
            scope=scope,
            source_ref=f"session:{scope.user_id}:{scope.chat_id}",
        )
    )
    payload = {
        "status": result.status,
        "item_id": result.item_id,
        "summary": result.summary,
        "memory_type": result.memory_type,
    }
    if result.error:
        payload["error"] = result.error
    return json.dumps(payload, ensure_ascii=False)


async def _recall_memory(memory_engine: Any, args: dict[str, Any], ctx: Any) -> str:
    scope = _scope_from_ctx(ctx, user_id=args.get("user_id"))
    result = await memory_engine.retrieve_explicit(
        ExplicitRetrievalRequest(
            query=str(args.get("query", "")).strip(),
            memory_type=str(args.get("memory_type", "")).strip(),
            include_superseded=bool(args.get("include_superseded", False)),
            search_mode=str(args.get("search_mode", "semantic")).strip() or "semantic",
            time_filter=str(args.get("time_filter", "")).strip(),
            limit=max(1, min(int(args.get("limit", 5)), 50)),
            scope=scope,
        )
    )
    payload = {
        "count": len(result.items),
        "applied_memory_types": result.applied_memory_types,
        "status_policy": _status_policy(result.items),
        "items": result.items,
    }
    if result.trace:
        payload["trace"] = result.trace
    if result.error:
        payload["error"] = result.error
    return json.dumps(payload, ensure_ascii=False)


async def _fetch_messages(memory_engine: Any, args: dict[str, Any]) -> str:
    source_refs = _coerce_source_refs(args)
    result = await memory_engine.fetch_messages(
        FetchMessagesRequest(
            source_refs=source_refs,
            context=max(0, min(int(args.get("context", 0)), 10)),
            limit=max(1, min(int(args.get("limit", 20)), 50)),
        )
    )
    payload: dict[str, Any] = {
        "matched_count": result.matched_count,
        "source_refs": result.source_refs,
        "messages": result.messages,
    }
    if result.invalid_source_refs:
        payload["invalid_source_refs"] = result.invalid_source_refs
    if len(result.source_refs) == 1:
        payload["source_ref"] = result.source_refs[0]
    if result.error:
        payload["error"] = result.error
    return json.dumps(payload, ensure_ascii=False)


async def _search_messages(memory_engine: Any, args: dict[str, Any], ctx: Any) -> str:
    scope = _scope_from_ctx(ctx, user_id=args.get("user_id"))
    result = await memory_engine.search_messages(
        SearchMessagesRequest(
            query=str(args.get("query", "")).strip(),
            role=str(args.get("role", "")).strip() or None,
            limit=max(1, min(int(args.get("limit", 10)), 50)),
            offset=max(0, int(args.get("offset", 0))),
            scope=scope,
        )
    )
    payload = {
        "count": len(result.messages),
        "matched_count": result.matched_count,
        "limit": result.limit,
        "offset": result.offset,
        "has_more": result.has_more,
        "next_offset": result.next_offset,
        "messages": result.messages,
    }
    if result.error:
        payload["error"] = result.error
    return json.dumps(payload, ensure_ascii=False)


async def _forget_memory(memory_engine: Any, args: dict[str, Any], ctx: Any) -> str:
    ids = [str(item).strip() for item in args.get("ids", []) if str(item).strip()]
    result = await memory_engine.forget(ForgetRequest(ids=ids, scope=_scope_from_ctx(ctx)))
    payload = {
        "superseded_ids": result.superseded_ids,
        "missing_ids": result.missing_ids,
    }
    if result.error:
        payload["error"] = result.error
    return json.dumps(payload, ensure_ascii=False)


def _scope_from_ctx(ctx: Any, *, user_id: Any = None) -> MemoryScope:
    session = getattr(ctx, "session", None)
    session_user_id = getattr(session, "user_id", None)
    raw_user_id = session_user_id
    if raw_user_id is None and str(user_id or "").strip() not in {"", "0"}:
        raw_user_id = user_id
    chat_id = getattr(session, "chat_id", None)
    return MemoryScope(
        user_id=int(raw_user_id) if raw_user_id is not None else None,
        chat_id=int(chat_id) if chat_id is not None else None,
        session_key=str(getattr(ctx, "session_key", "") or ""),
        channel=str(getattr(ctx, "channel", "") or "telegram"),
    )


def _coerce_source_refs(args: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        ref = str(value or "").strip()
        if ref and ref not in seen:
            seen.add(ref)
            refs.append(ref)

    _add(args.get("source_ref"))
    raw_refs = args.get("source_refs")
    if isinstance(raw_refs, list):
        for ref in raw_refs:
            _add(ref)
    elif raw_refs:
        _add(raw_refs)
    return refs


def _status_policy(items: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"active": 0, "superseded": 0, "other": 0}
    for item in items:
        status = str(item.get("status") or "").strip()
        if status == "active":
            counts["active"] += 1
        elif status == "superseded":
            counts["superseded"] += 1
        else:
            counts["other"] += 1
    return {
        "active_count": counts["active"],
        "superseded_count": counts["superseded"],
        "other_count": counts["other"],
        "rule": (
            "status=active 表示当前有效记忆；status=superseded 表示已被更新替代的历史记忆。"
            "当前状态、推荐、偏好、身份类问题遇到冲突时优先 active；"
            "superseded 只用于回答以前、变化过程或旧值。"
        ),
    }
