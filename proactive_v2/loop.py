from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from proactive_v2.agent_tick import AgentTick, ProactiveTickResult

logger = logging.getLogger(__name__)


class ProactiveLoop:
    """Small scheduler wrapper around AgentTick.

    The next Gate check controls the sleep interval, so blocked and idle ticks
    can use different schedules without changing the five-stage pipeline.
    """

    def __init__(self, agent_tick: AgentTick, *, interval_seconds: int = 300) -> None:
        self._agent_tick = agent_tick
        self._interval_seconds = max(1, int(interval_seconds))
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def run_once(self) -> ProactiveTickResult | None:
        return await self._agent_tick.tick()

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                result = await self.run_once()
            except Exception:
                logger.exception("ProactiveLoop tick failed")
                result = None
            delay = self._interval_seconds
            if result is not None and result.next_check_at is not None:
                delay = max(
                    1,
                    int((result.next_check_at - datetime.now(timezone.utc)).total_seconds()),
                )
            await asyncio.sleep(delay)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="proactive-loop")

    def stop(self) -> None:
        self._running = False

    async def close(self) -> None:
        self.stop()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._agent_tick.close()


def build_proactive_loop(**kwargs: Any) -> ProactiveLoop:
    return ProactiveLoop(**kwargs)
