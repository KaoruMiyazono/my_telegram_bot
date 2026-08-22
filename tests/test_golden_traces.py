from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.core.types import (
    AfterReasoningCtx,
    BeforeReasoningCtx,
    BeforeTurnCtx,
    InboundMessage,
    OutboundMessage,
    ReasonerResult,
    Session,
)
from agent.pipeline.passive_turn import PassiveTurnPipeline


GOLDEN_DIR = Path(__file__).parent / "golden"


def _tool_schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": name, "parameters": {"type": "object"}},
    }


def _tool_call(name: str) -> dict:
    return {
        "id": f"call-{name}",
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


class _BeforeTurn:
    def __init__(self, *, retrieved_count: int = 0, retrieval_mode: str = "") -> None:
        self.retrieved_count = retrieved_count
        self.retrieval_mode = retrieval_mode

    async def build_ctx(self, inbound: InboundMessage) -> BeforeTurnCtx:
        session = Session(user_id=inbound.user_id, chat_id=inbound.chat_id)
        memories = [object() for _ in range(self.retrieved_count)]
        return BeforeTurnCtx(
            inbound_message=inbound,
            session=session,
            retrieved_memories=memories,  # type: ignore[arg-type]
            content=inbound.content,
            retrieval_trace_raw={"retrieval_mode": self.retrieval_mode},
        )


class _BeforeReasoning:
    def __init__(self, tools: list[str]) -> None:
        self.tools = tools

    async def build_ctx(self, turn: BeforeTurnCtx) -> BeforeReasoningCtx:
        return BeforeReasoningCtx(
            session=turn.session,
            memories=turn.retrieved_memories,
            messages=[{"role": "user", "content": turn.content}],
            tools=[_tool_schema(name) for name in self.tools],
            content=turn.content,
        )


class _Reasoner:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def run_turn(self, ctx: BeforeReasoningCtx) -> ReasonerResult:
        return ReasonerResult(
            content="ok",
            tool_calls=[_tool_call(name) for name in self.calls],
            finish_reason="stop",
            turn_id=ctx.turn_id,
            trace_id=ctx.trace_id,
        )


class _AfterReasoning:
    async def build_ctx(self, result, session, chat_id, user_id) -> AfterReasoningCtx:
        outbound = OutboundMessage(
            chat_id=chat_id,
            content=result.content,
            turn_id=result.turn_id,
            trace_id=result.trace_id,
        )
        return AfterReasoningCtx(
            reasoner_result=result,
            outbound_message=outbound,
            tools_used=tuple(self._names(result.tool_calls)),
        )

    async def persist_messages(self, **kwargs):
        return []

    @staticmethod
    def _names(calls):
        return [call["function"]["name"] for call in calls]


class _AfterTurn:
    telegram_adapter = None

    async def execute(self, **kwargs) -> None:
        return None


class _SessionStore:
    def save(self, *args, **kwargs) -> None:
        return None


SCENARIOS = {
    "plain_text": {
        "content": "你好",
        "visible": [],
        "called": [],
        "retrieved_count": 0,
        "retrieval_mode": "",
    },
    "single_tool": {
        "content": "查天气",
        "visible": ["weather_lookup"],
        "called": ["weather_lookup"],
        "retrieved_count": 0,
        "retrieval_mode": "",
    },
    "multi_tool_round": {
        "content": "查找并核对",
        "visible": ["search_messages", "fetch_messages", "memorize"],
        "called": ["search_messages", "fetch_messages", "memorize"],
        "retrieved_count": 0,
        "retrieval_mode": "",
    },
    "memory_evidence": {
        "content": "我以前喜欢什么",
        "visible": ["recall_memory", "fetch_messages"],
        "called": ["recall_memory", "fetch_messages"],
        "retrieved_count": 2,
        "retrieval_mode": "hybrid_rrf",
    },
    "web_search_fetch_answer": {
        "content": "查询最新消息",
        "visible": ["web_search", "web_fetch"],
        "called": ["web_search", "web_fetch"],
        "retrieved_count": 0,
        "retrieval_mode": "",
    },
}


@pytest.mark.parametrize("name", SCENARIOS)
async def test_passive_pipeline_matches_golden_trace(name: str) -> None:
    scenario = SCENARIOS[name]
    pipeline = PassiveTurnPipeline(
        before_turn=_BeforeTurn(
            retrieved_count=scenario["retrieved_count"],
            retrieval_mode=scenario["retrieval_mode"],
        ),
        before_reasoning=_BeforeReasoning(scenario["visible"]),
        reasoner=_Reasoner(scenario["called"]),
        after_reasoning=_AfterReasoning(),
        after_turn=_AfterTurn(),
    )
    inbound = InboundMessage(
        user_id=42,
        chat_id=1001,
        content=scenario["content"],
        metadata={"turn_id": "turn:golden", "trace_id": "trace:golden"},
    )

    with patch("persistence.session_store.get_session_store", return_value=_SessionStore()):
        await pipeline.execute(inbound)

    assert pipeline.last_trace is not None
    actual = pipeline.last_trace.golden_snapshot()
    expected = json.loads((GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8"))
    assert actual == expected

    serialized = json.dumps(actual, ensure_ascii=False)
    assert scenario["content"] not in serialized
    assert "api_key" not in serialized.lower()
