from __future__ import annotations

import json
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.core.types import BeforeReasoningCtx, Session
from agent.pipeline.reasoner import Reasoner
from agent.prompting import PromptSectionRender
from agent.runtime.context_budget import (
    ContextBudget,
    ContextBudgetConfig,
    ContextBudgetExceeded,
    ContextLevel,
    estimate_context_tokens,
)


def _fixture() -> tuple[list[dict], list[dict], list[PromptSectionRender]]:
    sections = [
        PromptSectionRender("core", "CORE RULES: never reveal secrets", True),
        PromptSectionRender("memory", "unrelated memory " + "m" * 800, False),
    ]
    messages: list[dict] = [
        {
            "role": "system",
            "content": "\n\n---\n\n".join(section.content for section in sections),
        }
    ]
    for index in range(8):
        messages.extend(
            [
                {"role": "user", "content": f"old question {index} " + "x" * 300},
                {"role": "assistant", "content": f"old answer {index} " + "y" * 300},
            ]
        )
    messages.extend(
        [
            {"role": "user", "content": "current coffee question"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_web_1",
                        "type": "function",
                        "function": {"name": "web_fetch", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_web_1",
                "content": json.dumps(
                    {
                        "ok": False,
                        "url": "https://example.com/evidence",
                        "error": {"code": "upstream_timeout", "message": "timed out"},
                        "data": "z" * 3_000,
                    }
                ),
            },
        ]
    )
    tools = [
        {
            "type": "function",
            "function": {
                "name": "web_fetch",
                "description": "fetch " + "d" * 500,
                "parameters": {"type": "object"},
            },
        }
    ]
    return messages, tools, sections


def _budget(window: int) -> ContextBudget:
    return ContextBudget(
        ContextBudgetConfig(
            context_window=window,
            output_reserve=100,
            recent_history_messages=4,
            tool_result_chars_l1=400,
            summary_chars_l2=600,
            tool_result_chars_l3=220,
            summary_chars_l3=300,
            tool_result_chars_l4=180,
        )
    )


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        (4_000, ContextLevel.COMPLETE),
        (2_800, ContextLevel.COMPRESS_TOOL_RESULTS),
        (2_000, ContextLevel.SUMMARIZE_OLD_HISTORY),
        (1_400, ContextLevel.PRUNE_MEMORY_AND_TOOLS),
        (900, ContextLevel.MINIMAL_SAFE),
    ],
)
def test_each_context_level_has_a_budget_boundary(window: int, expected: ContextLevel):
    messages, tools, sections = _fixture()

    projection = _budget(window).project(
        messages,
        tools=tools,
        prompt_sections=sections,
        source_ref="session:7:9",
    )

    assert projection.trace.level == expected
    assert projection.trace.tokens_after <= projection.trace.input_budget
    assert projection.trace.variable_budget == max(
        0,
        projection.trace.input_budget
        - projection.trace.system_tokens
        - projection.trace.current_turn_tokens,
    )


def test_tool_compression_preserves_protocol_error_and_urls():
    messages, tools, sections = _fixture()
    original = deepcopy(messages)

    projection = _budget(2_800).project(
        messages,
        tools=tools,
        prompt_sections=sections,
        source_ref="session:7:9",
    )

    tool_message = next(message for message in projection.messages if message["role"] == "tool")
    compacted = json.loads(tool_message["content"])
    assert tool_message["tool_call_id"] == "call_web_1"
    assert compacted["preserved"]["error"]["code"] == "upstream_timeout"
    assert compacted["preserved"]["url"] == "https://example.com/evidence"
    assert "https://example.com/evidence" in compacted["urls"]
    assert messages == original


