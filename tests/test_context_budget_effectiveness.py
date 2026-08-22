"""Reproducible effectiveness experiment for the five context levels."""

from __future__ import annotations

import json
from copy import deepcopy

from agent.runtime.context_budget import ContextLevel
from tests.test_context_budget import _budget, _fixture


def test_five_level_context_compression_is_effective_and_safe():
    """Compare L0-L4 on one payload and print resume-friendly metrics."""

    scenarios = (
        (4_000, ContextLevel.COMPLETE),
        (2_800, ContextLevel.COMPRESS_TOOL_RESULTS),
        (2_000, ContextLevel.SUMMARIZE_OLD_HISTORY),
        (1_400, ContextLevel.PRUNE_MEMORY_AND_TOOLS),
        (900, ContextLevel.MINIMAL_SAFE),
    )
    original_messages, original_tools, sections = _fixture()
    source_snapshot = deepcopy(original_messages)
    rows: list[dict[str, object]] = []

    for context_window, expected_level in scenarios:
        projection = _budget(context_window).project(
            original_messages,
            tools=original_tools,
            prompt_sections=sections,
            source_ref="session:7:9",
        )
        trace = projection.trace
        reduction_pct = round(
            (1 - trace.tokens_after / trace.tokens_before) * 100,
            1,
        )

        assert trace.level == expected_level
        assert trace.tokens_after <= trace.input_budget
        assert "CORE RULES: never reveal secrets" in projection.messages[0]["content"]
        assert any(
            message.get("role") == "user"
            and message.get("content") == "current coffee question"
            for message in projection.messages
        )

        if expected_level >= ContextLevel.COMPRESS_TOOL_RESULTS:
            assert trace.tokens_after < trace.tokens_before

        tool_message = next(
            message
            for message in projection.messages
            if message.get("role") == "tool"
        )
        assert tool_message["tool_call_id"] == "call_web_1"
        tool_payload = json.loads(tool_message["content"])
        if expected_level == ContextLevel.COMPLETE:
            assert tool_payload["error"]["code"] == "upstream_timeout"
            assert tool_payload["url"] == "https://example.com/evidence"
        else:
            assert tool_payload["preserved"]["error"]["code"] == "upstream_timeout"
            assert tool_payload["preserved"]["url"] == "https://example.com/evidence"

        if expected_level in {
            ContextLevel.SUMMARIZE_OLD_HISTORY,
            ContextLevel.PRUNE_MEMORY_AND_TOOLS,
        }:
            summary = projection.messages[1]["content"]
            assert summary.startswith("# Conversation Summary v1/")
            assert "session:7:9" in summary

        rows.append(
            {
                "level": f"L{int(trace.level)}",
                "context_window": context_window,
                "input_budget": trace.input_budget,
                "tokens_before": trace.tokens_before,
                "tokens_after": trace.tokens_after,
                "tokens_saved": trace.tokens_before - trace.tokens_after,
                "reduction_pct": reduction_pct,
                "messages_before": len(original_messages),
                "messages_after": len(projection.messages),
                "tools_before": len(original_tools),
                "tools_after": len(projection.tools),
                "changes": [change.action for change in trace.changes],
            }
        )

    assert original_messages == source_snapshot
    assert [row["tokens_after"] for row in rows] == sorted(
        [row["tokens_after"] for row in rows],
        reverse=True,
    )
    print("M2_CONTEXT_EFFECTIVENESS=" + json.dumps(rows, ensure_ascii=False))
