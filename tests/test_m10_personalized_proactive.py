from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from agent.tools.message_push import MessagePushTool
from memory.markdown_store import MarkdownMemoryStore
from memory.store import MemoryStore
from persistence.database import init_db
from proactive_v2.agent_tick import AgentTick
from proactive_v2.contracts import (
    AgentTickContext,
    ProactivePolicy,
    ProactiveTarget,
    ProactiveTickResult,
    UserInterestContext,
)
from proactive_v2.gateway import GatewayResult
from proactive_v2.interests import (
    MemoryInterestReader,
    PersonalizedScore,
)
from proactive_v2.scheduler import AdaptiveScheduler
from proactive_v2.stages import AckOutboxDispatcher
from proactive_v2.state import ProactiveStateStore
from proactive_v2.mcp_sources import (
    McpProactiveGateway,
    McpProactiveSourceSpec,
    ProactiveSourceRegistry,
)


class FakeEmbedder:
    async def embed(self, _text: str) -> list[float]:
        return [0.1] * 1024


class StaticGateway:
    def __init__(self, snapshot: GatewayResult) -> None:
        self.snapshot = snapshot

    async def run(self) -> GatewayResult:
        return self.snapshot


class FakeAmbiguousJudge:
    async def score(self, **_kwargs: Any) -> PersonalizedScore:
        return PersonalizedScore(
            score=0.87,
            reason="llm_matches_profile",
            matched_topics=("前沿技术",),
        )


def policy(**overrides: object) -> ProactivePolicy:
    return replace(
        ProactivePolicy(
            cooldown_seconds=0,
            daily_limit=99,
            quiet_start_hour=0,
            quiet_end_hour=0,
            schedule_jitter_ratio=0,
        ),
        **overrides,
    )


def push(events: list[tuple[str, str]]) -> MessagePushTool:
    tool = MessagePushTool()

    async def send(chat_id: str, message: str) -> None:
        events.append((chat_id, message))

    tool.register_channel("telegram", text=send)
    return tool


def news_snapshot(
    *,
    event_id: str = "n1",
    title: str = "AI Agent 框架发布",
    body: str = "新的 AI Agent 框架支持 Python。",
    provider_score: float = 0.99,
    interesting: bool = True,
) -> GatewayResult:
    compound = f"news:feed:{event_id}"
    return GatewayResult(
        content_meta=[
            {
                "id": compound,
                "event_id": event_id,
                "title": title,
                "relevance_score": provider_score,
                "interesting": interesting,
            }
        ],
        content_store={compound: body},
    )


@pytest.mark.asyncio
async def test_same_news_uses_each_users_own_long_term_interest(tmp_path: Path) -> None:
    init_db()
    store = MemoryStore(FakeEmbedder())  # type: ignore[arg-type]
    await store.upsert_item("preference", "用户喜欢 AI Agent 和 Python 新闻", 101)
    await store.upsert_item("preference", "用户不喜欢 AI 新闻", 202)
    markdown = MarkdownMemoryStore(tmp_path / "markdown")
    reader = MemoryInterestReader(store, markdown)

    first_events: list[tuple[str, str]] = []
    second_events: list[tuple[str, str]] = []
    first = AgentTick(
        gateway=StaticGateway(news_snapshot()),
        push_tool=push(first_events),
        default_chat_id="101",
        user_id="101",
        session_key="telegram:101:101",
        mode="live",
        policy=policy(),
        state_store=ProactiveStateStore(":memory:"),
        interest_reader=reader.read,
    )
    second = AgentTick(
        gateway=StaticGateway(news_snapshot()),
        push_tool=push(second_events),
        default_chat_id="202",
        user_id="202",
        session_key="telegram:202:202",
        mode="live",
        policy=policy(),
        state_store=ProactiveStateStore(":memory:"),
        interest_reader=reader.read,
    )

    liked = await first.tick()
    disliked = await second.tick()

    assert liked is not None and liked.sent and liked.reason == "personalized_content"
    assert liked.reasoning_evidence[0].startswith("memory:preference:")
    assert disliked is not None and not disliked.sent
    assert disliked.traces[2].details["content_scores"][0]["reason"].startswith(
        "negative_interest:"
    )
    assert len(first_events) == 1
    assert second_events == []