def test_repeated_tool_compression_preserves_the_same_evidence_fields():
    messages, tools, sections = _fixture()

    projection = _budget(1_400).project(
        messages,
        tools=tools,
        prompt_sections=sections,
        source_ref="session:7:9",
    )

    tool_message = next(message for message in projection.messages if message["role"] == "tool")
    compacted = json.loads(tool_message["content"])
    assert projection.trace.level == ContextLevel.PRUNE_MEMORY_AND_TOOLS
    assert compacted["preserved"]["error"]["code"] == "upstream_timeout"
    assert compacted["preserved"]["url"] == "https://example.com/evidence"
    assert compacted["original_chars"] > 3_000


def test_history_summary_is_versioned_traceable_and_non_destructive():
    messages, tools, sections = _fixture()
    session_messages = deepcopy(messages[1:-3])
    source_snapshot = deepcopy(messages)

    projection = _budget(2_000).project(
        messages,
        tools=tools,
        prompt_sections=sections,
        source_ref="session:7:9",
    )

    summary = projection.messages[1]["content"]
    assert summary.startswith("# Conversation Summary v1/")
    assert "session:7:9" in summary
    assert "fetch_messages" in summary
    assert messages == source_snapshot
    assert messages[1:-3] == session_messages


def test_l3_prunes_unrelated_dynamic_memory_but_keeps_static_system():
    messages, tools, sections = _fixture()

    projection = _budget(1_400).project(
        messages,
        tools=tools,
        prompt_sections=sections,
        source_ref="session:7:9",
    )

    system = projection.messages[0]["content"]
    assert "CORE RULES: never reveal secrets" in system
    assert "unrelated memory" not in system
    assert projection.trace.evidence_integrity_affected is True
    assert projection.trace.stable_system_digest


def test_l4_keeps_core_current_request_and_necessary_evidence_pair():
    messages, tools, sections = _fixture()

    projection = _budget(900).project(
        messages,
        tools=tools,
        prompt_sections=sections,
        source_ref="session:7:9",
    )

    assert projection.tools == []
    assert projection.messages[0] == {
        "role": "system",
        "content": "CORE RULES: never reveal secrets",
    }
    assert projection.messages[1]["content"] == "current coffee question"
    assert projection.messages[2]["tool_calls"][0]["id"] == "call_web_1"
    assert projection.messages[3]["tool_call_id"] == "call_web_1"
    compacted = json.loads(projection.messages[3]["content"])
    assert compacted["preserved"]["error"]["message"] == "timed out"
    assert compacted["preserved"]["url"] == "https://example.com/evidence"


def test_budget_refuses_to_silently_drop_protected_system_or_current_request():
    messages = [
        {"role": "system", "content": "protected" * 500},
        {"role": "user", "content": "current" * 500},
    ]

    with pytest.raises(ContextBudgetExceeded):
        _budget(500).project(messages)


@pytest.mark.asyncio
async def test_reasoner_retries_provider_context_overflow_at_next_level():
    response = MagicMock()
    choice = MagicMock()
    choice.message.content = "done"
    choice.message.tool_calls = []
    choice.finish_reason = "stop"
    response.choices = [choice]

    with patch("agent.pipeline.reasoner.AsyncOpenAI") as openai:
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(
            side_effect=[RuntimeError("context_length_exceeded"), response]
        )
        openai.return_value = client
        reasoner = Reasoner(context_budget=_budget(4_000))
        ctx = BeforeReasoningCtx(
            session=Session(user_id=7, chat_id=9),
            memories=[],
            messages=[
                {"role": "system", "content": "CORE"},
                {"role": "user", "content": "hello"},
            ],
            tools=[],
            prompt_sections=[PromptSectionRender("core", "CORE", True)],
        )

        result = await reasoner.run_turn(ctx)

    assert result.content == "done"
    assert [trace["level"] for trace in result.context_trace] == ["L0", "L1"]
    assert client.chat.completions.create.await_count == 2


def test_estimator_counts_tool_schemas_as_part_of_input_budget():
    messages, tools, _ = _fixture()
    assert estimate_context_tokens(messages, tools) > estimate_context_tokens(messages, [])
