from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from agent.tools.message_push import MessagePushTool
from proactive_v2.agent_tick import AgentTick, ProactiveDecision
from proactive_v2.contracts import (
    AgentTickContext,
    FetchOutput,
    JudgeInput,
    JudgeProposal,
    JudgeToolCall,
    PhaseTrace,
    ProactivePolicy,
    ProactiveTarget,
)
from proactive_v2.gateway import DataGateway, GatewayResult
from proactive_v2.stages import JudgeStage
from proactive_v2.state import ProactiveStateStore


def policy(**overrides: object) -> ProactivePolicy:
    base = ProactivePolicy(
        cooldown_seconds=0,
        daily_limit=99,
        quiet_start_hour=0,
        quiet_end_hour=0,
    )
    return replace(base, **overrides)


def push_tool(events: list[object], *, fail: bool = False) -> MessagePushTool:
    tool = MessagePushTool()

    async def send(chat_id: str, message: str) -> None:
        events.append(("send", chat_id, message))
        if fail:
            raise RuntimeError("network down")

    tool.register_channel("telegram", text=send)
    return tool


@pytest.mark.asyncio
async def test_five_stages_send_then_persist_then_ack() -> None:
    events: list[object] = []
    state = ProactiveStateStore(":memory:")

    async def alerts() -> list[dict[str, str]]:
        return [{"ack_server": "alarm", "id": "e1", "title": "CPU", "body": "95%"}]

    async def ack(event_id: str, decision: str) -> None:
        assert state.delivery_count("telegram:42:42") == 1
        events.append(("ack", event_id, decision))

    tick = AgentTick(
        gateway=DataGateway(alert_fn=alerts),
        push_tool=push_tool(events),
        default_chat_id="42",
        user_id="42",
        session_key="telegram:42:42",
        mode="live",
        policy=policy(),
        state_store=state,
        ack_handlers={"alarm": ack},
    )
    result = await tick.tick()

    assert result is not None and result.sent
    assert [trace.phase for trace in result.traces] == [
        "gate", "fetch", "judge", "resolve", "deliver"
    ]
    assert events == [("send", "42", "CPU\n95%"), ("ack", "e1", "delivered")]
    assert state.ack_status(result.ack_outbox_ids[0]) == "acked"


@pytest.mark.asyncio
async def test_send_failure_never_persists_or_acks() -> None:
    events: list[object] = []
    state = ProactiveStateStore(":memory:")

    async def alerts() -> list[dict[str, str]]:
        return [{"ack_server": "alarm", "id": "e2", "title": "failure"}]

    async def ack(_event_id: str, _decision: str) -> None:
        events.append("ack")

    tick = AgentTick(
        gateway=DataGateway(alert_fn=alerts),
        push_tool=push_tool(events, fail=True),
        default_chat_id="42",
        session_key="s",
        mode="live",
        policy=policy(),
        state_store=state,
        ack_handlers={"alarm": ack},
    )
    result = await tick.tick()

    assert result is not None and not result.sent
    assert result.error.startswith("发送失败")
    assert state.delivery_count("s") == 0
    assert state.pending_acks() == []
    assert "ack" not in events


@pytest.mark.asyncio
async def test_context_alone_never_triggers_delivery() -> None:
    events: list[object] = []

    async def context() -> list[dict[str, str]]:
        return [{"kind": "preference", "text": "用户喜欢 AI 新闻"}]

    tick = AgentTick(
        gateway=DataGateway(context_fn=context),
        push_tool=push_tool(events),
        default_chat_id="42",
        mode="live",
        policy=policy(),
    )
    result = await tick.tick()

    assert result is not None
    assert result.decision == "skip"
    assert result.reason == "context_only"
    assert events == []


@pytest.mark.asyncio
async def test_busy_passive_session_blocks_before_fetch() -> None:
    fetched = False

    async def alerts() -> list[dict[str, str]]:
        nonlocal fetched
        fetched = True
        return [{"id": "e3", "title": "ordinary"}]

    tick = AgentTick(
        gateway=DataGateway(alert_fn=alerts),
        push_tool=push_tool([]),
        default_chat_id="42",
        session_key="busy-session",
        mode="live",
        policy=policy(),
        passive_busy_fn=lambda session_key: session_key == "busy-session",
    )
    result = await tick.tick()

    assert result is not None and result.reason == "gate:passive_busy"
    assert not fetched
    assert len(result.traces) == 5


