from __future__ import annotations

from uuid import uuid4

from agent.core.ids import build_session_key, identity_for_message
from agent.core.types import (
    InboundMessage,
    OutboundMessage,
    ReasonerResult,
    Session,
    TurnCommittedEvent,
)
from agent.tools.runtime import ToolRuntimeResult


def test_canonical_session_key_is_channel_chat_user() -> None:
    assert build_session_key(channel="Telegram", chat_id=1001, user_id=42) == (
        "telegram:1001:42"
    )


def test_turn_identity_generates_and_preserves_ids() -> None:
    generated = identity_for_message(user_id=42, chat_id=1001)
    assert generated.turn_id.startswith("turn:")
    assert generated.trace_id.startswith("trace:")
    assert generated.session_key == "telegram:1001:42"

    replayed = identity_for_message(
        user_id=42,
        chat_id=1001,
        metadata={"channel": "cli", "turn_id": "turn:golden", "trace_id": "trace:golden"},
    )
    assert replayed.turn_id == "turn:golden"
    assert replayed.trace_id == "trace:golden"
    assert replayed.session_key == "cli:1001:42"


def test_core_models_share_the_same_turn_contract() -> None:
    inbound = InboundMessage(
        user_id=42,
        chat_id=1001,
        content="hello",
        turn_id="turn:1",
        trace_id="trace:1",
    )
    session = Session(
        user_id=42,
        chat_id=1001,
        session_key="telegram:1001:42",
    )
    result = ReasonerResult(
        content="world",
        tool_calls=[],
        finish_reason="stop",
        turn_id=inbound.turn_id,
        trace_id=inbound.trace_id,
    )
    outbound = OutboundMessage(
        chat_id=1001,
        content=result.content,
        turn_id=result.turn_id,
        trace_id=result.trace_id,
    )
    committed = TurnCommittedEvent(
        turn_id=result.turn_id,
        trace_id=result.trace_id,
        session_key=session.session_key,
        user_id=42,
        inbound_content=inbound.content,
        outbound_message=outbound,
        new_memory_ids=[uuid4()],
    )

    assert committed.turn_id == inbound.turn_id == outbound.turn_id
    assert committed.trace_id == inbound.trace_id == outbound.trace_id
    assert committed.session_key == session.session_key


def test_tool_runtime_result_envelope_carries_trace_identity() -> None:
    result = ToolRuntimeResult(
        ok=True,
        status="success",
        tool_name="echo",
        data={"ok": True},
        turn_id="turn:1",
        trace_id="trace:1",
    )
    envelope = result.to_envelope()
    assert envelope["meta"]["turn_id"] == "turn:1"
    assert envelope["meta"]["trace_id"] == "trace:1"
