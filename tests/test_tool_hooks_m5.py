from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agent.tool_hooks import (
    HookContext,
    HookOutcome,
    ToolBusinessError,
    ToolExecutor,
    ToolHook,
    ToolUserCancelledError,
)
from agent.tool_hooks.types import ToolExecutionRequest
from agent.tools import Tool, ToolRegistry
from agent.tools.runtime import ToolRuntime, ToolRuntimeConfig


class RewriteAndAuditHook(ToolHook):
    name = "rewrite_and_audit"
    event = "before_call"

    def matches(self, ctx: HookContext) -> bool:
        return True

    async def run(self, ctx: HookContext) -> HookOutcome:
        return HookOutcome(
            updated_input={**ctx.current_arguments, "query": "rewritten"},
            audit_metadata={"actor": "policy", "token": "audit-secret"},
        )


class DenyWritesHook(ToolHook):
    name = "deny_writes"
    event = "before_call"

    def matches(self, ctx: HookContext) -> bool:
        return ctx.request.risk != "read-only"

    async def run(self, ctx: HookContext) -> HookOutcome:
        return HookOutcome(decision="deny", reason="write disabled")


class AddReferenceHook(ToolHook):
    name = "add_reference"
    event = "after_call"

    def matches(self, ctx: HookContext) -> bool:
        return True

    async def run(self, ctx: HookContext) -> HookOutcome:
        return HookOutcome(
            output_updated=True,
            updated_output={**ctx.result, "reference": "https://example.test"},
            audit_metadata={"reference_count": 1},
        )


class ErrorAuditHook(ToolHook):
    name = "error_audit"
    event = "on_error"

    def matches(self, ctx: HookContext) -> bool:
        return True

    async def run(self, ctx: HookContext) -> HookOutcome:
        return HookOutcome(
            audit_metadata={"observed_error": ctx.error_code},
            retryable=ctx.retryable,
        )


class CancelCleanupHook(ToolHook):
    name = "cancel_cleanup"
    event = "on_cancel"

    def __init__(self) -> None:
        self.called = False

    def matches(self, ctx: HookContext) -> bool:
        return True

    async def run(self, ctx: HookContext) -> HookOutcome:
        self.called = True
        return HookOutcome(audit_metadata={"cleaned": True})


def _runtime(
    tool: Tool,
    *,
    hooks: list[ToolHook] | None = None,
    risk: str = "read-only",
    source_type: str = "builtin",
) -> ToolRuntime:
    registry = ToolRegistry()
    registry.register(tool, risk=risk, source_type=source_type, source_name="test")
    return ToolRuntime(
        registry=registry,
        executor=ToolExecutor(hooks),
        config=ToolRuntimeConfig(
            default_timeout_s=0.02,
            read_only_max_retries=1,
            retry_backoff_s=0,
        ),
    )


def _tool(handler: Any, *, output_schema: dict[str, Any] | None = None) -> Tool:
    return Tool(
        name="demo",
        description="demo",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "api_key": {"type": "string"},
            },
            "required": ["query"],
        },
        output_schema=output_schema,
        handler=handler,
    )


async def test_before_after_trace_and_envelope_are_redacted() -> None:
    async def handler(arguments: dict[str, Any], ctx: Any) -> dict[str, Any]:
        assert arguments["api_key"] == "real-secret"
        return {"answer": arguments["query"], "token": "result-secret"}

    runtime = _runtime(
        _tool(handler), hooks=[RewriteAndAuditHook(), AddReferenceHook()]
    )
    result = await runtime.execute_call(
        call_id="call-1",
        tool_name="demo",
        raw_arguments={"query": "original", "api_key": "real-secret"},
    )
    envelope = result.to_envelope()

    assert result.ok is True
    assert envelope["data"]["answer"] == "rewritten"
    assert envelope["data"]["token"] == "[REDACTED]"
    assert envelope["data"]["reference"] == "https://example.test"
    assert envelope["meta"]["final_arguments"]["api_key"] == "[REDACTED]"
    serialized = json.dumps(envelope, ensure_ascii=False)
    assert "real-secret" not in serialized
    assert "result-secret" not in serialized
    assert "audit-secret" not in serialized
    assert envelope["meta"]["audit_metadata"]["reference_count"] == 1


