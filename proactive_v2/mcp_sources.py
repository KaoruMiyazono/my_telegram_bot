from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import tomllib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from agent.mcp.client import McpCallResult
from agent.mcp.manager import McpManager
from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry
from proactive_v2.gateway import GatewayResult

logger = logging.getLogger(__name__)

SourceChannel = Literal["alert", "content", "context"]
_VALID_CHANNELS: frozenset[SourceChannel] = frozenset(
    {"alert", "content", "context"}
)
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class McpProactiveSourceSpec:
    """One stable proactive data source backed by an MCP tool."""

    plugin_id: str
    source_id: str
    channels: tuple[SourceChannel, ...]
    mcp_server: str
    fetch_tool: str
    ack_tool: str = ""
    page_size: int = 0
    enabled: bool = False

    @property
    def key(self) -> str:
        return f"{self.plugin_id}:{self.source_id}"

    @property
    def needs_ack(self) -> bool:
        return bool(set(self.channels) & {"alert", "content"})

    def validate(self) -> None:
        if not _ID_RE.fullmatch(self.plugin_id):
            raise ValueError(f"Invalid proactive source plugin_id: {self.plugin_id!r}")
        if not _ID_RE.fullmatch(self.source_id):
            raise ValueError(f"Invalid proactive source_id: {self.source_id!r}")
        if not self.channels or len(set(self.channels)) != len(self.channels):
            raise ValueError(f"Source channels must be non-empty and unique: {self.key}")
        invalid = set(self.channels) - _VALID_CHANNELS
        if invalid:
            raise ValueError(f"Invalid proactive source channels {sorted(invalid)}: {self.key}")
        if not self.mcp_server.strip() or not self.fetch_tool.strip():
            raise ValueError(f"Source requires mcp_server and fetch_tool: {self.key}")
        if self.page_size < 0:
            raise ValueError(f"Source page_size cannot be negative: {self.key}")
        if self.channels == ("context",) and self.ack_tool:
            raise ValueError(f"Context-only source cannot declare ack_tool: {self.key}")


class ProactiveSourceRegistry:
    """Configured source catalog with independent runtime enable switches."""

    def __init__(self, specs: Mapping[str, McpProactiveSourceSpec]) -> None:
        self._specs: dict[str, McpProactiveSourceSpec] = {}
        self._enabled: set[str] = set()
        for spec in specs.values():
            spec.validate()
            if spec.key in self._specs:
                raise ValueError(f"Duplicate proactive source key: {spec.key}")
            self._specs[spec.key] = spec
            if spec.enabled:
                self._enabled.add(spec.key)

    @classmethod
    def from_config(cls, path: str | Path) -> "ProactiveSourceRegistry":
        return cls(load_proactive_source_specs(path))

    def enable(self, source_key: str) -> dict[str, Any]:
        spec = self.get(source_key)
        self._enabled.add(spec.key)
        return self.state(spec.key)

    def disable(self, source_key: str) -> dict[str, Any]:
        spec = self.get(source_key)
        self._enabled.discard(spec.key)
        return self.state(spec.key)

    def get(self, source_key: str) -> McpProactiveSourceSpec:
        try:
            return self._specs[source_key]
        except KeyError as exc:
            raise ValueError(f"Unknown proactive source: {source_key}") from exc

    def all(self) -> tuple[McpProactiveSourceSpec, ...]:
        return tuple(self._specs[key] for key in sorted(self._specs))

    def enabled(self) -> tuple[McpProactiveSourceSpec, ...]:
        return tuple(
            self._specs[key] for key in sorted(self._enabled) if key in self._specs
        )

    def state(self, source_key: str) -> dict[str, Any]:
        spec = self.get(source_key)
        payload = asdict(spec)
        payload["key"] = spec.key
        payload["runtime_enabled"] = spec.key in self._enabled
        return payload

    def list_states(self) -> list[dict[str, Any]]:
        return [self.state(spec.key) for spec in self.all()]

    def validate_servers(self, server_names: set[str]) -> None:
        missing = sorted(
            {spec.mcp_server for spec in self._specs.values()} - set(server_names)
        )
        if missing:
            raise ValueError(
                "Proactive sources reference unknown MCP servers: "
                + ", ".join(missing)
            )


