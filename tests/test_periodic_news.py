from __future__ import annotations

from agent.tools.message_push import MessagePushTool
from proactive_v2.agent_tick import AgentTick
from proactive_v2.contracts import ProactivePolicy
from proactive_v2.periodic_news import (
    CombinedProactiveGateway,
    ExaPeriodicNewsGateway,
    PeriodicNewsConfig,
    parse_topics,
)
from proactive_v2.state import ProactiveStateStore


class SearchClient:
    def __init__(self, payload=None, error: Exception | None = None) -> None:
        self.payload = payload or {"results": []}
        self.error = error
        self.calls = []

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.payload


def news_payload():
    return {
        "results": [
            {
                "title": "AI Agent 发布新框架",
                "url": "https://example.com/news?id=1&utm_source=test#top",
                "snippet": "一个面向大模型 Agent 的新框架正式发布。",
                "published_at": "2026-08-24T00:00:00Z",
                "source": "example.com",
            }
        ]
    }


async def test_exa_news_projects_search_results_with_stable_evidence() -> None:
    client = SearchClient(news_payload())
    gateway = ExaPeriodicNewsGateway(
        client,  # type: ignore[arg-type]
        PeriodicNewsConfig(("AI Agent", "大模型", "人工智能"), 3),
    )

    first = await gateway.run()
    second = await gateway.run()

    assert client.calls[0]["livecrawl"] == "preferred"
    assert client.calls[0]["num_results"] == 3
    assert first.content_meta[0]["id"] == second.content_meta[0]["id"]
    assert "utm_source" not in first.content_meta[0]["url"]
    assert "来源：https://example.com/news?id=1" in next(iter(first.content_store.values()))
    assert first.context[0]["kind"] == "preference"


async def test_news_source_failure_is_returned_as_gateway_failure() -> None:
    gateway = ExaPeriodicNewsGateway(  # type: ignore[arg-type]
        SearchClient(error=RuntimeError("exa unavailable"))
    )
    result = await gateway.run()
    assert result.content_meta == []
    assert result.source_failures == {"builtin:periodic_news": "exa unavailable"}


async def test_news_runs_through_interest_dedupe_delivery_and_ack() -> None:
    gateway = ExaPeriodicNewsGateway(SearchClient(news_payload()))  # type: ignore[arg-type]
    sent: list[tuple[str, str]] = []
    push = MessagePushTool()

    async def send(chat_id: str, message: str) -> None:
        sent.append((chat_id, message))

    push.register_channel("telegram", text=send)
    state = ProactiveStateStore(":memory:")
    tick = AgentTick(
        gateway=CombinedProactiveGateway([gateway]),
        push_tool=push,
        default_chat_id="123",
        user_id="123",
        session_key="telegram:123:123",
        mode="live",
        policy=ProactivePolicy(
            cooldown_seconds=0,
            daily_limit=10,
            quiet_start_hour=0,
            quiet_end_hour=0,
            normal_interval_seconds=36_000,
            schedule_jitter_ratio=0,
        ),
        state_store=state,
        ack_handlers=gateway.ack_handlers(),
    )

    first = await tick.tick()
    second = await tick.tick()

    assert first is not None and first.sent
    assert first.next_interval_seconds == 36_000
    assert "AI Agent 发布新框架" in sent[0][1]
    assert "https://example.com/news?id=1" in sent[0][1]
    assert state.ack_status(first.ack_outbox_ids[0]) == "acked"
    assert second is not None and not second.sent
    assert second.reason == "event_duplicate"
    assert len(sent) == 1


def test_parse_topics_accepts_chinese_and_ascii_separators() -> None:
    assert parse_topics("AI Agent，大模型、人工智能,AI Agent") == (
        "AI Agent",
        "大模型",
        "人工智能",
    )