@pytest.mark.asyncio
async def test_provider_score_cannot_override_negative_preference() -> None:
    events: list[tuple[str, str]] = []
    interests = UserInterestContext(
        user_id="1",
        negative_topics=("娱乐八卦",),
        memory_evidence=("memory:preference:no-gossip",),
        source_count=1,
    )
    tick = AgentTick(
        gateway=StaticGateway(
            news_snapshot(title="娱乐八卦热搜", body="明星综艺消息", provider_score=1.0)
        ),
        push_tool=push(events),
        default_chat_id="1",
        user_id="1",
        mode="live",
        policy=policy(),
        interest_reader=lambda _user_id: interests,
    )

    result = await tick.tick()

    assert result is not None and result.decision == "skip"
    assert result.traces[2].details["content_scores"][0]["user_score"] == 0.0
    assert events == []


@pytest.mark.asyncio
async def test_cold_start_requires_explicit_high_confidence_candidate() -> None:
    events: list[tuple[str, str]] = []
    tick = AgentTick(
        gateway=StaticGateway(news_snapshot(provider_score=0.99, interesting=False)),
        push_tool=push(events),
        default_chat_id="1",
        user_id="1",
        mode="live",
        policy=policy(cold_start_threshold=0.9),
        interest_reader=lambda user_id: UserInterestContext(user_id=user_id),
    )

    result = await tick.tick()

    assert result is not None and result.reason == "no_candidate"
    assert result.traces[2].details["interest_cold_start"] is True
    assert events == []


@pytest.mark.asyncio
async def test_ambiguous_content_can_use_bounded_llm_fallback() -> None:
    interests = UserInterestContext(
        user_id="1",
        positive_topics=("前沿技术",),
        memory_evidence=("memory:preference:frontier",),
        source_count=1,
    )
    events: list[tuple[str, str]] = []
    tick = AgentTick(
        gateway=StaticGateway(
            news_snapshot(title="新型纠错算法", body="研究团队公布实验结果")
        ),
        push_tool=push(events),
        default_chat_id="1",
        user_id="1",
        mode="live",
        policy=policy(),
        interest_reader=lambda _user_id: interests,
        ambiguous_interest_judge=FakeAmbiguousJudge(),
    )

    result = await tick.tick()

    assert result is not None and result.sent
    score_trace = result.traces[2].details["content_scores"][0]
    assert score_trace == {
        "item_id": "news:feed:n1",
        "provider_score": 0.99,
        "user_score": 0.87,
        "reason": "llm_matches_profile",
    }


def test_adaptive_scheduler_backs_off_and_recovers_on_alert() -> None:
    scheduler = AdaptiveScheduler(
        policy(
            empty_interval_seconds=10,
            empty_backoff_multiplier=2,
            empty_backoff_max_seconds=40,
            alert_interval_seconds=3,
        )
    )
    now = datetime.now(timezone.utc)

    def result(snapshot: GatewayResult) -> ProactiveTickResult:
        return ProactiveTickResult(
            tick_id=f"tick-{scheduler.empty_streak}",
            decision="skip",
            sent=False,
            score=0,
            reason="no_candidate",
            gateway=snapshot,
        )

    assert scheduler.observe(result(GatewayResult()), now=now).interval_seconds == 10
    assert scheduler.observe(result(GatewayResult()), now=now).interval_seconds == 20
    assert scheduler.observe(result(GatewayResult()), now=now).interval_seconds == 40
    alert = GatewayResult(alerts=[{"event_id": "a1"}])
    recovered = scheduler.observe(result(alert), now=now)
    assert recovered.interval_seconds == 3
    assert recovered.reason == "alert_freshness"
    assert scheduler.empty_streak == 0


def test_adaptive_scheduler_uses_error_backoff_for_failed_sources() -> None:
    scheduler = AdaptiveScheduler(
        policy(
            error_backoff_base_seconds=7,
            error_backoff_max_seconds=30,
        )
    )
    failed = GatewayResult(source_failures={"news:broken": "timeout"})
    now = datetime.now(timezone.utc)
    first = ProactiveTickResult("e1", "skip", False, 0, "no_candidate", gateway=failed)
    second = ProactiveTickResult("e2", "skip", False, 0, "no_candidate", gateway=failed)

    assert scheduler.observe(first, now=now).interval_seconds == 7
    assert scheduler.observe(second, now=now).interval_seconds == 14
    assert scheduler.error_streak == 2