class McpSourceCaller(Protocol):
    async def call(
        self,
        server: str,
        tool_name: str,
        arguments: dict[str, object],
    ) -> Any: ...


class McpManagerSourceCaller:
    """Decode structured MCP results from the shared runtime manager."""

    def __init__(self, manager: McpManager) -> None:
        self._manager = manager

    async def call(
        self,
        server: str,
        tool_name: str,
        arguments: dict[str, object],
    ) -> Any:
        result = await self._manager.call_tool(server, tool_name, arguments)
        return decode_mcp_result(result)


@dataclass(frozen=True)
class QuarantinedSourceItem:
    source_key: str
    item_id: str
    reason: str
    payload: object


@dataclass
class _SourceBatch:
    alerts: list[dict[str, Any]]
    content: list[dict[str, Any]]
    context: list[dict[str, Any]]
    quarantined: list[QuarantinedSourceItem]

    @classmethod
    def empty(cls) -> "_SourceBatch":
        return cls(alerts=[], content=[], context=[], quarantined=[])


class McpProactiveGateway:
    """Fetch every enabled MCP Source once and isolate failures by Source."""

    def __init__(
        self,
        caller: McpSourceCaller,
        registry: ProactiveSourceRegistry,
        *,
        max_pages: int = 256,
    ) -> None:
        self._caller = caller
        self._registry = registry
        self._max_pages = max(1, int(max_pages))

    async def run(self) -> GatewayResult:
        specs = self._registry.enabled()
        if not specs:
            return GatewayResult()
        results = await asyncio.gather(
            *(self._fetch_source(spec) for spec in specs),
            return_exceptions=True,
        )
        gateway = GatewayResult()
        for spec, result in zip(specs, results):
            if isinstance(result, BaseException):
                gateway.source_failures[spec.key] = str(result)
                logger.warning(
                    "[proactive.source] isolated source failure key=%s error=%s",
                    spec.key,
                    result,
                )
                continue
            gateway.alerts.extend(result.alerts)
            gateway.context.extend(result.context)
            for item in result.content:
                item_id = str(item["_compound_key"])
                body = str(item.pop("_content_body", ""))
                item.pop("_compound_key", None)
                gateway.content_meta.append(item)
                gateway.content_store[item_id] = body
            gateway.quarantined.extend(asdict(item) for item in result.quarantined)
        return gateway

    async def _fetch_source(self, spec: McpProactiveSourceSpec) -> _SourceBatch:
        payload = await self._fetch_payload(spec)
        raw_items: list[object]
        if spec.channels == ("context",) and isinstance(payload, dict):
            raw_items = [payload]
        elif isinstance(payload, list):
            raw_items = list(payload)
        else:
            raise ValueError(
                f"Source payload must be a list or context object: {spec.key}"
            )

        batch = _SourceBatch.empty()
        for index, raw in enumerate(raw_items):
            try:
                item = _validate_source_item(raw, spec)
            except ValueError as exc:
                batch.quarantined.append(
                    QuarantinedSourceItem(
                        source_key=spec.key,
                        item_id=_raw_item_id(raw, index),
                        reason=str(exc),
                        payload=raw,
                    )
                )
                continue
            kind = cast(SourceChannel, item["kind"])
            if kind == "alert":
                batch.alerts.append(item)
            elif kind == "context":
                batch.context.append(item)
            else:
                compound = f"{spec.key}:{item['event_id']}"
                body = str(item.get("body") or item.get("content") or "")
                metadata = dict(item)
                metadata["id"] = compound
                metadata["_compound_key"] = compound
                metadata["_content_body"] = body
                metadata.pop("body", None)
                metadata.pop("content", None)
                batch.content.append(metadata)
        return batch

    async def _fetch_payload(self, spec: McpProactiveSourceSpec) -> Any:
        if spec.page_size <= 0:
            return await self._caller.call(spec.mcp_server, spec.fetch_tool, {})
        items: list[object] = []
        offset = 0
        for _ in range(self._max_pages):
            page = await self._caller.call(
                spec.mcp_server,
                spec.fetch_tool,
                {"offset": offset, "limit": spec.page_size},
            )
            if not isinstance(page, list):
                raise ValueError(f"Paged source must return a list: {spec.key}")
            items.extend(page)
            if len(page) < spec.page_size:
                return items
            offset += len(page)
        raise RuntimeError(f"Source exceeded max pages: {spec.key}")

    def ack_handlers(self) -> dict[str, Callable[[str, str], Awaitable[None]]]:
        handlers: dict[str, Callable[[str, str], Awaitable[None]]] = {}
        for spec in self._registry.all():
            if not spec.needs_ack:
                continue

            async def handler(
                event_id: str,
                decision: str,
                *,
                source: McpProactiveSourceSpec = spec,
            ) -> None:
                await self._ack_one(source, event_id, decision)

            handlers[spec.key] = handler
        return handlers

    async def _ack_one(
        self,
        spec: McpProactiveSourceSpec,
        event_id: str,
        decision: str,
    ) -> None:
        if not spec.ack_tool:
            return
        payload = await self._caller.call(
            spec.mcp_server,
            spec.ack_tool,
            {"event_ids": [event_id], "feedback": decision},
        )
        if not isinstance(payload, dict):
            raise ValueError(f"ACK payload must be an object: {spec.key}")
        if payload.get("status") != "committed":
            raise ValueError(
                f"ACK status must be committed: {spec.key}: {payload.get('status')!r}"
            )
        committed = payload.get("ids")
        if committed != [event_id]:
            raise ValueError(
                f"ACK ids must exactly match requested event: {spec.key}: {committed!r}"
            )