async def test_read_write_tool_can_be_denied_before_handler() -> None:
    calls = 0

    async def handler(arguments: dict[str, Any], ctx: Any) -> str:
        nonlocal calls
        calls += 1
        return "unexpected"

    result = await _runtime(
        _tool(handler), hooks=[DenyWritesHook()], risk="read-write"
    ).execute_call(
        call_id="call-2", tool_name="demo", raw_arguments={"query": "delete"}
    )
    assert result.status == "denied"
    assert result.error_code == "policy_check"
    assert calls == 0


@pytest.mark.parametrize(
    ("source_type", "error", "expected"),
    [
        ("builtin", ConnectionError("down"), "network_error"),
        ("mcp", ConnectionError("down"), "mcp_unavailable"),
        ("builtin", ToolBusinessError("bad request"), "business_error"),
        ("builtin", ToolUserCancelledError("stopped"), "cancelled"),
    ],
)
async def test_typed_error_taxonomy(
    source_type: str, error: Exception, expected: str
) -> None:
    async def handler(arguments: dict[str, Any], ctx: Any) -> str:
        raise error

    result = await _runtime(
        _tool(handler), hooks=[ErrorAuditHook()], source_type=source_type
    ).execute_call(
        call_id="call-3", tool_name="demo", raw_arguments={"query": "q"}
    )
    assert result.ok is False
    assert result.error_code == expected
    assert result.audit_metadata["observed_error"] == expected
    expected_calls = 2 if expected in {"network_error", "mcp_unavailable"} else 1
    assert len(result.error_hook_trace) == expected_calls


async def test_retry_requires_readonly_and_idempotent() -> None:
    calls = 0

    async def handler(arguments: dict[str, Any], ctx: Any) -> str:
        nonlocal calls
        calls += 1
        raise ConnectionError("retryable transport")

    tool = _tool(handler)
    tool.idempotent = False
    result = await _runtime(tool, hooks=[ErrorAuditHook()]).execute_call(
        call_id="call-4", tool_name="demo", raw_arguments={"query": "q"}
    )
    assert result.error_code == "network_error"
    assert result.retry_count == 0
    assert calls == 1


async def test_input_and_output_validation_enter_error_hook() -> None:
    input_result = await _runtime(
        _tool(lambda arguments, ctx: "unused"), hooks=[ErrorAuditHook()]
    ).execute_call(call_id="i", tool_name="demo", raw_arguments={})
    assert input_result.error_code == "input_validation"
    assert input_result.audit_metadata["observed_error"] == "input_validation"

    output_schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    output_result = await _runtime(
        _tool(lambda arguments, ctx: {"wrong": True}, output_schema=output_schema),
        hooks=[ErrorAuditHook()],
    ).execute_call(
        call_id="o", tool_name="demo", raw_arguments={"query": "q"}
    )
    assert output_result.error_code == "output_validation"
    assert output_result.audit_metadata["observed_error"] == "output_validation"


async def test_async_cancellation_runs_cleanup_hook_and_propagates() -> None:
    cleanup = CancelCleanupHook()
    executor = ToolExecutor([cleanup])
    started = asyncio.Event()

    async def invoke(name: str, arguments: dict[str, Any]) -> str:
        started.set()
        await asyncio.Event().wait()
        return "never"

    task = asyncio.create_task(
        executor.execute(
            ToolExecutionRequest(
                call_id="cancel", tool_name="demo", arguments={}, source="passive"
            ),
            invoke,
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleanup.called is True


@pytest.mark.parametrize("source_type", ["builtin", "plugin", "mcp"])
async def test_all_tool_sources_share_one_runtime_envelope(source_type: str) -> None:
    result = await _runtime(
        _tool(lambda arguments, ctx: {"source": source_type}),
        source_type=source_type,
    ).execute_call(
        call_id=source_type,
        tool_name="demo",
        raw_arguments={"query": "q"},
    )
    envelope = result.to_envelope()
    assert set(envelope) >= {"ok", "status", "data", "error", "meta"}
    assert envelope["data"] == {"source": source_type}