@pytest.mark.asyncio
async def test_source_layer_dedupes_duplicate_provider_event_in_one_snapshot() -> None:
    class Caller:
        async def call(
            self, _server: str, _tool: str, _arguments: dict[str, object]
        ) -> object:
            return [
                {"event_id": "same", "title": "first"},
                {"event_id": "same", "title": "duplicate"},
            ]

    spec = McpProactiveSourceSpec(
        plugin_id="test",
        source_id="alerts",
        channels=("alert",),
        mcp_server="demo",
        fetch_tool="fetch_alerts",
        enabled=True,
    )
    gateway = McpProactiveGateway(
        Caller(),
        ProactiveSourceRegistry({spec.key: spec}),
    )

    result = await gateway.run()

    assert [row["title"] for row in result.alerts] == ["first"]
    assert result.source_duplicates == ["test:alerts:same"]


@pytest.mark.asyncio
async def test_same_event_returned_one_hundred_times_is_sent_once() -> None:
    snapshot = GatewayResult(
        alerts=[
            {
                "event_id": "stable-event",
                "ack_server": "test:alert",
                "title": "只应发送一次",
            }
        ]
    )
    events: list[tuple[str, str]] = []
    tick = AgentTick(
        gateway=StaticGateway(snapshot),
        push_tool=push(events),
        default_chat_id="1",
        session_key="s",
        mode="live",
        policy=policy(),
        state_store=ProactiveStateStore(":memory:"),
    )

    results = [await tick.tick() for _ in range(100)]

    assert results[0] is not None and results[0].sent
    assert all(result is not None and not result.sent for result in results[1:])
    assert {result.reason for result in results[1:] if result is not None} == {
        "event_duplicate"
    }
    assert len(events) == 1


def test_content_hash_layer_precedes_semantic_layer() -> None:
    state = ProactiveStateStore(":memory:")
    context = AgentTickContext(
        ProactiveTarget("telegram", "1", "s"),
        policy=policy(),
        mode="live",
    )
    state.record_delivery_and_enqueue_acks(
        context=context,
        delivery_key="first",
        message="完全相同的新闻正文",
        evidence=["news:feed:first"],
    )
    assert state.content_hash_seen(
        "s",
        "完全相同的新闻正文",
        now=context.started_at,
        window_hours=24,
    )


@pytest.mark.asyncio
async def test_ack_outbox_recovers_after_restart_without_resending(tmp_path: Path) -> None:
    db_path = tmp_path / "proactive.db"
    state = ProactiveStateStore(db_path)
    context = AgentTickContext(
        ProactiveTarget("telegram", "1", "s"),
        policy=policy(),
        mode="live",
    )
    _delivery_id, ack_ids = state.record_delivery_and_enqueue_acks(
        context=context,
        delivery_key="sent-once",
        message="已经发送给用户",
        evidence=["news:feed:e1"],
    )

    async def fail(_event_id: str, _decision: str) -> None:
        raise RuntimeError("provider unavailable")

    failing = AckOutboxDispatcher(
        state,
        {"news:feed": fail},
        retry_base_seconds=1,
    )
    await failing.drain(ack_ids)
    assert state.ack_status(ack_ids[0]) == "failed"
    state.close()

    recovered_state = ProactiveStateStore(db_path)
    calls: list[tuple[str, str]] = []

    async def succeed(event_id: str, decision: str) -> None:
        calls.append((event_id, decision))

    recovered = AckOutboxDispatcher(recovered_state, {"news:feed": succeed})
    await recovered.drain(now=datetime.now(timezone.utc) + timedelta(seconds=2))

    assert calls == [("e1", "delivered")]
    assert recovered_state.ack_status(ack_ids[0]) == "acked"
    assert recovered_state.delivery_count("s") == 1


def test_ack_outbox_moves_permanent_failure_to_dead_letter() -> None:
    state = ProactiveStateStore(":memory:")
    context = AgentTickContext(
        ProactiveTarget("telegram", "1", "s"),
        policy=policy(),
        mode="live",
    )
    _delivery_id, ack_ids = state.record_delivery_and_enqueue_acks(
        context=context,
        delivery_key="dead-letter",
        message="sent",
        evidence=["news:feed:dead"],
    )
    for attempt in range(3):
        state.settle_ack(
            ack_ids[0],
            success=False,
            error="still failing",
            max_attempts=3,
            now=context.started_at + timedelta(seconds=attempt),
        )

    record = state.ack_record(ack_ids[0])
    assert record is not None
    assert record["status"] == "dead"
    assert record["attempts"] == 3
    assert record["next_retry_at"] is None
