"""M1 acceptance tests for MessageBus, SessionLane, cancellation and recovery."""

from __future__ import annotations

import asyncio
from collections import defaultdict

from agent.core.envelope import MessagePriority, envelope_from_inbound
from agent.core.message_bus import MessageBus
from agent.core.types import InboundMessage, OutboundMessage
from agent.runtime.turn_runtime import TurnRuntime
from persistence.database import init_db
from persistence.runtime_message_store import RuntimeMessageStore


async def _eventually(predicate, *, timeout: float = 3.0) -> None:
    async def _wait() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_wait(), timeout=timeout)


class _RecordingPipeline:
    def __init__(self, *, delay: float = 0.005) -> None:
        self.delay = delay
        self.started: list[tuple[int, str]] = []
        self.completed: list[tuple[int, str]] = []
        self.active = 0
        self.max_active = 0
        self.by_chat: dict[int, list[str]] = defaultdict(list)

    async def execute(self, message: InboundMessage) -> OutboundMessage:
        self.started.append((message.chat_id, message.content))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            self.completed.append((message.chat_id, message.content))
            self.by_chat[message.chat_id].append(message.content)
            return OutboundMessage(
                chat_id=message.chat_id,
                content=f"reply:{message.content}",
                turn_id=message.turn_id,
                trace_id=message.trace_id,
            )
        finally:
            self.active -= 1


async def _runtime(pipeline: _RecordingPipeline):
    init_db()
    delivered = []
    store = RuntimeMessageStore()
    bus = MessageBus(store)
    runtime = TurnRuntime(bus=bus, pipeline=pipeline)  # type: ignore[arg-type]

    async def capture(envelope):
        delivered.append(envelope)

    bus.subscribe_outbound("telegram", capture)
    await runtime.start()
    return runtime, store, delivered


async def test_same_session_twenty_messages_are_strictly_ordered() -> None:
    pipeline = _RecordingPipeline()
    runtime, store, delivered = await _runtime(pipeline)
    envelopes = []
    try:
        for index in range(20):
            envelope = envelope_from_inbound(
                InboundMessage(42, 1001, f"m{index}"),
                client_message_id=f"same-{index}",
            )
            envelopes.append(envelope)
            assert await runtime.bus.publish_inbound(envelope)

        await _eventually(lambda: len(delivered) == 20)
        assert pipeline.max_active == 1
        assert [content for _, content in pipeline.completed] == [f"m{i}" for i in range(20)]
        assert all(store.status(item.message_id) == "done" for item in envelopes)
    finally:
        await runtime.stop()


async def test_ten_sessions_run_concurrently_without_history_mixing() -> None:
    pipeline = _RecordingPipeline(delay=0.03)
    runtime, _, delivered = await _runtime(pipeline)
    try:
        for chat_id in range(10):
            for index in range(2):
                envelope = envelope_from_inbound(
                    InboundMessage(chat_id + 1, 2000 + chat_id, f"s{chat_id}-m{index}"),
                    client_message_id=f"cross-{chat_id}-{index}",
                )
                assert await runtime.bus.publish_inbound(envelope)

        await _eventually(lambda: len(delivered) == 20)
        assert pipeline.max_active >= 2
        for chat_id in range(10):
            assert pipeline.by_chat[2000 + chat_id] == [f"s{chat_id}-m0", f"s{chat_id}-m1"]
    finally:
        await runtime.stop()


async def test_interrupt_cancels_old_turn_and_suppresses_stale_reply() -> None:
    pipeline = _RecordingPipeline(delay=10.0)
    runtime, store, delivered = await _runtime(pipeline)
    old = envelope_from_inbound(
        InboundMessage(7, 77, "long task"), client_message_id="long-task"
    )
    stop = envelope_from_inbound(
        InboundMessage(7, 77, "/stop"),
        client_message_id="stop-task",
        priority=MessagePriority.INTERRUPT,
    )
    try:
        assert await runtime.bus.publish_inbound(old)
        await _eventually(lambda: runtime.cancellation.is_active(old.session_key))
        assert await runtime.bus.publish_inbound(stop)
        await _eventually(lambda: store.status(old.message_id) == "cancelled")
        await _eventually(lambda: len(delivered) == 1)
        assert delivered[0].as_outbound().content == "已停止当前任务。"
        assert all(item.as_outbound().content != "reply:long task" for item in delivered)
    finally:
        await runtime.stop()


async def test_new_message_can_preempt_active_turn() -> None:
    pipeline = _RecordingPipeline(delay=0.2)
    runtime, store, delivered = await _runtime(pipeline)
    old = envelope_from_inbound(
        InboundMessage(8, 88, "old"), client_message_id="preempt-old"
    )
    new = envelope_from_inbound(
        InboundMessage(8, 88, "new", metadata={"preempt_active": True}),
        client_message_id="preempt-new",
    )
    try:
        assert await runtime.bus.publish_inbound(old)
        await _eventually(lambda: runtime.cancellation.is_active(old.session_key))
        assert await runtime.bus.publish_inbound(new)
        await _eventually(lambda: len(delivered) == 1)
        assert store.status(old.message_id) == "cancelled"
        assert delivered[0].as_outbound().content == "reply:new"
    finally:
        await runtime.stop()


async def test_dedupe_and_recovery_do_not_reconsume_committed_message() -> None:
    init_db()
    store = RuntimeMessageStore()
    first_bus = MessageBus(store)
    done = envelope_from_inbound(
        InboundMessage(9, 99, "done"), client_message_id="stable-client-id"
    )
    queued = envelope_from_inbound(
        InboundMessage(9, 100, "queued"), client_message_id="recover-me"
    )
    assert await first_bus.publish_inbound(done)
    store.mark(done.message_id, "done")
    assert not await first_bus.publish_inbound(
        envelope_from_inbound(
            InboundMessage(9, 99, "duplicate"), client_message_id="stable-client-id"
        )
    )
    assert await first_bus.publish_inbound(queued)

    restarted = MessageBus(store)
    recovered = await restarted.recover()
    assert recovered == 1
    recovered_envelope = await asyncio.wait_for(restarted.consume_inbound(), timeout=0.2)
    assert recovered_envelope.message_id == queued.message_id
    assert store.status(done.message_id) == "done"