def load_proactive_source_specs(
    path: str | Path,
) -> dict[str, McpProactiveSourceSpec]:
    config_path = Path(path).expanduser()
    if not config_path.exists():
        return {}
    payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    raw_sources = payload.get("sources", {})
    if not isinstance(raw_sources, dict):
        raise ValueError("proactive_sources.toml must contain a [sources] table")
    specs: dict[str, McpProactiveSourceSpec] = {}
    for source_id, raw in raw_sources.items():
        if not isinstance(raw, dict):
            raise ValueError(f"Proactive source config must be a table: {source_id}")
        raw_channels = raw.get("channels", [])
        if not isinstance(raw_channels, list):
            raise ValueError(f"Source channels must be a list: {source_id}")
        spec = McpProactiveSourceSpec(
            plugin_id=str(raw.get("plugin_id") or "builtin"),
            source_id=str(source_id),
            channels=tuple(cast(SourceChannel, str(item)) for item in raw_channels),
            mcp_server=str(raw.get("mcp_server") or ""),
            fetch_tool=str(raw.get("fetch_tool") or ""),
            ack_tool=str(raw.get("ack_tool") or ""),
            page_size=int(raw.get("page_size") or 0),
            enabled=bool(raw.get("enabled", False)),
        )
        spec.validate()
        specs[spec.key] = spec
    return specs


