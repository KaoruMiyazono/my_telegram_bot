"""Async runtime wrapper around the existing five-phase passive pipeline."""

from __future__ import annotations

import asyncio
import logging

from agent.core.envelope import (
    MessageEnvelope,
    MessagePriority,
    envelope_from_outbound,
)
from agent.core.message_bus import MessageBus
from agent.core.session_lane import SessionLaneManager
from agent.core.types import OutboundMessage
from agent.pipeline.passive_turn import PassiveTurnPipeline
from agent.runtime.cancellation import CancellationRegistry, InterruptResult
from agent.runtime.mode_coordinator import ModeCoordinator
from agent.runtime.stream_events import StreamEventBroker, ToolStreamBridge
from agent.core.event_bus import EventBus

logger = logging.getLogger(__name__)


class TurnRuntime:
    """Consumes the bus, schedules session lanes, and owns turn cancellation."""

    def __init__(
        self,
        *,
        bus: MessageBus,
        pipeline: PassiveTurnPipeline,
        cancellation: CancellationRegistry | None = None,
        mode_coordinator: ModeCoordinator | None = None,
        stream_broker: StreamEventBroker | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self.bus = bus
        self.pipeline = pipeline
        self.cancellation = cancellation or CancellationRegistry()
        self.mode_coordinator = mode_coordinator
        self.stream_broker = stream_broker
        self._tool_stream_bridge = (
            ToolStreamBridge(stream_broker, event_bus or EventBus.get_instance())
            if stream_broker is not None
            else None
        )
        self.lanes = SessionLaneManager(
            self._execute,
            self.cancellation,
            on_cancelled=self._mark_cancelled,
        )
        self._consumer: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        self._stopping = False
        await self.bus.start()
        if self._tool_stream_bridge is not None:
            self._tool_stream_bridge.start()
        if self._consumer is None or self._consumer.done():
            self._consumer = asyncio.create_task(self._consume(), name="turn-runtime")

    async def stop(self) -> None:
        self._stopping = True
        if self._consumer is not None:
            self._consumer.cancel()
            try:
                await self._consumer
            except asyncio.CancelledError:
                pass
            self._consumer = None
        await self.lanes.close()
        await self.bus.stop()
        if self._tool_stream_bridge is not None:
            self._tool_stream_bridge.close()

    def interrupt(self, session_key: str) -> InterruptResult:
        return self.cancellation.interrupt(session_key)

    async def _consume(self) -> None:
        while not self._stopping:
            envelope = await self.bus.consume_inbound()
            content = str(envelope.payload.get("content") or "").strip().lower()
            if envelope.priority == MessagePriority.INTERRUPT or content == "/stop":
                await self._handle_interrupt(envelope)
                continue
            metadata = dict(envelope.payload.get("metadata") or {})
            await self.lanes.submit(
                envelope,
                preempt_active=bool(metadata.get("preempt_active", False)),
            )

    async def _execute(self, envelope: MessageEnvelope) -> object:
        self.bus.store.mark(envelope.message_id, "running", increment_attempts=True)
        trace_id = str(envelope.payload.get("trace_id") or envelope.message_id)
        turn_id = str(envelope.payload.get("turn_id") or envelope.message_id)
        if self.stream_broker is not None:
            await self.stream_broker.publish(
                session_key=envelope.session_key,
                turn_id=turn_id,
                trace_id=trace_id,
                event_type="turn.started",
                payload={"channel": envelope.channel},
            )
        if self.mode_coordinator is not None:
            await self.mode_coordinator.enter_passive(
                envelope.session_key, trace_id=trace_id
            )
        try:
            outbound = await self.pipeline.execute(envelope.as_inbound())
            if self.stream_broker is not None:
                for delta in _content_chunks(outbound.content):
                    await self.stream_broker.publish(
                        session_key=envelope.session_key,
                        turn_id=turn_id,
                        trace_id=trace_id,
                        event_type="assistant.delta",
                        payload={"delta": delta},
                    )
            delivered = await self.bus.publish_outbound_and_wait(
                envelope_from_outbound(outbound, inbound=envelope)
            )
            if not delivered:
                raise RuntimeError("outbound delivery failed")
            if self.stream_broker is not None:
                await self.stream_broker.publish(
                    session_key=envelope.session_key,
                    turn_id=turn_id,
                    trace_id=trace_id,
                    event_type="turn.completed",
                    payload={"status": "completed", "content": outbound.content},
                )
        except asyncio.CancelledError:
            self.bus.store.mark(envelope.message_id, "cancelled")
            if self.stream_broker is not None:
                await self.stream_broker.publish(
                    session_key=envelope.session_key,
                    turn_id=turn_id,
                    trace_id=trace_id,
                    event_type="turn.cancelled",
                    payload={"status": "cancelled"},
                )
            raise
        except Exception as error:
            self.bus.store.mark(envelope.message_id, "failed")
            if self.stream_broker is not None:
                await self.stream_broker.publish(
                    session_key=envelope.session_key,
                    turn_id=turn_id,
                    trace_id=trace_id,
                    event_type="turn.completed",
                    payload={
                        "status": "failed",
                        "error_type": type(error).__name__,
                    },
                )
            raise
        else:
            self.bus.store.mark(envelope.message_id, "done")
        finally:
            if self.mode_coordinator is not None:
                self.mode_coordinator.exit_passive(
                    envelope.session_key, trace_id=trace_id
                )
        return outbound

    async def _handle_interrupt(self, envelope: MessageEnvelope) -> None:
        result = self.interrupt(envelope.session_key)
        self.bus.store.mark(envelope.message_id, "done", increment_attempts=True)
        content = "已停止当前任务。" if result.status == "interrupted" else "当前没有正在执行的任务。"
        reply = OutboundMessage(chat_id=envelope.chat_id, content=content)
        await self.bus.publish_outbound(envelope_from_outbound(reply, inbound=envelope))

    def _mark_cancelled(self, envelope: MessageEnvelope) -> None:
        if self.bus.store.status(envelope.message_id) not in {"done", "failed", "cancelled"}:
            self.bus.store.mark(envelope.message_id, "cancelled")


def _content_chunks(content: str, size: int = 160) -> list[str]:
    value = str(content or "")
    return [value[index : index + size] for index in range(0, len(value), size)] or [""]
