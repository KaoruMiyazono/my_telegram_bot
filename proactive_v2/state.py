from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from config.settings import settings
from proactive_v2.contracts import AgentTickContext, ProactiveTickResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proactive_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    delivery_key TEXT NOT NULL,
    message TEXT NOT NULL,
    priority TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    UNIQUE(session_key, delivery_key)
);
CREATE INDEX IF NOT EXISTS idx_proactive_delivery_session_time
ON proactive_deliveries(session_key, sent_at);

CREATE TABLE IF NOT EXISTS proactive_delivery_events (
    session_key TEXT NOT NULL,
    compound_event_id TEXT NOT NULL,
    delivery_id INTEGER NOT NULL,
    delivered_at TEXT NOT NULL,
    PRIMARY KEY(session_key, compound_event_id)
);

CREATE TABLE IF NOT EXISTS proactive_ack_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'acked', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_id, event_id, decision)
);
CREATE INDEX IF NOT EXISTS idx_proactive_ack_status
ON proactive_ack_outbox(status, created_at);

CREATE TABLE IF NOT EXISTS proactive_tick_traces (
    tick_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    mode TEXT NOT NULL,
    decision TEXT NOT NULL,
    sent INTEGER NOT NULL,
    reason TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class ProactiveStateStore:
    """Durable delivery, dedupe, trace and ACK-outbox state."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._path = str(db_path or settings.DATABASE_PATH)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self._path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def count_deliveries_since(
        self,
        session_key: str,
        since: datetime,
    ) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM proactive_deliveries "
                "WHERE session_key = ? AND sent_at >= ?",
                (session_key, _utc(since).isoformat()),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def delivery_key_seen(
        self,
        session_key: str,
        delivery_key: str,
        *,
        now: datetime,
        window_hours: int,
    ) -> bool:
        cutoff = _utc(now) - timedelta(hours=max(1, window_hours))
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM proactive_deliveries "
                "WHERE session_key = ? AND delivery_key = ? AND sent_at >= ?",
                (session_key, delivery_key, cutoff.isoformat()),
            ).fetchone()
        return row is not None

    def delivered_events(self, session_key: str, evidence: Iterable[str]) -> set[str]:
        values = tuple(dict.fromkeys(str(item) for item in evidence if str(item)))
        if not values:
            return set()
        placeholders = ",".join("?" for _ in values)
        with self._lock:
            rows = self._db.execute(
                "SELECT compound_event_id FROM proactive_delivery_events "
                f"WHERE session_key = ? AND compound_event_id IN ({placeholders})",
                (session_key, *values),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def recent_messages(
        self,
        session_key: str,
        *,
        now: datetime,
        window_hours: int,
    ) -> list[str]:
        cutoff = _utc(now) - timedelta(hours=max(1, window_hours))
        with self._lock:
            rows = self._db.execute(
                "SELECT message FROM proactive_deliveries "
                "WHERE session_key = ? AND sent_at >= ? ORDER BY sent_at DESC",
                (session_key, cutoff.isoformat()),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def record_delivery_and_enqueue_acks(
        self,
        *,
        context: AgentTickContext,
        delivery_key: str,
        message: str,
        evidence: Iterable[str],
    ) -> tuple[int, tuple[int, ...]]:
        now = _utc(context.started_at).isoformat()
        evidence_values = tuple(dict.fromkeys(str(item) for item in evidence if str(item)))
        with self._lock:
            try:
                cursor = self._db.execute(
                    "INSERT INTO proactive_deliveries("
                    "session_key, delivery_key, message, priority, sent_at"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (
                        context.target.session_key,
                        delivery_key,
                        message,
                        context.priority,
                        now,
                    ),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("Failed to record proactive delivery")
                delivery_id = int(cursor.lastrowid)
                ack_ids: list[int] = []
                for compound in evidence_values:
                    source_id, event_id = split_compound_event_id(compound)
                    self._db.execute(
                        "INSERT OR IGNORE INTO proactive_delivery_events("
                        "session_key, compound_event_id, delivery_id, delivered_at"
                        ") VALUES (?, ?, ?, ?)",
                        (context.target.session_key, compound, delivery_id, now),
                    )
                    ack_cursor = self._db.execute(
                        "INSERT OR IGNORE INTO proactive_ack_outbox("
                        "delivery_id, source_id, event_id, decision, status, "
                        "attempts, created_at, updated_at"
                        ") VALUES (?, ?, ?, 'delivered', 'pending', 0, ?, ?)",
                        (delivery_id, source_id, event_id, now, now),
                    )
                    if ack_cursor.rowcount == 1 and ack_cursor.lastrowid:
                        ack_ids.append(int(ack_cursor.lastrowid))
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return delivery_id, tuple(ack_ids)

    def pending_acks(self, *, limit: int = 100) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT id, source_id, event_id, decision, attempts "
                "FROM proactive_ack_outbox WHERE status IN ('pending', 'failed') "
                "ORDER BY created_at LIMIT ?",
                (max(1, limit),),
            ).fetchall()

    def settle_ack(self, ack_id: int, *, success: bool, error: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._db.execute(
                "UPDATE proactive_ack_outbox SET status = ?, attempts = attempts + 1, "
                "last_error = ?, updated_at = ? WHERE id = ?",
                ("acked" if success else "failed", error or None, now, ack_id),
            )
            self._db.commit()

    def ack_status(self, ack_id: int) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT status FROM proactive_ack_outbox WHERE id = ?", (ack_id,)
            ).fetchone()
        return str(row[0]) if row is not None else None

    def delivery_count(self, session_key: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM proactive_deliveries WHERE session_key = ?",
                (session_key,),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def record_tick(self, context: AgentTickContext, result: ProactiveTickResult) -> None:
        payload = {
            "tick_id": result.tick_id,
            "decision": result.decision,
            "sent": result.sent,
            "score": result.score,
            "reason": result.reason,
            "message": result.message,
            "evidence": list(result.evidence),
            "delivery_key": result.delivery_key,
            "next_check_at": (
                result.next_check_at.isoformat() if result.next_check_at else None
            ),
            "traces": [trace.__dict__ for trace in result.traces],
            "ack_outbox_ids": list(result.ack_outbox_ids),
            "error": result.error,
        }
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO proactive_tick_traces("
                "tick_id, session_key, mode, decision, sent, reason, result_json, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    context.tick_id,
                    context.target.session_key,
                    context.mode,
                    result.decision,
                    int(result.sent),
                    result.reason,
                    json.dumps(payload, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._db.commit()

    def tick_trace(self, tick_id: str) -> dict[str, object] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT result_json FROM proactive_tick_traces WHERE tick_id = ?",
                (tick_id,),
            ).fetchone()
        return json.loads(str(row[0])) if row is not None else None


def split_compound_event_id(compound: str) -> tuple[str, str]:
    source_id, separator, event_id = str(compound).partition(":")
    if not separator or not source_id or not event_id:
        raise ValueError(f"Invalid proactive evidence id: {compound!r}")
    return source_id, event_id


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
