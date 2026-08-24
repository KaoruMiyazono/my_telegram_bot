"""Periodic Exa news source for the existing proactive five-stage pipeline."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from agent.tools.web_search import WebSearchClient
from proactive_v2.gateway import GatewayResult, ProactiveGateway


AckHandler = Callable[[str, str], Awaitable[None]]


@dataclass(frozen=True)
class PeriodicNewsConfig:
    topics: tuple[str, ...] = ("AI Agent", "大模型", "人工智能")
    max_results: int = 5


class ExaPeriodicNewsGateway:
    """Search current news and project it into proactive content candidates."""

    source_key = "builtin:periodic_news"

    def __init__(
        self,
        client: WebSearchClient,
        config: PeriodicNewsConfig | None = None,
    ) -> None:
        self._client = client
        self._config = config or PeriodicNewsConfig()

    async def run(self) -> GatewayResult:
        topics = tuple(topic.strip() for topic in self._config.topics if topic.strip())
        if not topics:
            return GatewayResult(source_failures={self.source_key: "news topics are empty"})
        query = " OR ".join(topics) + " 最新新闻 最新进展"
        try:
            payload = await self._client.search(
                query=query,
                num_results=max(1, self._config.max_results),
                livecrawl="preferred",
                search_type="auto",
            )
        except Exception as exc:
            return GatewayResult(source_failures={self.source_key: str(exc)})

        result = GatewayResult(
            context=[
                {
                    "kind": "preference",
                    "text": "用户希望收到" + "、".join(topics) + "新闻",
                    "_source": self.source_key,
                }
            ]
        )
        for item in payload.get("results", []):
            if not isinstance(item, dict):
                continue
            url = _canonical_url(str(item.get("url") or ""))
            if not url:
                continue
            event_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
            compound = f"{self.source_key}:{event_id}"
            title = str(item.get("title") or url).strip()
            snippet = str(item.get("snippet") or "").strip()
            published = str(item.get("published_at") or "").strip()
            body_parts = [snippet]
            if published:
                body_parts.append(f"发布时间：{published}")
            body_parts.append(f"来源：{url}")
            result.content_meta.append(
                {
                    "id": compound,
                    "event_id": event_id,
                    "title": title,
                    "source": str(item.get("source") or "exa"),
                    "url": url,
                    "relevance_score": 0.95,
                    "interesting": True,
                }
            )
            result.content_store[compound] = "\n".join(
                part for part in body_parts if part
            )
        return result

    def ack_handlers(self) -> dict[str, AckHandler]:
        async def acknowledge(_event_id: str, _decision: str) -> None:
            # Exa search results have no provider ACK API. The local ACK outbox
            # still records completion so it cannot retry forever.
            return None

        return {self.source_key: acknowledge}


class CombinedProactiveGateway:
    """Merge independent gateways while isolating one gateway's failure."""

    def __init__(self, gateways: Sequence[ProactiveGateway]) -> None:
        self._gateways = tuple(gateways)

    async def run(self) -> GatewayResult:
        snapshots = await asyncio.gather(
            *(gateway.run() for gateway in self._gateways),
            return_exceptions=True,
        )
        merged = GatewayResult()
        seen: set[str] = set()
        for index, snapshot in enumerate(snapshots):
            if isinstance(snapshot, BaseException):
                merged.source_failures[f"gateway:{index}"] = str(snapshot)
                continue
            merged.alerts.extend(snapshot.alerts)
            merged.context.extend(snapshot.context)
            merged.source_failures.update(snapshot.source_failures)
            merged.quarantined.extend(snapshot.quarantined)
            merged.source_duplicates.extend(snapshot.source_duplicates)
            for item in snapshot.content_meta:
                item_id = str(item.get("id") or "")
                if not item_id or item_id in seen:
                    if item_id:
                        merged.source_duplicates.append(item_id)
                    continue
                seen.add(item_id)
                merged.content_meta.append(item)
                merged.content_store[item_id] = snapshot.content_store.get(item_id, "")
        return merged


def parse_topics(value: str) -> tuple[str, ...]:
    """Parse comma-separated environment configuration into stable topics."""

    normalized = value.replace("，", ",").replace("、", ",")
    return tuple(dict.fromkeys(part.strip() for part in normalized.split(",") if part.strip()))


def _canonical_url(value: str) -> str:
    if not value.startswith(("http://", "https://")):
        return ""
    parts = urlsplit(value)
    query = urlencode(
        [
            (key, item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
        ]
    )
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, query, ""))
