"""M12 acceptance tests for four adapters and durable ordered streaming."""

from __future__ import annotations

import asyncio

import httpx

from agent.core.event_bus import EventBus
from agent.core.message_bus import MessageBus
from agent.core.types import AfterToolResultCtx, BeforeToolCallCtx, InboundMessage, OutboundMessage
from agent.runtime.stream_events import StreamEventBroker, StreamEventStore
from agent.runtime.turn_runtime import TurnRuntime
from channels.base import ChannelIdentityStore, ChannelRequest, RuntimeChannelAdapter
from channels.cli_adapter import CliAdapter
from channels.telegram.adapter import TelegramAdapter
from channels.web_gateway import WebGateway
from persistence.database import init_db
from persistence.runtime_message_store import RuntimeMessageStore
from persistence.session_store import SessionStore


async def _eventually(predicate, *, timeout: float = 2.0) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout=timeout)


class _Pipeline:
    def __init__(self, *, delay: float = 0.01) -> None:
        self.delay = delay

    async def execute(self, message: InboundMessage) -> OutboundMessage:
        await asyncio.sleep(self.delay)
        return OutboundMessage(
            chat_id=message.chat_id,
            content=f"answer:{message.content}",
            turn_id=message.turn_id,
            trace_id=message.trace_id,
        )


async def _runtime(tmp_path, *, delay: float = 0.01):
    init_db()
    stream_store = StreamEventStore(tmp_path / "stream.db")
    broker = StreamEventBroker(stream_store, subscriber_queue_size=8)
    bus = MessageBus(RuntimeMessageStore())
    event_bus = EventBus()
    runtime = TurnRuntime(
        bus=bus,
        pipeline=_Pipeline(delay=delay),  # type: ignore[arg-type]
        stream_broker=broker,
        event_bus=event_bus,
    )
    await runtime.start()
    return runtime, stream_store, broker, event_bus


def test_m12_session_key_has_channel_account_chat_and_thread(tmp_path) -> None:
    identities = ChannelIdentityStore(tmp_path / "identities.db")
    try:
        first = identities.resolve("http", "alice")
        second = identities.resolve("websocket", "alice")
        assert first != second
        assert first >= 8_000_000_000_000_000
        assert second >= 8_000_000_000_000_000

        identities.bind("websocket", "alice", first)
        assert identities.resolve("websocket", "alice") == first
    finally:
        identities.close()


def test_bound_identity_still_has_independent_channel_sessions() -> None:
    init_db()
    store = SessionStore()
    store.save(
        42,
        9,
        [{"role": "user", "content": "from http"}],
        session_key="http:alice:9:main",
        channel="http",
    )
    store.save(
        42,
        9,
        [{"role": "user", "content": "from websocket"}],
        session_key="websocket:alice:9:main",
        channel="websocket",
    )
    http_state = store.load_state(42, 9, session_key="http:alice:9:main")
    websocket_state = store.load_state(
        42, 9, session_key="websocket:alice:9:main"
    )
    assert http_state is not None and http_state[0][0]["content"] == "from http"
    assert (
        websocket_state is not None
        and websocket_state[0][0]["content"] == "from websocket"
    )


async def test_runtime_emits_ordered_started_delta_completed_and_replays(tmp_path) -> None:
    runtime, store, broker, _ = await _runtime(tmp_path)
    adapter = RuntimeChannelAdapter(runtime, ChannelIdentityStore(tmp_path / "ids.db"))

    async def delivered(_envelope) -> None:
        return None

    runtime.bus.subscribe_outbound("http", delivered)
    try:
        receipt = await adapter.submit(
            ChannelRequest("http", "alice", 10, "topic-7", "hello", 101, "m12-1")
        )
        assert receipt.session_key == "http:alice:10:topic-7"
        await _eventually(lambda: store.terminal_result(receipt.turn_id) is not None)
        events = store.turn_events(receipt.turn_id)
        assert [event.event_type for event in events] == [
            "turn.started",
            "assistant.delta",
            "turn.completed",
        ]
        assert [event.seq for event in events] == sorted(event.seq for event in events)
        assert events[-1].payload["content"] == "answer:hello"

        replayed = broker.store.replay(receipt.session_key, after_seq=events[0].seq)
        assert [event.event_type for event in replayed] == [
            "assistant.delta",
            "turn.completed",
        ]
    finally:
        adapter.identities.close()
        await runtime.stop()
        store.close()


async def test_tool_taps_become_stream_events_without_result_body(tmp_path) -> None:
    runtime, store, broker, event_bus = await _runtime(tmp_path, delay=0.1)

    async def delivered(_envelope) -> None:
        return None

    runtime.bus.subscribe_outbound("cli", delivered)
    identities = ChannelIdentityStore(tmp_path / "ids.db")
    adapter = RuntimeChannelAdapter(runtime, identities)
    receipt = await adapter.submit(
        ChannelRequest("cli", "local", 1, "main", "tool", 102, "m12-tool")
    )
    try:
        await _eventually(lambda: broker.active_identity(receipt.session_key) is not None)
        await event_bus.observe(
            BeforeToolCallCtx(receipt.session_key, "cli", "1", "web_search", {"q": "x"})
        )
        await event_bus.observe(
            AfterToolResultCtx(
                receipt.session_key,
                "cli",
                "1",
                "web_search",
                {"q": "x"},
                "secret tool body",
                "success",
            )
        )
        await _eventually(lambda: store.terminal_result(receipt.turn_id) is not None)
        events = store.turn_events(receipt.turn_id)
        assert "tool.started" in [event.event_type for event in events]
        assert "tool.completed" in [event.event_type for event in events]
        assert "secret tool body" not in str([event.payload for event in events])
    finally:
        identities.close()
        await runtime.stop()
        store.close()


