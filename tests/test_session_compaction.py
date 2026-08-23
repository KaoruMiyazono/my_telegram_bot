from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.core.types import BeforeReasoningCtx, Session
from agent.pipeline.reasoner import Reasoner
from agent.runtime.session_compaction import (
    SessionCompactionConfig,
    SessionContextCompactionError,
    SessionContextCompactor,
)
from persistence.database import init_db
from persistence.session_compaction_store import SessionCompactionStore
from persistence.session_store import SessionStore


VALID_SUMMARY = """## Goal
完成上下文压缩验证。
## Constraints & Preferences
保留原始消息与最近对话。
## Progress
### Done
旧交互已归纳。
### In Progress
继续当前请求。
### Blocked
无。
## Key Decisions
使用不可变压缩代次。
## Next Steps
回答当前问题。
## Critical Context
原始证据仍在Session数据库中。"""


def _history(pairs: int, *, chars: int = 360) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for index in range(pairs):
        messages.extend(
            [
                {"role": "user", "content": f"user-{index}:" + "甲" * chars},
                {"role": "assistant", "content": f"assistant-{index}:" + "乙" * chars},
            ]
        )
    return messages


def _config(**overrides) -> SessionCompactionConfig:
    values = {
        "context_window": 2_800,
        "output_reserve": 300,
        "soft_limit_ratio": 0.74,
        "keep_recent_tokens": 300,
        "summary_max_tokens": 500,
    }
    values.update(overrides)
    return SessionCompactionConfig(**values)


async def _summary_builder(_prompt: str, _max_tokens: int):
    return VALID_SUMMARY, {"prompt_tokens": 100, "completion_tokens": 80}


async def test_compaction_persists_generation_without_rewriting_raw_session() -> None:
    init_db()
    raw = _history(8)
    original = deepcopy(raw)
    SessionStore().save(7, 11, raw)
    messages = [
        {"role": "system", "content": "stable system"},
        *deepcopy(raw),
        {"role": "user", "content": "current request"},
    ]
    compactor = SessionContextCompactor(
        user_id=7,
        chat_id=11,
        session_messages=raw,
        model="test-model",
        summary_builder=_summary_builder,
        config=_config(),
    )

    prepared = await compactor.prepare(messages, tools=[])

    assert prepared.compacted is True
    assert prepared.checkpoint is not None
    assert prepared.checkpoint.generation == 1
    assert prepared.trace["trigger"] == "soft_limit"
    assert messages[0]["content"] == "stable system"
    assert messages[1]["role"] == "system"
    assert "<session-context-compaction>" in messages[1]["content"]
    assert messages[-1]["content"] == "current request"
    assert SessionStore().load(7, 11) == original

    stored = SessionCompactionStore().get_active(7, 11)
    assert stored is not None
    assert stored.source_ref == "session:7:11#compaction:1"
    assert stored.source_message_ids[0] == "session:7:11#msg:0"
    assert stored.estimated_tokens_after < stored.soft_limit_tokens


async def test_active_generation_is_reused_after_reload() -> None:
    init_db()
    raw = _history(8)
    SessionStore().save(7, 12, raw)
    first_messages = [
        {"role": "system", "content": "stable system"},
        *deepcopy(raw),
        {"role": "user", "content": "first current"},
    ]
    first = SessionContextCompactor(
        user_id=7,
        chat_id=12,
        session_messages=raw,
        model="test-model",
        summary_builder=_summary_builder,
        config=_config(),
    )
    await first.prepare(first_messages, tools=[])

    reloaded_messages = [
        {"role": "system", "content": "stable system"},
        *deepcopy(raw),
        {"role": "user", "content": "second current"},
    ]
    reloaded = SessionContextCompactor(
        user_id=7,
        chat_id=12,
        session_messages=raw,
        model="test-model",
        summary_builder=_summary_builder,
        config=_config(context_window=20_000),
    )

    prepared = await reloaded.prepare(reloaded_messages, tools=[])

    assert prepared.checkpoint is None
    assert prepared.trace["trigger"] == "active_checkpoint"
    assert "generation=1" in reloaded_messages[1]["content"]
    assert reloaded_messages[-1]["content"] == "second current"
    assert len(SessionCompactionStore().list_generations(7, 12)) == 1


