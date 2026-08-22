"""Durable asynchronous transport between channels and TurnRuntime."""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Awaitable, Callable

from agent.core.envelope import MessageEnvelope
from persistence.runtime_message_store import RuntimeMessageStore

logger = logging.getLogger(__name__)
EnvelopeHandler = Callable[[MessageEnvelope], Awaitable[None]]


class MessageBus:
    """Priority queues with durable admission and channel-specific delivery."""

    def __init__(self, store: RuntimeMessageStore | None = None) -> None:
        self.store = store or RuntimeMessageStore()
        self._inbound: asyncio.PriorityQueue[tuple[int, int, MessageEnvelope]] = asyncio.PriorityQueue()
        self._outbound: asyncio.PriorityQueue[tuple[int, int, MessageEnvelope]] = asyncio.PriorityQueue()
        self._sequence = itertools.count()
        self._subscribers: dict[str, list[EnvelopeHandler]] = {}
        self._dispatch_task: asyncio.Task[None] | None = None
        self._stopping = False

    async def publish_inbound(self, envelope: MessageEnvelope) -> bool:
        if envelope.direction != "inbound":
            raise ValueError("publish_inbound requires an inbound envelope")
        if not self.store.admit(envelope):
            return False
        await self._put(self._inbound, envelope)
        return True

    async def publish_outbound(self, envelope: MessageEnvelope) -> bool:
        if envelope.direction != "outbound":
            raise ValueError("publish_outbound requires an outbound envelope")
        if not self.store.admit(envelope):
            return False
        await self._put(self._outbound, envelope)
        return True

    async def consume_inbound(self) -> MessageEnvelope:
        _, _, envelope = await self._inbound.get()
        return envelope

    def subscribe_outbound(self, channel: str, callback: EnvelopeHandler) -> None:
        self._subscribers.setdefault(channel, []).append(callback)

    async def recover(self) -> int:
        envelopes = self.store.recover_inbound()
        for envelope in envelopes:
            await self._put(self._inbound, envelope)
        return len(envelopes)

    async def start(self) -> None:
        self._stopping = False
        await self.recover()
        if self._dispatch_task is None or self._dispatch_task.done():
            self._dispatch_task = asyncio.create_task(
                self._dispatch_outbound(), name="message-bus-outbound"
            )

    async def stop(self) -> None:
        self._stopping = True
        if self._dispatch_task is not None:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
            self._dispatch_task = None

    async def _put(
        self,
        queue: asyncio.PriorityQueue[tuple[int, int, MessageEnvelope]],
        envelope: MessageEnvelope,
    ) -> None:
        await queue.put((int(envelope.priority), next(self._sequence), envelope))

    async def _dispatch_outbound(self) -> None:
        while not self._stopping:
            _, _, envelope = await self._outbound.get()
            handlers = self._subscribers.get(envelope.channel, [])
            if not handlers:
                logger.error("No outbound subscriber for channel=%s", envelope.channel)
                self.store.mark(envelope.message_id, "failed", increment_attempts=True)
                continue
            self.store.mark(envelope.message_id, "running", increment_attempts=True)
            try:
                for handler in handlers:
                    await handler(envelope)
            except asyncio.CancelledError:
                self.store.mark(envelope.message_id, "queued")
                raise
            except Exception:
                logger.exception("Outbound delivery failed message_id=%s", envelope.message_id)
                self.store.mark(envelope.message_id, "failed")
            else:
                self.store.mark(envelope.message_id, "done")

    @property
    def inbound_size(self) -> int:
        return self._inbound.qsize()

    @property
    def outbound_size(self) -> int:
        return self._outbound.qsize()
