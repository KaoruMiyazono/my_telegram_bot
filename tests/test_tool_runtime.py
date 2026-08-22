from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.tool_hooks.base import ToolHook
from agent.tool_hooks.types import HookContext, HookOutcome
from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry
from agent.tools.runtime import ToolRuntime, ToolRuntimeConfig


class RewriteHook(ToolHook):
    event = "pre_tool_use"
    name = "rewrite"

    def matches(self, ctx: HookContext) -> bool:
        return ctx.request.tool_name == "echo"

    async def run(self, ctx: HookContext) -> HookOutcome:
        return HookOutcome(updated_input={**ctx.current_arguments, "x": 2})


class DenyHook(ToolHook):
    event = "pre_tool_use"
    name = "deny"

    def matches(self, ctx: HookContext) -> bool:
        return ctx.request.tool_name == "echo"

    async def run(self, ctx: HookContext) -> HookOutcome:
        return HookOutcome(decision="deny", reason="blocked")


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "x": {"type": "integer", "minimum": 1, "maximum": 5},
            "mode": {"type": "string", "enum": ["a", "b"]},
            "items": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["x"],
    }


def _runtime(
    registry: ToolRegistry,
    *,
    hooks: list[ToolHook] | None = None,
) -> ToolRuntime:
    from agent.tool_hooks.executor import ToolExecutor

    return ToolRuntime(
        registry=registry,
        executor=ToolExecutor(hooks or []),
        config=ToolRuntimeConfig(default_timeout_s=1.0, retry_backoff_s=0.0),
    )


async def test_runtime_success_and_json_envelope() -> None:
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="echo",
            description="echo",
            parameters=_schema(),
            handler=lambda args, ctx: json.dumps({"x": args["x"]}, ensure_ascii=False),
        ),
        risk="read-only",
    )
    result = await _runtime(registry).execute_call(
        call_id="c1",
        tool_name="echo",
        raw_arguments='{"x": 1}',
    )
    payload = result.to_envelope()
    assert payload["ok"] is True
    assert payload["data"] == {"x": 1}
    assert payload["meta"]["final_arguments"] == {"x": 1}
    print("test_runtime_success_and_json_envelope: PASS")


async def test_runtime_argument_parse_error() -> None:
    result = await _runtime(ToolRegistry()).execute_call(
        call_id="c1",
        tool_name="echo",
        raw_arguments="{bad",
    )
    payload = result.to_envelope()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "argument_parse"
    assert payload["error"]["retryable"] is True
    print("test_runtime_argument_parse_error: PASS")


async def test_runtime_input_validation_blocks_handler() -> None:
    calls = {"count": 0}
    registry = ToolRegistry()

    def handler(args, ctx):
        calls["count"] += 1
        return "should-not-run"

    registry.register(
        Tool(name="echo", description="echo", parameters=_schema(), handler=handler),
        risk="read-only",
    )
    result = await _runtime(registry).execute_call(
        call_id="c1",
        tool_name="echo",
        raw_arguments='{"mode": "c"}',
    )
    payload = result.to_envelope()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "input_validation"
    assert "缺少必填字段：x" in payload["error"]["message"]
    assert calls["count"] == 0
    print("test_runtime_input_validation_blocks_handler: PASS")


async def test_runtime_tool_lookup_error() -> None:
    result = await _runtime(ToolRegistry()).execute_call(
        call_id="c1",
        tool_name="missing",
        raw_arguments="{}",
    )
    payload = result.to_envelope()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "tool_lookup"
    print("test_runtime_tool_lookup_error: PASS")


async def test_runtime_hook_rewrite_and_deny() -> None:
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="echo",
            description="echo",
            parameters=_schema(),
            handler=lambda args, ctx: json.dumps({"x": args["x"]}, ensure_ascii=False),
        ),
        risk="read-only",
    )
    rewritten = await _runtime(registry, hooks=[RewriteHook()]).execute_call(
        call_id="c1",
        tool_name="echo",
        raw_arguments='{"x": 1}',
    )
    assert rewritten.ok is True
    assert rewritten.final_arguments == {"x": 2}

    denied = await _runtime(registry, hooks=[DenyHook()]).execute_call(
        call_id="c2",
        tool_name="echo",
        raw_arguments='{"x": 1}',
    )
    payload = denied.to_envelope()
    assert payload["ok"] is False
    assert payload["status"] == "denied"
    assert payload["error"]["code"] == "policy_check"
    print("test_runtime_hook_rewrite_and_deny: PASS")


async def test_runtime_timeout_retries_readonly() -> None:
    registry = ToolRegistry()

    async def slow(args, ctx):
        await asyncio.sleep(0.05)
        return "late"

    registry.register(
        Tool(
            name="slow",
            description="slow",
            parameters={"type": "object", "properties": {}},
            handler=slow,
            timeout_s=0.01,
        ),
        risk="read-only",
    )
    result = await _runtime(registry).execute_call(
        call_id="c1",
        tool_name="slow",
        raw_arguments="{}",
    )
    payload = result.to_envelope()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "timeout"
    assert payload["meta"]["retry_count"] == 1
    print("test_runtime_timeout_retries_readonly: PASS")


async def test_runtime_retries_transient_readonly_but_not_write() -> None:
    readonly_calls = {"count": 0}
    write_calls = {"count": 0}
    registry = ToolRegistry()

    def flaky(args, ctx):
        readonly_calls["count"] += 1
        if readonly_calls["count"] == 1:
            raise RuntimeError("temporarily unavailable")
        return json.dumps({"ok": "after-retry"}, ensure_ascii=False)

    def write_flaky(args, ctx):
        write_calls["count"] += 1
        raise RuntimeError("temporarily unavailable")

    registry.register(
        Tool(
            name="flaky",
            description="flaky",
            parameters={"type": "object", "properties": {}},
            handler=flaky,
        ),
        risk="read-only",
    )
    registry.register(
        Tool(
            name="write_flaky",
            description="write",
            parameters={"type": "object", "properties": {}},
            handler=write_flaky,
        ),
        risk="read-write",
    )
    readonly = await _runtime(registry).execute_call(
        call_id="c1",
        tool_name="flaky",
        raw_arguments="{}",
    )
    assert readonly.ok is True
    assert readonly.retry_count == 1
    assert readonly_calls["count"] == 2

    write = await _runtime(registry).execute_call(
        call_id="c2",
        tool_name="write_flaky",
        raw_arguments="{}",
    )
    assert write.ok is False
    assert write.retry_count == 0
    assert write_calls["count"] == 1
    print("test_runtime_retries_transient_readonly_but_not_write: PASS")


async def main() -> None:
    await test_runtime_success_and_json_envelope()
    await test_runtime_argument_parse_error()
    await test_runtime_input_validation_blocks_handler()
    await test_runtime_tool_lookup_error()
    await test_runtime_hook_rewrite_and_deny()
    await test_runtime_timeout_retries_readonly()
    await test_runtime_retries_transient_readonly_but_not_write()
    print("\nAll tool runtime tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
