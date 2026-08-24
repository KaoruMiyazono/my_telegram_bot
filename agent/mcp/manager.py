from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agent.mcp.client import McpClient, McpClientProtocol
from agent.mcp.client import McpCallResult
from agent.mcp.host import McpHost, McpServerRuntime
from agent.mcp.spec import McpServerSpec, load_mcp_specs
from agent.mcp.state import McpRuntimeState, McpRuntimeStateStore
from agent.mcp.tool import build_mcp_tool
from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)
ClientFactory = Callable[[McpServerSpec], McpClientProtocol]


class McpManager:
    """Transactional runtime registration/unregistration for configured MCP servers."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        specs: dict[str, McpServerSpec],
        allowed_commands: set[str],
        allow_loopback_http: bool = True,
        drain_timeout: float = 10.0,
        state_store: McpRuntimeStateStore | None = None,
        client_factory: ClientFactory = McpClient,
    ) -> None:
        self.registry = registry
        self.specs = dict(specs)
        self.allowed_commands = set(allowed_commands)
        self.allow_loopback_http = allow_loopback_http
        self.drain_timeout = drain_timeout
        self.state_store = state_store or McpRuntimeStateStore()
        self.client_factory = client_factory
        self.host = McpHost(on_failure=self._on_call_failure)
        self._locks: dict[str, asyncio.Lock] = {}

    @classmethod
    def from_config(
        cls,
        *,
        registry: ToolRegistry,
        config_path: str | Path,
        allowed_commands: set[str],
        allow_loopback_http: bool = True,
        connect_timeout: float = 20.0,
        drain_timeout: float = 10.0,
    ) -> "McpManager":
        return cls(
            registry=registry,
            specs=load_mcp_specs(
                config_path, default_connect_timeout=connect_timeout
            ),
            allowed_commands=allowed_commands,
            allow_loopback_http=allow_loopback_http,
            drain_timeout=drain_timeout,
        )

    async def start(self) -> None:
        for spec in self.specs.values():
            if not spec.enabled:
                current = self.state_store.get(spec.name)
                self.state_store.transition(
                    spec,
                    status="configured",
                    generation=current.generation if current else 0,
                )
                continue
            try:
                await self.add(spec.name)
            except Exception as exc:
                logger.error("MCP server failed to start: name=%s error=%s", spec.name, exc)

    async def add(self, server_name: str) -> McpRuntimeState:
        """Validate, connect, list tools, then atomically publish one generation."""

        spec = self._configured(server_name)
        async with self._server_lock(server_name):
            spec.validate(
                allowed_commands=self.allowed_commands,
                allow_loopback_http=self.allow_loopback_http,
            )
            prior = self.state_store.get(server_name)
            generation = (prior.generation if prior else 0) + 1
            self.state_store.transition(
                spec, status="starting", generation=generation
            )
            candidate: McpServerRuntime | None = None
            client = self.client_factory(spec)
            try:
                candidate = await asyncio.wait_for(
                    self.host.prepare(spec, generation, client),
                    timeout=spec.connect_timeout,
                )
                wrappers = [
                    build_mcp_tool(
                        host=self.host,
                        server_name=spec.name,
                        generation=generation,
                        remote=remote,
                        timeout_s=spec.call_timeout,
                    )
                    for remote in candidate.tools
                ]
                # Complete tools/list is validated before this single catalog swap.
                self.registry.replace_source_tools(
                    source_type="mcp",
                    source_name=server_name,
                    tools=[
                        (tool, "read-only" if tool.idempotent else "read-write")
                        for tool in wrappers
                    ],
                )
                old = await self.host.activate(candidate)
                state = self.state_store.transition(
                    spec,
                    status="ready",
                    generation=generation,
                    tool_count=len(wrappers),
                    started=True,
                )
                if old is not None:
                    try:
                        await self.host.close_runtime(
                            old, drain_timeout=self.drain_timeout
                        )
                    except Exception as exc:
                        # The new generation is already live. Failure to clean
                        # up an old connection must not roll back its catalog.
                        logger.error(
                            "Old MCP generation failed to close: name=%s "
                            "generation=%s error=%s",
                            server_name,
                            old.generation,
                            exc,
                        )
                return state
            except Exception as exc:
                if candidate is not None:
                    await candidate.client.close()
                else:
                    await client.close()
                self.state_store.transition(
                    spec,
                    status="failed",
                    generation=generation,
                    last_error=str(exc),
                )
                raise

    async def remove(self, server_name: str) -> McpRuntimeState:
        """Reject new calls, remove schemas, drain active calls, then close."""

        spec = self._configured(server_name)
        async with self._server_lock(server_name):
            prior = self.state_store.get(server_name)
            generation = prior.generation if prior else 0
            self.state_store.transition(
                spec, status="draining", generation=generation,
                tool_count=prior.tool_count if prior else 0,
            )
            runtime = await self.host.detach(server_name)
            self.registry.unregister_source(source_type="mcp", source_name=server_name)
            if runtime is not None:
                await self.host.close_runtime(
                    runtime, drain_timeout=self.drain_timeout
                )
            return self.state_store.transition(
                spec, status="stopped", generation=generation
            )

    async def refresh(self, server_name: str) -> McpRuntimeState:
        """Publish a fresh generation after a new tools/list snapshot."""

        return await self.add(server_name)

    async def close(self) -> None:
        for name in list(self.specs):
            state = self.state_store.get(name)
            if state is None or state.status not in {"ready", "starting", "draining"}:
                continue
            try:
                await self.remove(name)
            except Exception as exc:
                logger.error("MCP server failed to stop: name=%s error=%s", name, exc)

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, object],
    ) -> McpCallResult:
        """Call one active remote tool without routing through the LLM catalog."""

        self._configured(server_name)
        state = self.state_store.get(server_name)
        if state is None or state.status != "ready":
            raise RuntimeError(f"MCP server is not ready: {server_name}")
        names = await self.host.tool_names(server_name, state.generation)
        if tool_name not in names:
            raise ValueError(f"Unknown MCP tool: {server_name}.{tool_name}")
        return await self.host.call(
            server_name=server_name,
            generation=state.generation,
            remote_tool=tool_name,
            arguments=arguments,
        )

    def list_states(self) -> list[dict[str, Any]]:
        configured = []
        stored = {state.name: state for state in self.state_store.list()}
        for name, spec in sorted(self.specs.items()):
            state = stored.get(name)
            configured.append(
                asdict(state)
                if state is not None
                else {
                    "name": name,
                    "transport": spec.transport,
                    "status": "configured",
                    "generation": 0,
                    "tool_count": 0,
                    "last_error": "",
                }
            )
        return configured

    async def _on_call_failure(self, name: str, generation: int, error: str) -> None:
        state = self.state_store.get(name)
        if state is None or state.generation != generation:
            return
        spec = self.specs.get(name)
        if spec is not None:
            runtime = await self.host.detach(name)
            self.registry.unregister_source(source_type="mcp", source_name=name)
            if runtime is not None:
                try:
                    await self.host.close_runtime(
                        runtime, drain_timeout=self.drain_timeout
                    )
                except Exception:
                    logger.exception(
                        "Failed MCP runtime could not close cleanly: name=%s", name
                    )
            self.state_store.transition(
                spec,
                status="failed",
                generation=generation,
                tool_count=state.tool_count,
                last_error=error,
            )

    def _configured(self, name: str) -> McpServerSpec:
        try:
            return self.specs[name]
        except KeyError as exc:
            raise ValueError(f"Unknown configured MCP server: {name}") from exc

    def _server_lock(self, name: str) -> asyncio.Lock:
        return self._locks.setdefault(name, asyncio.Lock())


def register_mcp_management_tools(registry: ToolRegistry, manager: McpManager) -> None:
    """Expose safe lifecycle operations; the model chooses only configured names."""

    server_enum = sorted(manager.specs)

    async def add_handler(arguments: dict[str, Any], ctx: Any = None) -> str:
        state = await manager.add(str(arguments.get("server_name") or ""))
        return json.dumps(asdict(state), ensure_ascii=False)

    async def remove_handler(arguments: dict[str, Any], ctx: Any = None) -> str:
        state = await manager.remove(str(arguments.get("server_name") or ""))
        return json.dumps(asdict(state), ensure_ascii=False)

    async def list_handler(arguments: dict[str, Any], ctx: Any = None) -> str:
        return json.dumps({"servers": manager.list_states()}, ensure_ascii=False)

    server_schema: dict[str, Any] = {
        "type": "string",
        "description": "mcp_servers.toml 中预先审计过的服务名",
    }
    if server_enum:
        server_schema["enum"] = server_enum
    registry.register(
        Tool(
            name="mcp_add",
            description="启动或刷新一个预配置 MCP Server，并原子注册其远端工具。",
            parameters={
                "type": "object",
                "properties": {"server_name": server_schema},
                "required": ["server_name"],
            },
            handler=add_handler,
            idempotent=False,
        ),
        risk="read-write", always_on=True, source_type="builtin",
        source_name="mcp_runtime", tier="meta", preloadable=False,
    )
    registry.register(
        Tool(
            name="mcp_remove",
            description="卸载一个预配置 MCP Server；拒绝新调用并排空进行中的调用。",
            parameters={
                "type": "object",
                "properties": {"server_name": server_schema},
                "required": ["server_name"],
            },
            handler=remove_handler,
            idempotent=False,
        ),
        risk="dangerous", always_on=True, source_type="builtin",
        source_name="mcp_runtime", tier="meta", preloadable=False,
    )
    registry.register(
        Tool(
            name="mcp_list",
            description="查看允许使用的 MCP Server、状态、代际和工具数量。",
            parameters={"type": "object", "properties": {}},
            handler=list_handler,
        ),
        risk="read-only", always_on=True, source_type="builtin",
        source_name="mcp_runtime", tier="meta", preloadable=False,
    )
