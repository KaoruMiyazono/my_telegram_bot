"""HTTP/SSE and WebSocket adapters backed by one shared TurnRuntime."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.endpoints import WebSocketEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from agent.core.envelope import MessageEnvelope
from agent.runtime.stream_events import StreamEventBroker
from channels.base import (
    ChannelIdentityStore,
    ChannelRequest,
    RuntimeChannelAdapter,
)


class WebGateway:
    def __init__(
        self,
        runtime,
        identities: ChannelIdentityStore,
        broker: StreamEventBroker,
        *,
        api_token: str = "",
    ) -> None:
        self.runtime = runtime
        self.identities = identities
        self.broker = broker
        self.api_token = api_token
        self.http = RuntimeChannelAdapter(runtime, identities)
        self.websocket = RuntimeChannelAdapter(runtime, identities)
        runtime.bus.subscribe_outbound("http", self.send_envelope)
        runtime.bus.subscribe_outbound("websocket", self.send_envelope)
        self.app = Starlette(
            routes=[
                Route("/health", self.health, methods=["GET"]),
                Route("/v1/chat", self.chat, methods=["POST"]),
                Route("/v1/chat/cancel", self.cancel, methods=["POST"]),
                Route("/v1/events", self.events, methods=["GET"]),
                Route("/v1/events/ack", self.ack, methods=["POST"]),
                Route("/v1/result/{turn_id:str}", self.result, methods=["GET"]),
                Route("/v1/identities/bind", self.bind_identity, methods=["POST"]),
                WebSocketRoute("/v1/ws", _GatewayWebSocket, name="gateway-ws"),
            ]
        )
        self.app.state.gateway = self

    async def send_envelope(self, _envelope: MessageEnvelope) -> None:
        """HTTP/WS clients consume the durable event stream as delivery."""

    def authorized(self, headers: Any, query_token: str = "") -> bool:
        if not self.api_token:
            return True
        authorization = str(headers.get("authorization") or "")
        return authorization == f"Bearer {self.api_token}" or query_token == self.api_token

    async def health(self, _request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def chat(self, request: Request) -> JSONResponse:
        if not self.authorized(request.headers):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        payload = await request.json()
        channel_request = self._request(payload, channel="http")
        receipt = await self.http.submit(channel_request)
        return JSONResponse(_receipt_dict(receipt), status_code=202 if receipt.accepted else 409)

    async def cancel(self, request: Request) -> JSONResponse:
        if not self.authorized(request.headers):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        payload = await request.json()
        receipt = await self.http.cancel(self._request(payload, channel="http"))
        return JSONResponse(_receipt_dict(receipt), status_code=202 if receipt.accepted else 409)

    async def events(self, request: Request):
        if not self.authorized(request.headers, request.query_params.get("token", "")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        session_key = request.query_params.get("session_key", "")
        if not session_key:
            return JSONResponse({"error": "session_key_required"}, status_code=400)
        client_id = request.query_params.get("client_id", "")
        requested = int(request.query_params.get("after_seq", "0") or 0)
        acknowledged = (
            self.broker.store.acked_seq(
                channel="http", client_id=client_id, session_key=session_key
            )
            if client_id
            else 0
        )
        subscription = await self.broker.subscribe(
            session_key, after_seq=max(requested, acknowledged)
        )

        async def generate():
            try:
                async for event in subscription:
                    data = json.dumps(event.to_dict(), ensure_ascii=False)
                    yield f"id: {event.seq}\nevent: {event.event_type}\ndata: {data}\n\n"
                    if event.terminal:
                        break
            finally:
                await subscription.close()

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def ack(self, request: Request) -> JSONResponse:
        if not self.authorized(request.headers):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        payload = await request.json()
        seq = self.broker.store.acknowledge(
            channel="http",
            client_id=str(payload["client_id"]),
            session_key=str(payload["session_key"]),
            seq=int(payload["seq"]),
        )
        return JSONResponse({"acked_seq": seq})

    async def result(self, request: Request) -> JSONResponse:
        if not self.authorized(request.headers):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        event = self.broker.store.terminal_result(str(request.path_params["turn_id"]))
        if event is None:
            return JSONResponse({"status": "pending"}, status_code=202)
        return JSONResponse(event.to_dict())

    async def bind_identity(self, request: Request) -> JSONResponse:
        if not self.authorized(request.headers):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        payload = await request.json()
        user_id = self.identities.bind(
            str(payload["channel"]), str(payload["account_id"]), int(payload["user_id"])
        )
        return JSONResponse({"user_id": user_id, "explicit_binding": True})

    def _request(self, payload: dict[str, Any], *, channel: str) -> ChannelRequest:
        account_id = str(payload.get("account_id") or "anonymous")
        user_id = self.identities.resolve(channel, account_id)
        return ChannelRequest(
            channel=channel,
            account_id=account_id,
            chat_id=int(payload.get("chat_id") or 1),
            thread_id=str(payload.get("thread_id") or "main"),
            content=str(payload.get("content") or ""),
            user_id=user_id,
            client_message_id=(
                str(payload["client_message_id"])
                if payload.get("client_message_id") is not None
                else None
            ),
            metadata=dict(payload.get("metadata") or {}),
        )


class _GatewayWebSocket(WebSocketEndpoint):
    encoding = "json"

    async def on_connect(self, websocket: WebSocket) -> None:
        self._turn_tasks: set[asyncio.Task[None]] = set()
        self._send_lock = asyncio.Lock()
        gateway: WebGateway = websocket.app.state.gateway
        if not gateway.authorized(websocket.headers, websocket.query_params.get("token", "")):
            await websocket.close(code=4401)
            return
        await websocket.accept()

    async def on_receive(self, websocket: WebSocket, data: Any) -> None:
        gateway: WebGateway = websocket.app.state.gateway
        payload = dict(data or {})
        action = str(payload.pop("action", "send"))
        if action == "ack":
            seq = gateway.broker.store.acknowledge(
                channel="websocket",
                client_id=str(payload["client_id"]),
                session_key=str(payload["session_key"]),
                seq=int(payload["seq"]),
            )
            async with self._send_lock:
                await websocket.send_json({"type": "client.acked", "acked_seq": seq})
            return
        request = gateway._request(payload, channel="websocket")
        receipt = (
            await gateway.websocket.cancel(request)
            if action == "cancel"
            else await gateway.websocket.submit(request)
        )
        async with self._send_lock:
            await websocket.send_json({"type": "turn.accepted", **_receipt_dict(receipt)})
        if not receipt.accepted or action == "cancel":
            return
        task = asyncio.create_task(
            self._stream_turn(websocket, gateway, receipt),
            name=f"websocket-stream:{receipt.turn_id}",
        )
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)

    async def on_disconnect(self, _websocket: WebSocket, _close_code: int) -> None:
        for task in self._turn_tasks:
            task.cancel()
        if self._turn_tasks:
            await asyncio.gather(*self._turn_tasks, return_exceptions=True)
        self._turn_tasks.clear()

    async def _stream_turn(self, websocket: WebSocket, gateway: WebGateway, receipt) -> None:
        subscription = await gateway.broker.subscribe(receipt.session_key)
        try:
            async for event in subscription:
                if event.turn_id != receipt.turn_id:
                    continue
                async with self._send_lock:
                    await websocket.send_json(event.to_dict())
                if event.terminal:
                    break
        finally:
            await subscription.close()


class WebGatewayServer:
    def __init__(self, gateway: WebGateway, *, host: str, port: int) -> None:
        if host not in {"127.0.0.1", "localhost", "::1"} and not gateway.api_token:
            raise ValueError("CHANNEL_API_TOKEN is required for a non-loopback web gateway")
        self._server = uvicorn.Server(
            uvicorn.Config(gateway.app, host=host, port=int(port), log_level="info")
        )
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._server.serve(), name="web-gateway")

    async def stop(self) -> None:
        self._server.should_exit = True
        if self._task is not None:
            await self._task
        self._task = None


def _receipt_dict(receipt) -> dict[str, Any]:
    return {
        "accepted": receipt.accepted,
        "message_id": receipt.message_id,
        "turn_id": receipt.turn_id,
        "trace_id": receipt.trace_id,
        "session_key": receipt.session_key,
    }