async def test_new_messages_create_a_child_generation() -> None:
    init_db()
    raw = _history(8)
    SessionStore().save(7, 14, raw)
    first_messages = [
        {"role": "system", "content": "stable system"},
        *deepcopy(raw),
        {"role": "user", "content": "first current"},
    ]
    first = SessionContextCompactor(
        user_id=7,
        chat_id=14,
        session_messages=raw,
        model="test-model",
        summary_builder=_summary_builder,
        config=_config(),
    )
    first_result = await first.prepare(first_messages, tools=[])
    assert first_result.checkpoint is not None

    extended = raw + _history(4, chars=180)
    SessionStore().save(7, 14, extended)
    second_messages = [
        {"role": "system", "content": "stable system"},
        *deepcopy(extended),
        {"role": "user", "content": "second current"},
    ]
    second = SessionContextCompactor(
        user_id=7,
        chat_id=14,
        session_messages=extended,
        model="test-model",
        summary_builder=_summary_builder,
        config=_config(),
    )
    second_result = await second.prepare(second_messages, tools=[], force=True)

    assert second_result.checkpoint is not None
    assert second_result.checkpoint.generation == 2
    assert second_result.checkpoint.parent_generation == 1
    assert set(first_result.checkpoint.source_message_ids).issubset(
        second_result.checkpoint.source_message_ids
    )
    assert [item.generation for item in SessionCompactionStore().list_generations(7, 14)] == [1, 2]


async def test_current_tool_protocol_is_preserved_as_an_indivisible_tail() -> None:
    init_db()
    raw = _history(8)
    SessionStore().save(7, 15, raw)
    current_tail = [
        {"role": "user", "content": "search now"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "result"},
    ]
    messages = [
        {"role": "system", "content": "stable system"},
        *deepcopy(raw),
        *deepcopy(current_tail),
    ]
    compactor = SessionContextCompactor(
        user_id=7,
        chat_id=15,
        session_messages=raw,
        model="test-model",
        summary_builder=_summary_builder,
        config=_config(),
    )

    await compactor.prepare(messages, tools=[])

    assert messages[-3:] == current_tail


async def test_compaction_fails_loudly_without_a_closed_prefix() -> None:
    init_db()
    raw = _history(1, chars=50)
    SessionStore().save(7, 13, raw)
    messages = [
        {"role": "system", "content": "stable system"},
        *deepcopy(raw),
        {"role": "user", "content": "current"},
    ]
    compactor = SessionContextCompactor(
        user_id=7,
        chat_id=13,
        session_messages=raw,
        model="test-model",
        summary_builder=_summary_builder,
        config=_config(),
    )

    with pytest.raises(SessionContextCompactionError, match="no_closed_prefix"):
        await compactor.prepare(messages, tools=[], force=True)
    assert SessionCompactionStore().get_head(7, 13) == 0


def _response(content: str) -> MagicMock:
    response = MagicMock()
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = []
    choice.finish_reason = "stop"
    response.choices = [choice]
    response.usage = None
    return response


async def test_provider_overflow_forces_one_persisted_compaction_and_retry() -> None:
    init_db()
    raw = _history(5, chars=100)
    SessionStore().save(8, 21, raw)
    ctx = BeforeReasoningCtx(
        session=Session(user_id=8, chat_id=21, messages=deepcopy(raw)),
        memories=[],
        messages=[
            {"role": "system", "content": "stable system"},
            *deepcopy(raw),
            {"role": "user", "content": "continue"},
        ],
        tools=[],
        content="continue",
    )

    with patch("agent.pipeline.reasoner.AsyncOpenAI") as openai_cls:
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(
            side_effect=[
                Exception("maximum context length exceeded"),
                _response(VALID_SUMMARY),
                _response("完成"),
            ]
        )
        openai_cls.return_value = client
        reasoner = Reasoner(
            session_compaction_config=_config(context_window=5_000),
        )

        result = await reasoner.run_turn(ctx)

    assert result.content == "完成"
    assert [trace["trigger"] for trace in result.context_trace] == [
        "within_budget",
        "context_overflow",
    ]
    assert client.chat.completions.create.call_count == 3
    active = SessionCompactionStore().get_active(8, 21)
    assert active is not None
    assert active.trigger == "context_overflow"
