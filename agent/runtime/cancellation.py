"""Control-plane cancellation for active session turns."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InterruptResult:
    status: str
    session_key: str
    turn_id: str = ""


@dataclass
class _ActiveTurn:
    turn_id: str
    task: asyncio.Task[Any]


class CancellationRegistry:
    def __init__(self) -> None:
        self._active: dict[str, _ActiveTurn] = {}

    def register(self, session_key: str, turn_id: str, task: asyncio.Task[Any]) -> None:
        self._active[session_key] = _ActiveTurn(turn_id=turn_id, task=task)

    def unregister(self, session_key: str, task: asyncio.Task[Any]) -> None:
        active = self._active.get(session_key)
        if active is not None and active.task is task:
            self._active.pop(session_key, None)

    def interrupt(self, session_key: str) -> InterruptResult:
        active = self._active.get(session_key)
        if active is None or active.task.done():
            return InterruptResult(status="idle", session_key=session_key)
        active.task.cancel()
        return InterruptResult(
            status="interrupted", session_key=session_key, turn_id=active.turn_id
        )

    def interrupt_all(self) -> int:
        count = 0
        for active in list(self._active.values()):
            if not active.task.done():
                active.task.cancel()
                count += 1
        return count

    def is_active(self, session_key: str) -> bool:
        active = self._active.get(session_key)
        return bool(active is not None and not active.task.done())
