"""Coordinate passive turns, proactive delivery, and interruptible idle work."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol


class IdleController(Protocol):
    async def pause_for_passive(self) -> None: ...

    def resume_after_passive(self) -> None: ...


@dataclass(frozen=True)
class ModeTransition:
    mode: str
    action: str
    session_key: str
    trace_id: str
    timestamp: datetime


class ModeCoordinator:
    """A small priority arbiter: Passive > Proactive > Idle.

    State changes contain no awaits, so a proactive delivery and a passive turn
    cannot both acquire the same session in one event-loop interleaving.
    """

    def __init__(self) -> None:
        self._passive: dict[str, int] = defaultdict(int)
        self._proactive: set[str] = set()
        self._proactive_done: dict[str, asyncio.Event] = {}
        self._idle: IdleController | None = None
        self._proactive_waker: Callable[[], None] | None = None
        self._transitions: list[ModeTransition] = []

    def attach_idle(self, controller: IdleController) -> None:
        self._idle = controller

    def attach_proactive_waker(self, callback: Callable[[], None]) -> None:
        self._proactive_waker = callback

    async def enter_passive(self, session_key: str, *, trace_id: str = "") -> None:
        was_globally_idle = not self.has_passive_work
        self._passive[session_key] += 1
        self._record("passive", "waiting", session_key, trace_id)
        if was_globally_idle and self._idle is not None:
            await self._idle.pause_for_passive()
        event = self._proactive_done.get(session_key)
        if session_key in self._proactive and event is not None:
            await event.wait()
        self._record("passive", "started", session_key, trace_id)

    def exit_passive(self, session_key: str, *, trace_id: str = "") -> None:
        count = self._passive.get(session_key, 0)
        if count <= 1:
            self._passive.pop(session_key, None)
        else:
            self._passive[session_key] = count - 1
        self._record("passive", "finished", session_key, trace_id)
        if not self.has_passive_work:
            if self._idle is not None:
                self._idle.resume_after_passive()
            if self._proactive_waker is not None:
                self._proactive_waker()

    def try_enter_proactive(self, session_key: str, *, trace_id: str = "") -> bool:
        if self.is_passive_active(session_key) or session_key in self._proactive:
            self._record("proactive", "blocked", session_key, trace_id)
            return False
        self._proactive.add(session_key)
        self._proactive_done[session_key] = asyncio.Event()
        self._record("proactive", "started", session_key, trace_id)
        return True

    def exit_proactive(self, session_key: str, *, trace_id: str = "") -> None:
        self._proactive.discard(session_key)
        event = self._proactive_done.pop(session_key, None)
        if event is not None:
            event.set()
        self._record("proactive", "finished", session_key, trace_id)

    def is_passive_active(self, session_key: str) -> bool:
        return self._passive.get(session_key, 0) > 0

    @property
    def has_passive_work(self) -> bool:
        return any(count > 0 for count in self._passive.values())

    @property
    def idle_allowed(self) -> bool:
        return not self.has_passive_work and not self._proactive

    @property
    def transitions(self) -> tuple[ModeTransition, ...]:
        return tuple(self._transitions)

    def _record(self, mode: str, action: str, session_key: str, trace_id: str) -> None:
        self._transitions.append(
            ModeTransition(mode, action, session_key, trace_id, datetime.now(timezone.utc))
        )
        if len(self._transitions) > 1000:
            del self._transitions[:500]
