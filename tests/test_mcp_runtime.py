from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agent.mcp.client import McpCallResult, McpToolInfo
from agent.mcp.host import McpGenerationStaleError
from agent.mcp.manager import McpManager, register_mcp_management_tools
from agent.mcp.spec import McpServerSpec
from agent.mcp.state import McpRuntimeStateStore
from agent.tools.registry import ToolRegistry
from persistence.database import get_connection, init_db


class FakeClient:
    def __init__(
        self,
        tools: tuple[McpToolInfo, ...],
        *,
        started: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self.tools = tools
        self.started = started
        self.release = release
        self.closed = False

    async def connect(self) -> tuple[McpToolInfo, ...]:
        return self.tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpCallResult:
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        return McpCallResult(
            is_error=False,
            content=[{"type": "text", "text": json.dumps(arguments)}],
        )

    async def close(self) -> None:
        self.closed = True


class FailingClient(FakeClient):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpCallResult:
        raise ConnectionError("test MCP transport crashed")


def _spec(name: str = "demo") -> McpServerSpec:
    return McpServerSpec(
        name=name,
        transport="stdio",
        command=Path(sys.executable).name,
        args=("unused.py",),
    )


def _tool(name: str) -> McpToolInfo:
    return McpToolInfo(
        name=name,
        description=f"{name} tool",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        read_only=True,
    )


@pytest.mark.asyncio
async def test_runtime_add_refresh_remove_is_atomic_and_generational() -> None:
    init_db()
    registry = ToolRegistry()
    created = 0

    def factory(spec: McpServerSpec) -> FakeClient:
        nonlocal created
        created += 1
        return FakeClient((_tool("first" if created == 1 else "second"),))

    manager = McpManager(
        registry=registry,
        specs={"demo": _spec()},
        allowed_commands={Path(sys.executable).name},
        client_factory=factory,
    )

    first = await manager.add("demo")
    assert first.status == "ready"
    assert first.generation == 1
    assert registry.has_tool("mcp_demo__first")

    second = await manager.refresh("demo")
    assert second.generation == 2
    assert not registry.has_tool("mcp_demo__first")
    assert registry.has_tool("mcp_demo__second")

    stopped = await manager.remove("demo")
    assert stopped.status == "stopped"
    assert not registry.has_tool("mcp_demo__second")


@pytest.mark.asyncio
async def test_remove_drains_and_discards_old_generation_result() -> None:
    init_db()
    registry = ToolRegistry()
    started = asyncio.Event()
    release = asyncio.Event()
    client = FakeClient((_tool("slow"),), started=started, release=release)
    manager = McpManager(
        registry=registry,
        specs={"demo": _spec()},
        allowed_commands={Path(sys.executable).name},
        client_factory=lambda spec: client,
        drain_timeout=1,
    )
    await manager.add("demo")
    tool = registry.get_tool("mcp_demo__slow")
    assert tool is not None
    call = asyncio.create_task(tool.execute({"text": "old"}))
    await started.wait()
    removal = asyncio.create_task(manager.remove("demo"))
    await asyncio.sleep(0)
    assert not registry.has_tool("mcp_demo__slow")
    release.set()
    with pytest.raises(McpGenerationStaleError):
        await call
    await removal
    assert client.closed is True


@pytest.mark.asyncio
async def test_real_stdio_server_register_call_and_remove() -> None:
    init_db()
    server_file = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"
    spec = McpServerSpec(
        name="echo",
        transport="stdio",
        command=sys.executable,
        args=(str(server_file),),
        connect_timeout=10,
        call_timeout=10,
    )
    registry = ToolRegistry()
    manager = McpManager(
        registry=registry,
        specs={"echo": spec},
        allowed_commands={Path(sys.executable).name},
    )
    state = await manager.add("echo")
    assert state.tool_count == 2
    tool = registry.get_tool("mcp_echo__echo")
    assert tool is not None
    payload = json.loads(await tool.execute({"text": "hello"}))
    assert payload["untrusted_external_data"] is True
    assert payload["generation"] == 1
    assert any("echo:hello" in str(block) for block in payload["content"])
    await manager.remove("echo")


@pytest.mark.asyncio
async def test_server_crash_is_isolated_and_marks_generation_failed() -> None:
    init_db()
    registry = ToolRegistry()
    client = FailingClient((_tool("explode"),))
    manager = McpManager(
        registry=registry,
        specs={"demo": _spec()},
        allowed_commands={Path(sys.executable).name},
        client_factory=lambda spec: client,
    )
    await manager.add("demo")
    tool = registry.get_tool("mcp_demo__explode")
    assert tool is not None
    with pytest.raises(ConnectionError, match="transport crashed"):
        await tool.execute({"text": "boom"})
    state = manager.state_store.get("demo")
    assert state is not None and state.status == "failed"
    assert not registry.has_tool("mcp_demo__explode")
    assert client.closed is True


def test_security_validation_and_persisted_config_do_not_store_secret_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    monkeypatch.setenv("MCP_TEST_TOKEN", "super-secret-value")
    spec = McpServerSpec(
        name="remote",
        transport="streamable_http",
        url="https://example.test/mcp",
        header_refs={"Authorization": "MCP_TEST_TOKEN"},
    )
    spec.validate(allowed_commands=set(), allow_loopback_http=True)
    store = McpRuntimeStateStore()
    store.transition(spec, status="configured", generation=0)
    config_json = get_connection().execute(
        "SELECT config_json FROM mcp_runtime_servers WHERE name = 'remote'"
    ).fetchone()[0]
    assert "MCP_TEST_TOKEN" in config_json
    assert "super-secret-value" not in config_json

    with pytest.raises(ValueError, match="HTTPS"):
        McpServerSpec(
            name="unsafe",
            transport="streamable_http",
            url="http://example.test/mcp",
        ).validate(allowed_commands=set(), allow_loopback_http=True)


def test_management_tools_accept_only_preconfigured_server_names() -> None:
    init_db()
    registry = ToolRegistry()
    manager = McpManager(
        registry=registry,
        specs={"demo": _spec()},
        allowed_commands={Path(sys.executable).name},
        client_factory=lambda spec: FakeClient((_tool("echo"),)),
    )
    register_mcp_management_tools(registry, manager)
    schema = registry.get_tool("mcp_add").parameters  # type: ignore[union-attr]
    assert schema["properties"]["server_name"]["enum"] == ["demo"]
    assert "command" not in schema["properties"]
    assert "url" not in schema["properties"]
