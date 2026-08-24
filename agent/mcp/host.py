from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from agent.mcp.client import McpCallResult, McpClientProtocol, McpToolInfo
from agent.mcp.spec import McpServerSpec


class McpGenerationStaleError(RuntimeError):
    pass


@dataclass
class McpServerRuntime:
    spec: McpServerSpec
    generation: int
    client: McpClientProtocol
    tools: tuple[McpToolInfo, ...]
    status: str = "ready"
    active_tasks: set[asyncio.Task[object]] = field(default_factory=set)


FailureCallback = Callable[[str, int, str], Awaitable[None] | None]


class McpHost:
    """Own live MCP connections and enforce generation/draining semantics."""

    def __init__(self, *, on_failure: FailureCallback | None = None) -> None:
        self._active: dict[str, McpServerRuntime] = {}
        self._lock = asyncio.Lock()
        self._on_failure = on_failure

    async def prepare(
        self,
        spec: McpServerSpec,
        generation: int,
        client: McpClientProtocol,
    ) -> McpServerRuntime:
        tools = await client.connect()
        return McpServerRuntime(
            spec=spec,
            generation=generation,
            client=client,
            tools=tools,
        )

    async def activate(self, runtime: McpServerRuntime) -> McpServerRuntime | None:
        async with self._lock:
            old = self._active.get(runtime.spec.name)
            if old is not None:
                old.status = "draining"
            self._active[runtime.spec.name] = runtime
            return old

    async def detach(self, name: str) -> McpServerRuntime | None:
        async with self._lock:
            runtime = self._active.pop(name, None)
            if runtime is not None:
                runtime.status = "draining"
            return runtime

    async def tool_names(self, server_name: str, generation: int) -> frozenset[str]:
        async with self._lock:
            runtime = self._active.get(server_name)
            if (
                runtime is None
                or runtime.status != "ready"
                or runtime.generation != generation
            ):
                raise McpGenerationStaleError(
                    f"MCP generation is no longer active: {server_name}@{generation}"
                )
            return frozenset(tool.name for tool in runtime.tools)

    async def call(
        self,
        *,
        server_name: str,
        generation: int,
        remote_tool: str,
        arguments: dict[str, object],
    ) -> McpCallResult:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("MCP call requires an asyncio task")
        async with self._lock:
            runtime = self._active.get(server_name)
            if (
                runtime is None
                or runtime.status != "ready"
                or runtime.generation != generation
            ):
                raise McpGenerationStaleError(
                    f"MCP generation is no longer active: {server_name}@{generation}"
                )
            runtime.active_tasks.add(task)
        try:
            result = await runtime.client.call_tool(remote_tool, dict(arguments))
            async with self._lock:
                current = self._active.get(server_name)
                if (
                    current is None
                    or current is not runtime
                    or runtime.status != "ready"
                    or current.generation != generation
                ):
                    raise McpGenerationStaleError(
                        f"Discarded result from stale MCP generation: "
                        f"{server_name}@{generation}"
                    )
            return result
        except (asyncio.CancelledError, McpGenerationStaleError):
            raise
        except Exception as exc:
            await self._notify_failure(server_name, generation, str(exc))
            raise
        finally:
            runtime.active_tasks.discard(task)

    async def close_runtime(
        self,
        runtime: McpServerRuntime,
        *,
        drain_timeout: float,
    ) -> None:
        runtime.status = "draining"
        current = asyncio.current_task()
        pending = [task for task in runtime.active_tasks if task is not current]
        if pending:
            _, still_pending = await asyncio.wait(pending, timeout=drain_timeout)
            for task in still_pending:
                task.cancel()
            if still_pending:
                await asyncio.gather(*still_pending, return_exceptions=True)
        await runtime.client.close()
        runtime.status = "stopped"

    async def _notify_failure(self, name: str, generation: int, error: str) -> None:
        if self._on_failure is None:
            return
        result = self._on_failure(name, generation, error)
        if asyncio.iscoroutine(result):
            await result
