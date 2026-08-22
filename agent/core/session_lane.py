"""FIFO session lanes: serial within one session, concurrent across sessions."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from agent.core.envelope import MessageEnvelope
from agent.runtime.cancellation import CancellationRegistry

logger = logging.getLogger(__name__)
TurnHandler = Callable[[MessageEnvelope], Coroutine[Any, Any, Any]]
CancelHandler = Callable[[MessageEnvelope], None]


@dataclass
class _Lane:
    queue: asyncio.Queue[MessageEnvelope] = field(default_factory=asyncio.Queue)
    worker: asyncio.Task[None] | None = None


class SessionLaneManager:
    def __init__(
        self,
        handler: TurnHandler,
        cancellation: CancellationRegistry,
        *,
        on_cancelled: CancelHandler | None = None,
    ) -> None:
        self._handler = handler
        self._cancellation = cancellation
        self._on_cancelled = on_cancelled
        self._lanes: dict[str, _Lane] = {}
        self._closing = False

    async def submit(self, envelope: MessageEnvelope, *, preempt_active: bool = False) -> None:
        if self._closing:
            raise RuntimeError("session lane manager is closing")
        if preempt_active:
            self._cancellation.interrupt(envelope.session_key)
        lane = self._lanes.setdefault(envelope.session_key, _Lane())
        await lane.queue.put(envelope)
        if lane.worker is None or lane.worker.done():
            lane.worker = asyncio.create_task(
                self._run_lane(envelope.session_key, lane),
                name=f"session-lane:{envelope.session_key}",
            )

    async def _run_lane(self, session_key: str, lane: _Lane) -> None:
        try:
            while not self._closing:
                try:
                    envelope = lane.queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                task = asyncio.create_task(
                    self._handler(envelope), name=f"turn:{envelope.message_id}"
                )
                turn_id = str(envelope.payload.get("turn_id") or envelope.message_id)
                self._cancellation.register(session_key, turn_id, task)
                try:
                    await task
                except asyncio.CancelledError:
                    if self._on_cancelled is not None:
                        self._on_cancelled(envelope)
                except Exception:
                    logger.exception("Session turn failed session_key=%s", session_key)
                finally:
                    self._cancellation.unregister(session_key, task)
                    lane.queue.task_done()
        finally:
            if lane.queue.empty() and self._lanes.get(session_key) is lane:
                self._lanes.pop(session_key, None)

    async def close(self) -> None:
        self._closing = True
        self._cancellation.interrupt_all()
        workers = [lane.worker for lane in self._lanes.values() if lane.worker is not None]
        for lane in self._lanes.values():
            while not lane.queue.empty():
                envelope = lane.queue.get_nowait()
                if self._on_cancelled is not None:
                    self._on_cancelled(envelope)
                lane.queue.task_done()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self._lanes.clear()

    @property
    def lane_count(self) -> int:
        return len(self._lanes)