@pytest.mark.asyncio
async def test_urgent_cooldown_bypass_is_explicit_policy() -> None:
    state = ProactiveStateStore(":memory:")
    events: list[object] = []

    async def decide(_snapshot: object) -> ProactiveDecision:
        return ProactiveDecision("reply", "urgent", 1.0, "urgent", ["alarm:new"])

    # Seed a recent delivery, which makes the session subject to cooldown.
    seed = AgentTickContext(
        target=ProactiveTarget("telegram", "42", "s"),
        policy=policy(),
        mode="live",
        started_at=datetime.now(timezone.utc),
    )
    state.record_delivery_and_enqueue_acks(
        context=seed, delivery_key="old", message="old", evidence=[]
    )

    blocked = AgentTick(
        gateway=DataGateway(),
        push_tool=push_tool(events),
        default_chat_id="42",
        session_key="s",
        priority="urgent",
        mode="live",
        policy=policy(cooldown_seconds=3600, urgent_bypass_cooldown=False),
        state_store=state,
        decision_fn=decide,
    )
    blocked_result = await blocked.tick()
    assert blocked_result is not None and blocked_result.reason == "gate:cooldown"

    allowed = AgentTick(
        gateway=DataGateway(),
        push_tool=push_tool(events),
        default_chat_id="42",
        session_key="s",
        priority="urgent",
        mode="live",
        policy=policy(cooldown_seconds=3600, urgent_bypass_cooldown=True),
        state_store=state,
        decision_fn=decide,
    )
    allowed_result = await allowed.tick()
    assert allowed_result is not None and allowed_result.sent


@pytest.mark.asyncio
async def test_shadow_records_trace_without_sending_or_delivery() -> None:
    state = ProactiveStateStore(":memory:")
    events: list[object] = []

    async def alerts() -> list[dict[str, str]]:
        return [{"id": "shadow-1", "title": "candidate"}]

    tick = AgentTick(
        gateway=DataGateway(alert_fn=alerts),
        push_tool=push_tool(events),
        default_chat_id="42",
        session_key="shadow",
        mode="shadow",
        policy=policy(),
        state_store=state,
    )
    result = await tick.tick()

    assert result is not None and result.decision == "reply" and not result.sent
    assert events == []
    assert state.delivery_count("shadow") == 0
    assert state.tick_trace(result.tick_id) is not None
    assert result.traces[-1].outcome == "shadow"


@pytest.mark.asyncio
async def test_event_and_semantic_dedupe_prevent_repeat_push() -> None:
    state = ProactiveStateStore(":memory:")
    events: list[object] = []
    current = {"id": "same", "title": "重要提醒"}

    async def alerts() -> list[dict[str, str]]:
        return [dict(current)]

    tick = AgentTick(
        gateway=DataGateway(alert_fn=alerts),
        push_tool=push_tool(events),
        default_chat_id="42",
        session_key="dedupe",
        mode="live",
        policy=policy(),
        state_store=state,
    )
    first = await tick.tick()
    second = await tick.tick()
    assert first is not None and first.sent
    assert second is not None and second.reason == "event_duplicate"

    current["id"] = "different"
    third = await tick.tick()
    assert third is not None and third.reason == "semantic_duplicate"
    assert len(events) == 1


@pytest.mark.asyncio
async def test_judge_function_calling_uses_strict_whitelist() -> None:
    calls = 0

    async def judge(value: JudgeInput) -> JudgeProposal:
        nonlocal calls
        calls += 1
        if not value.tool_results:
            return JudgeProposal(
                "reply",
                tool_calls=(
                    JudgeToolCall("allowed_lookup", {"query": "x"}),
                    JudgeToolCall("dangerous_tool", {}),
                ),
            )
        return JudgeProposal("reply", "verified", 0.9, "tool_evidence")

    stage = JudgeStage(
        judge,
        tools={"allowed_lookup": lambda query: {"query": query, "ok": True}},
    )
    now = datetime.now(timezone.utc).isoformat()
    fetched = FetchOutput(
        GatewayResult(),
        PhaseTrace("fetch", now, now, "fetched", "test"),
    )
    result = await stage.run(
        JudgeInput(
            AgentTickContext(
                ProactiveTarget("telegram", "42", "s"),
                policy=policy(max_judge_steps=1),
            ),
            fetched,
        )
    )

    assert calls == 2
    assert result.proposal.message == "verified"
    assert result.tool_results[0]["ok"] is True
    assert result.tool_results[1]["error"] == "tool_not_whitelisted"