async def test_slow_subscriber_does_not_block_publish_or_other_session(tmp_path) -> None:
    store = StreamEventStore(tmp_path / "stream.db")
    broker = StreamEventBroker(store, subscriber_queue_size=1)
    slow = await broker.subscribe("session:slow")
    fast = await broker.subscribe("session:fast")
    received = []

    async def consume_fast() -> None:
        async for event in fast:
            received.append(event.seq)
            if len(received) == 3:
                return

    consumer = asyncio.create_task(consume_fast())
    try:
        for index in range(3):
            await broker.publish(
                session_key="session:slow",
                turn_id="turn:slow",
                trace_id="trace:slow",
                event_type="assistant.delta",
                payload={"delta": str(index)},
            )
            await broker.publish(
                session_key="session:fast",
                turn_id="turn:fast",
                trace_id="trace:fast",
                event_type="assistant.delta",
                payload={"delta": str(index)},
            )
            await asyncio.sleep(0)
        await asyncio.wait_for(consumer, timeout=1)
        assert received == [1, 2, 3]
        assert len(store.replay("session:slow")) == 3
    finally:
        consumer.cancel()
        await slow.close()
        await fast.close()
        store.close()


def test_client_ack_is_monotonic_and_survives_restart(tmp_path) -> None:
    path = tmp_path / "stream.db"
    first = StreamEventStore(path)
    assert first.acknowledge(
        channel="http", client_id="browser-1", session_key="http:a:1:main", seq=8
    ) == 8
    assert first.acknowledge(
        channel="http", client_id="browser-1", session_key="http:a:1:main", seq=3
    ) == 8
    first.close()
    restarted = StreamEventStore(path)
    try:
        assert restarted.acked_seq(
            channel="http", client_id="browser-1", session_key="http:a:1:main"
        ) == 8
    finally:
        restarted.close()


async def test_cancel_emits_terminal_cancelled_event(tmp_path) -> None:
    runtime, store, _, _ = await _runtime(tmp_path, delay=10)
    identities = ChannelIdentityStore(tmp_path / "ids.db")
    adapter = RuntimeChannelAdapter(runtime, identities)

    async def delivered(_envelope) -> None:
        return None

    runtime.bus.subscribe_outbound("websocket", delivered)
    request = ChannelRequest(
        "websocket", "tab-1", 5, "main", "long", 103, "m12-long"
    )
    receipt = await adapter.submit(request)
    try:
        await _eventually(lambda: store.turn_events(receipt.turn_id))
        await adapter.cancel(
            ChannelRequest(
                "websocket", "tab-1", 5, "main", "/stop", 103, "m12-stop"
            )
        )
        await _eventually(lambda: store.terminal_result(receipt.turn_id) is not None)
        assert store.terminal_result(receipt.turn_id).event_type == "turn.cancelled"  # type: ignore[union-attr]
    finally:
        identities.close()
        await runtime.stop()
        store.close()


async def test_four_terminal_surfaces_share_one_turn_runtime(tmp_path) -> None:
    runtime, store, broker, _ = await _runtime(tmp_path)
    identities = ChannelIdentityStore(tmp_path / "ids.db")
    telegram = TelegramAdapter("test", runtime=runtime, identities=identities)
    cli = CliAdapter(runtime, identities, broker)
    web = WebGateway(runtime, identities, broker, api_token="secret")
    try:
        assert telegram.runtime is runtime
        assert cli.runtime is runtime
        assert web.http.runtime is runtime
        assert web.websocket.runtime is runtime
        route_paths = {getattr(route, "path", "") for route in web.app.routes}
        assert "/v1/ws" in route_paths
        assert "/v1/events" in route_paths
    finally:
        identities.close()
        await runtime.stop()
        store.close()


async def test_http_submit_result_and_explicit_identity_binding(tmp_path) -> None:
    runtime, store, broker, _ = await _runtime(tmp_path)
    identities = ChannelIdentityStore(tmp_path / "ids.db")
    gateway = WebGateway(runtime, identities, broker, api_token="secret")
    transport = httpx.ASGITransport(app=gateway.app)
    headers = {"Authorization": "Bearer secret"}
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            bound = await client.post(
                "/v1/identities/bind",
                headers=headers,
                json={"channel": "http", "account_id": "alice", "user_id": 42},
            )
            assert bound.json()["user_id"] == 42
            response = await client.post(
                "/v1/chat",
                headers=headers,
                json={
                    "account_id": "alice",
                    "chat_id": 9,
                    "thread_id": "topic",
                    "content": "hi",
                    "client_message_id": "http-m12",
                },
            )
            assert response.status_code == 202
            receipt = response.json()
            assert receipt["session_key"] == "http:alice:9:topic"
            await _eventually(lambda: store.terminal_result(receipt["turn_id"]) is not None)
            result = await client.get(
                f"/v1/result/{receipt['turn_id']}", headers=headers
            )
            assert result.json()["payload"]["content"] == "answer:hi"
            events = await client.get(
                "/v1/events",
                headers=headers,
                params={
                    "session_key": receipt["session_key"],
                    "after_seq": 0,
                    "client_id": "browser-1",
                },
            )
            assert events.headers["content-type"].startswith("text/event-stream")
            assert "event: turn.completed" in events.text
            await asyncio.sleep(0.01)
            terminal = store.terminal_result(receipt["turn_id"])
            assert terminal is not None
            ack = await client.post(
                "/v1/events/ack",
                headers=headers,
                json={
                    "client_id": "browser-1",
                    "session_key": receipt["session_key"],
                    "seq": terminal.seq,
                },
            )
            assert ack.json()["acked_seq"] == terminal.seq
    finally:
        identities.close()
        await runtime.stop()
        store.close()
