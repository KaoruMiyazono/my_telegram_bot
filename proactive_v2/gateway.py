from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class GatewayResult:
    alerts: list[dict[str, Any]] = field(default_factory=list)
    context: list[dict[str, Any]] = field(default_factory=list)
    content_meta: list[dict[str, Any]] = field(default_factory=list)
    content_store: dict[str, str] = field(default_factory=dict)


class DataGateway:
    """Prefetch proactive inputs before an agent tick decides whether to push."""

    def __init__(
        self,
        *,
        alert_fn: Any = None,
        feed_fn: Any = None,
        context_fn: Any = None,
        web_fetch_tool: Any = None,
        max_chars: int = 8_000,
        content_limit: int = 5,
    ) -> None:
        self._alert_fn = alert_fn
        self._feed_fn = feed_fn
        self._context_fn = context_fn
        self._web_fetch_tool = web_fetch_tool
        self._max_chars = max_chars
        self._content_limit = content_limit

    async def run(self) -> GatewayResult:
        alerts_task = asyncio.create_task(self._fetch_alerts())
        context_task = asyncio.create_task(self._fetch_context())
        content_task = asyncio.create_task(self._fetch_content())
        alerts, context, (content_meta, content_store) = await asyncio.gather(
            alerts_task,
            context_task,
            content_task,
        )
        return GatewayResult(
            alerts=alerts,
            context=context,
            content_meta=content_meta,
            content_store=content_store,
        )

    async def _fetch_alerts(self) -> list[dict[str, Any]]:
        try:
            return await self._alert_fn() if self._alert_fn else []
        except Exception as exc:
            logger.warning("[gateway] alerts fetch failed: %s", exc)
            return []

    async def _fetch_context(self) -> list[dict[str, Any]]:
        try:
            return await self._context_fn() if self._context_fn else []
        except Exception as exc:
            logger.warning("[gateway] context fetch failed: %s", exc)
            return []

    async def _fetch_content(self) -> tuple[list[dict[str, Any]], dict[str, str]]:
        try:
            events = await self._feed_fn(limit=self._content_limit) if self._feed_fn else []
        except Exception as exc:
            logger.warning("[gateway] feed fetch failed: %s", exc)
            return [], {}
        if not events:
            return [], {}

        fetch_tasks = [
            asyncio.create_task(self._fetch_one_url(str(event.get("url") or "")))
            for event in events
        ]
        fetch_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

        content_meta: list[dict[str, Any]] = []
        content_store: dict[str, str] = {}
        for event, result in zip(events, fetch_results):
            item_id = event.get("event_id") or event.get("id") or ""
            ack_server = event.get("ack_server") or "feed"
            compound_key = f"{ack_server}:{item_id}"
            content_meta.append(
                {
                    "id": compound_key,
                    "title": event.get("title") or "",
                    "source": event.get("source_name") or event.get("source") or "",
                    "url": event.get("url") or "",
                    "published_at": event.get("published_at") or "",
                }
            )
            content_store[compound_key] = "" if isinstance(result, Exception) else str(result or "")
        return content_meta, content_store

    async def _fetch_one_url(self, url: str) -> str:
        if not url or self._web_fetch_tool is None:
            return ""
        try:
            result = await self._web_fetch_tool.execute(url=url, format="text")
            return str(result or "")[: self._max_chars]
        except Exception as exc:
            logger.debug("[gateway] web fetch failed url=%s: %s", url, exc)
            return ""
