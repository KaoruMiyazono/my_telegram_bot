from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from agent.mcp.spec import McpServerSpec


@dataclass(frozen=True)
class McpToolInfo:
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool = False


@dataclass(frozen=True)
class McpCallResult:
    is_error: bool
    content: list[dict[str, Any]]


class McpClientProtocol(Protocol):
    async def connect(self) -> tuple[McpToolInfo, ...]: ...
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpCallResult: ...
    async def close(self) -> None: ...


class McpClient:
    """Transport-neutral client over the official MCP Python SDK.

    The SDK transport context is opened and closed by one owner task. Public
    methods send commands to that task, avoiding AnyIO cancel-scope corruption
    when mcp_add and mcp_remove happen in different Telegram turns.
    """

    def __init__(self, spec: McpServerSpec) -> None:
        self.spec = spec
        self._commands: asyncio.Queue[tuple[str, Any, asyncio.Future[Any]]] = (
            asyncio.Queue()
        )
        self._runner: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[tuple[McpToolInfo, ...]] | None = None

    async def connect(self) -> tuple[McpToolInfo, ...]:
        if self._runner is not None:
            return await self.list_tools()
        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        self._runner = asyncio.create_task(
            self._run(self._ready), name=f"mcp-client:{self.spec.name}"
        )
        return await self._ready

    async def list_tools(self) -> tuple[McpToolInfo, ...]:
        return await self._request("list", None)

    async def _list_tools(self, session: ClientSession) -> tuple[McpToolInfo, ...]:
        cursor: str | None = None
        tools: list[McpToolInfo] = []
        while True:
            page = await session.list_tools(cursor=cursor)
            for remote in page.tools:
                annotations = getattr(remote, "annotations", None)
                tools.append(
                    McpToolInfo(
                        name=str(remote.name),
                        description=str(remote.description or ""),
                        input_schema=dict(remote.inputSchema or {"type": "object"}),
                        read_only=bool(
                            getattr(annotations, "readOnlyHint", False)
                            if annotations is not None
                            else False
                        ),
                    )
                )
            cursor = getattr(page, "nextCursor", None)
            if not cursor:
                break
        return tuple(tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpCallResult:
        return await self._request("call", (name, arguments))

    async def close(self) -> None:
        runner = self._runner
        if runner is None:
            return
        if not runner.done():
            try:
                await self._request("close", None)
            except Exception:
                pass
        await asyncio.gather(runner, return_exceptions=True)
        if self._runner is runner:
            self._runner = None

    async def _run(
        self, ready: asyncio.Future[tuple[McpToolInfo, ...]]
    ) -> None:
        stack = AsyncExitStack()
        try:
            read, write = await self._open_transport(stack)
            session = await stack.enter_async_context(
                ClientSession(
                    read,
                    write,
                    read_timeout_seconds=timedelta(seconds=self.spec.call_timeout),
                )
            )
            await asyncio.wait_for(session.initialize(), timeout=self.spec.connect_timeout)
            tools = await self._list_tools(session)
            ready.set_result(tools)
            while True:
                action, payload, future = await self._commands.get()
                if action == "close":
                    future.set_result(None)
                    break
                try:
                    if action == "list":
                        future.set_result(await self._list_tools(session))
                    elif action == "call":
                        name, arguments = payload
                        result = await session.call_tool(name, arguments)
                        content = [_jsonable(block) for block in result.content]
                        structured = getattr(result, "structuredContent", None)
                        if structured is not None:
                            content.append(
                                {"type": "structured", "data": _jsonable(structured)}
                            )
                        future.set_result(
                            McpCallResult(
                                is_error=bool(result.isError), content=content
                            )
                        )
                    else:
                        future.set_exception(RuntimeError(f"Unknown MCP action: {action}"))
                except Exception as exc:
                    future.set_exception(exc)
                    break
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
        finally:
            while not self._commands.empty():
                _, _, future = self._commands.get_nowait()
                if not future.done():
                    future.set_exception(RuntimeError("MCP client connection closed"))
            await stack.aclose()

    async def _request(self, action: str, payload: Any) -> Any:
        runner = self._runner
        if runner is None or runner.done():
            raise RuntimeError(f"MCP server is not connected: {self.spec.name}")
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await self._commands.put((action, payload, future))
        return await future

    async def _open_transport(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        if self.spec.transport == "stdio":
            inherited = dict(self.spec.resolved_env())
            params = StdioServerParameters(
                command=self.spec.command,
                args=list(self.spec.args),
                env=inherited or None,
                cwd=self.spec.cwd,
            )
            read, write = await stack.enter_async_context(stdio_client(params))
            return read, write

        http_client = httpx.AsyncClient(
            headers=self.spec.resolved_headers(),
            follow_redirects=False,
            timeout=self.spec.call_timeout,
        )
        await stack.enter_async_context(http_client)
        read, write, _ = await stack.enter_async_context(
            streamable_http_client(self.spec.url, http_client=http_client)
        )
        return read, write

def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)