def register_proactive_source_management_tools(
    tools: ToolRegistry,
    sources: ProactiveSourceRegistry,
) -> None:
    source_keys = [spec.key for spec in sources.all()]
    source_schema: dict[str, Any] = {
        "type": "string",
        "description": "proactive_sources.toml 中预先声明的数据源稳定 key",
    }
    if source_keys:
        source_schema["enum"] = source_keys

    async def enable_handler(arguments: dict[str, Any], ctx: Any = None) -> str:
        return json.dumps(
            sources.enable(str(arguments.get("source_key") or "")),
            ensure_ascii=False,
        )

    async def disable_handler(arguments: dict[str, Any], ctx: Any = None) -> str:
        return json.dumps(
            sources.disable(str(arguments.get("source_key") or "")),
            ensure_ascii=False,
        )

    async def list_handler(arguments: dict[str, Any], ctx: Any = None) -> str:
        return json.dumps({"sources": sources.list_states()}, ensure_ascii=False)

    for name, description, handler, risk in (
        (
            "proactive_source_enable",
            "运行时启用一个预配置的主动 MCP Source。",
            enable_handler,
            "read-write",
        ),
        (
            "proactive_source_disable",
            "运行时禁用一个主动 MCP Source，不卸载其 MCP Server。",
            disable_handler,
            "read-write",
        ),
    ):
        tools.register(
            Tool(
                name=name,
                description=description,
                parameters={
                    "type": "object",
                    "properties": {"source_key": source_schema},
                    "required": ["source_key"],
                },
                handler=handler,
                idempotent=False,
            ),
            risk=risk,
            always_on=True,
            source_type="builtin",
            source_name="proactive_source_runtime",
            tier="meta",
            preloadable=False,
        )
    tools.register(
        Tool(
            name="proactive_source_list",
            description="查看主动 MCP Source 声明及当前启用状态。",
            parameters={"type": "object", "properties": {}},
            handler=list_handler,
        ),
        risk="read-only",
        always_on=True,
        source_type="builtin",
        source_name="proactive_source_runtime",
        tier="meta",
        preloadable=False,
    )


def decode_mcp_result(result: McpCallResult) -> Any:
    if result.is_error:
        raise RuntimeError(f"MCP tool returned an error: {result.content!r}")
    structured: list[Any] = []
    texts: list[str] = []
    for block in result.content:
        if block.get("type") == "structured":
            structured.append(block.get("data"))
        elif block.get("type") == "text" and block.get("text") is not None:
            texts.append(str(block["text"]))
    if structured:
        return _unwrap_mcp_value(structured[-1])
    text = "\n".join(texts).strip()
    if not text:
        return None
    try:
        return _unwrap_mcp_value(json.loads(text))
    except (TypeError, ValueError):
        return text


def _unwrap_mcp_value(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"result"}:
        return value["result"]
    return value


def _validate_source_item(
    raw: object,
    spec: McpProactiveSourceSpec,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"Source item must be an object, got {type(raw).__name__}")
    item = dict(raw)
    kind = str(item.get("kind") or "").strip()
    if not kind and len(spec.channels) == 1:
        kind = spec.channels[0]
    if kind not in spec.channels:
        raise ValueError(f"Item kind is missing or undeclared: {kind!r}")
    item["kind"] = kind
    if kind == "context":
        item["_source"] = spec.key
        item.pop("ack_server", None)
        return item

    event_id = str(item.get("event_id") or item.get("id") or "").strip()
    if not event_id:
        raise ValueError("Alert/content item requires provider event_id")
    item["event_id"] = event_id
    item["ack_server"] = spec.key
    for score_name in ("preprocess_score", "rank_score", "relevance_score"):
        if score_name not in item or item[score_name] is None:
            continue
        try:
            score = float(item[score_name])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{score_name} must be numeric") from exc
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise ValueError(f"{score_name} must be finite and within [0,1]")
        item[score_name] = score
    if "relevance_score" not in item:
        item["relevance_score"] = float(
            item.get("preprocess_score") or item.get("rank_score") or 0.0
        )
    for field_name in ("published_at", "triggered_at", "first_seen_at"):
        value = item.get(field_name)
        if value in (None, ""):
            continue
        try:
            parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be an ISO timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{field_name} must include timezone")
    return item


def _raw_item_id(raw: object, index: int) -> str:
    if isinstance(raw, dict):
        value = raw.get("event_id") or raw.get("id")
        if value is not None and str(value).strip():
            return str(value).strip()
    return f"index:{index}"
