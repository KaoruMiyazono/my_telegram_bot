"""Ordered, durable turn events shared by every channel adapter."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from config.settings import settings
from agent.core.event_bus import EventBus, EventSubscription
from agent.core.types import AfterToolResultCtx, BeforeToolCallCtx

StreamEventType = Literal[
    "turn.started",
    "assistant.delta",
    "tool.started",
    "tool.completed",
    "tool.failed",
    "turn.completed",
    "turn.cancelled",
]
_TERMINAL_TYPES = {"turn.completed", "turn.cancelled"}
_CLOSED = object()


@dataclass(frozen=True)
class StreamEvent:
    event_id: str
    session_key: str
    turn_id: str
    trace_id: str
    seq: int
    event_type: StreamEventType
    payload: dict[str, Any]
    created_at: datetime

    @property
    def terminal(self) -> bool:
        return self.event_type in _TERMINAL_TYPES

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["created_at"] = self.created_at.isoformat()
        value["type"] = value.pop("event_type")
        return value


class StreamEventStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self._db = sqlite3.connect(str(path or settings.DATABASE_PATH))
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout = 5000")
        self._ensure_schema()

    def append(
        self,
        *,
        session_key: str,
        turn_id: str,
        trace_id: str,
        event_type: StreamEventType,
        payload: dict[str, Any] | None = None,
    ) -> StreamEvent:
        now = datetime.now(timezone.utc)
        self._db.execute("BEGIN IMMEDIATE")
        row = self._db.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM runtime_stream_events WHERE session_key=?",
            (session_key,),
        ).fetchone()
        seq = int(row[0]) + 1
        event = StreamEvent(
            event_id=f"event:{uuid4().hex}",
            session_key=session_key,
            turn_id=turn_id,
            trace_id=trace_id,
            seq=seq,
            event_type=event_type,
            payload=dict(payload or {}),
            created_at=now,
        )
        self._db.execute(
            """INSERT INTO runtime_stream_events(
                event_id, session_key, turn_id, trace_id, seq, event_type,
                payload_json, terminal, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.event_id,
                session_key,
                turn_id,
                trace_id,
                seq,
                event_type,
                json.dumps(event.payload, ensure_ascii=False),
                1 if event.terminal else 0,
                now.isoformat(),
            ),
        )
        self._db.commit()
        return event

    def replay(
        self, session_key: str, *, after_seq: int = 0, limit: int = 1000
    ) -> list[StreamEvent]:
        rows = self._db.execute(
            """SELECT * FROM runtime_stream_events
               WHERE session_key=? AND seq>? ORDER BY seq LIMIT ?""",
            (session_key, max(0, int(after_seq)), max(1, int(limit))),
        ).fetchall()
        return [_event_from_row(row) for row in rows]

    def turn_events(self, turn_id: str) -> list[StreamEvent]:
        rows = self._db.execute(
            "SELECT * FROM runtime_stream_events WHERE turn_id=? ORDER BY seq",
            (turn_id,),
        ).fetchall()
        return [_event_from_row(row) for row in rows]

    def terminal_result(self, turn_id: str) -> StreamEvent | None:
        row = self._db.execute(
            """SELECT * FROM runtime_stream_events
               WHERE turn_id=? AND terminal=1 ORDER BY seq DESC LIMIT 1""",
            (turn_id,),
        ).fetchone()
        return _event_from_row(row) if row is not None else None

    def acknowledge(
        self, *, channel: str, client_id: str, session_key: str, seq: int
    ) -> int:
        value = max(0, int(seq))
        self._db.execute(
            """INSERT INTO runtime_stream_acks(
                channel, client_id, session_key, acked_seq, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(channel, client_id, session_key) DO UPDATE SET
                acked_seq=MAX(runtime_stream_acks.acked_seq, excluded.acked_seq),
                updated_at=excluded.updated_at""",
            (channel, client_id, session_key, value, datetime.now(timezone.utc).isoformat()),
        )
        self._db.commit()
        return self.acked_seq(channel=channel, client_id=client_id, session_key=session_key)

    def acked_seq(self, *, channel: str, client_id: str, session_key: str) -> int:
        row = self._db.execute(
            """SELECT acked_seq FROM runtime_stream_acks
               WHERE channel=? AND client_id=? AND session_key=?""",
            (channel, client_id, session_key),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def close(self) -> None:
        self._db.close()

    def _ensure_schema(self) -> None:
        self._db.executescript(
            """CREATE TABLE IF NOT EXISTS runtime_stream_events (
                event_id TEXT PRIMARY KEY,
                session_key TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                terminal INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(session_key, seq)
            );
            CREATE INDEX IF NOT EXISTS idx_runtime_stream_turn
            ON runtime_stream_events(turn_id, seq);
            CREATE TABLE IF NOT EXISTS runtime_stream_acks (
                channel TEXT NOT NULL,
                client_id TEXT NOT NULL,
                session_key TEXT NOT NULL,
                acked_seq INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(channel, client_id, session_key)
            );"""
        )
        self._db.commit()


class StreamSubscription:
    def __init__(
        self,
        broker: "StreamEventBroker",
        session_key: str,
        queue: asyncio.Queue[StreamEvent | object],
        replay: list[StreamEvent],
    ) -> None:
        self._broker = broker
        self.session_key = session_key
        self._queue = queue
        self._replay = replay
        self._closed = False

    def __aiter__(self) -> "StreamSubscription":
        return self

    async def __anext__(self) -> StreamEvent:
        if self._replay:
            return self._replay.pop(0)
        item = await self._queue.get()
        if item is _CLOSED:
            raise StopAsyncIteration
        assert isinstance(item, StreamEvent)
        return item

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._broker.unsubscribe(self)


class StreamEventBroker:
    """Persist first, then non-blocking fanout; slow clients reconnect by seq."""

    def __init__(self, store: StreamEventStore, *, subscriber_queue_size: int = 128) -> None:
        self.store = store
        self._queue_size = max(1, int(subscriber_queue_size))
        self._subscribers: dict[str, set[StreamSubscription]] = {}
        self._lock = asyncio.Lock()
        self._active_turns: dict[str, tuple[str, str]] = {}

    async def publish(
        self,
        *,
        session_key: str,
        turn_id: str,
        trace_id: str,
        event_type: StreamEventType,
        payload: dict[str, Any] | None = None,
    ) -> StreamEvent:
        async with self._lock:
            event = self.store.append(
                session_key=session_key,
                turn_id=turn_id,
                trace_id=trace_id,
                event_type=event_type,
                payload=payload,
            )
            if event_type == "turn.started":
                self._active_turns[session_key] = (turn_id, trace_id)
            elif event_type in _TERMINAL_TYPES:
                self._active_turns.pop(session_key, None)
            overflowed: list[StreamSubscription] = []
            for subscription in self._subscribers.get(session_key, set()):
                try:
                    subscription._queue.put_nowait(event)
                except asyncio.QueueFull:
                    overflowed.append(subscription)
            for subscription in overflowed:
                self._drop_locked(subscription)
            return event

    def active_identity(self, session_key: str) -> tuple[str, str] | None:
        return self._active_turns.get(session_key)

    async def subscribe(
        self, session_key: str, *, after_seq: int = 0
    ) -> StreamSubscription:
        async with self._lock:
            replay = self.store.replay(session_key, after_seq=after_seq)
            subscription = StreamSubscription(
                self,
                session_key,
                asyncio.Queue(maxsize=self._queue_size),
                replay,
            )
            self._subscribers.setdefault(session_key, set()).add(subscription)
            return subscription

    async def unsubscribe(self, subscription: StreamSubscription) -> None:
        async with self._lock:
            self._drop_locked(subscription)

    def _drop_locked(self, subscription: StreamSubscription) -> None:
        subscribers = self._subscribers.get(subscription.session_key)
        if subscribers is not None:
            subscribers.discard(subscription)
            if not subscribers:
                self._subscribers.pop(subscription.session_key, None)
        try:
            subscription._queue.put_nowait(_CLOSED)
        except asyncio.QueueFull:
            try:
                subscription._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            subscription._queue.put_nowait(_CLOSED)


class ToolStreamBridge:
    """Project existing tool lifecycle taps into the active turn stream."""

    def __init__(self, broker: StreamEventBroker, event_bus: EventBus) -> None:
        self._broker = broker
        self._event_bus = event_bus
        self._subscriptions: list[EventSubscription] = []

    def start(self) -> None:
        if self._subscriptions:
            return
        self._subscriptions = [
            self._event_bus.observe(BeforeToolCallCtx, self._before_tool),
            self._event_bus.observe(AfterToolResultCtx, self._after_tool),
        ]

    def close(self) -> None:
        for subscription in self._subscriptions:
            self._event_bus.unsubscribe(subscription)
        self._subscriptions.clear()

    async def _before_tool(self, event: BeforeToolCallCtx) -> None:
        identity = self._broker.active_identity(event.session_key)
        if identity is None:
            return
        turn_id, trace_id = identity
        await self._broker.publish(
            session_key=event.session_key,
            turn_id=turn_id,
            trace_id=trace_id,
            event_type="tool.started",
            payload={"tool_name": event.tool_name},
        )

    async def _after_tool(self, event: AfterToolResultCtx) -> None:
        identity = self._broker.active_identity(event.session_key)
        if identity is None:
            return
        turn_id, trace_id = identity
        ok = event.status in {"success", "completed", "ok"}
        await self._broker.publish(
            session_key=event.session_key,
            turn_id=turn_id,
            trace_id=trace_id,
            event_type="tool.completed" if ok else "tool.failed",
            payload={"tool_name": event.tool_name, "status": event.status},
        )
def _event_from_row(row: sqlite3.Row) -> StreamEvent:
    event_type = str(row["event_type"])
    return StreamEvent(
        event_id=str(row["event_id"]),
        session_key=str(row["session_key"]),
        turn_id=str(row["turn_id"]),
        trace_id=str(row["trace_id"]),
        seq=int(row["seq"]),
        event_type=event_type,  # type: ignore[arg-type]
        payload=dict(json.loads(str(row["payload_json"]))),
        created_at=datetime.fromisoformat(str(row["created_at"])),
    )
