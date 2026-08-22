from __future__ import annotations

import asyncio
import logging
from typing import Any

from proactive_v2.agent_tick import AgentTick, ProactiveTickResult

logger = logging.getLogger(__name__)


class ProactiveLoop:
    """Small scheduler wrapper around AgentTick.

    It is not started by `main.py` yet. That keeps the passive Telegram bot
    behavior unchanged while making the proactive chain testable.
    """

    def __init__(self, agent_tick: AgentTick, *, interval_seconds: int = 300) -> None:
        self._agent_tick = agent_tick
        self._interval_seconds = max(1, int(interval_seconds))
        self._running = False

    async def run_once(self) -> ProactiveTickResult | None:
        return await self._agent_tick.tick()

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await self.run_once()
            except Exception:
                logger.exception("ProactiveLoop tick failed")
            await asyncio.sleep(self._interval_seconds)

    def stop(self) -> None:
        self._running = False


def build_proactive_loop(**kwargs: Any) -> ProactiveLoop:
    return ProactiveLoop(**kwargs)
