"""SQLite owner for durable MessageBus state."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from agent.core.envelope import MessageEnvelope
from persistence.database import get_connection


class RuntimeMessageStore:
    """Persist queue admission and terminal state for crash-safe deduplication."""

    def admit(self, envelope: MessageEnvelope) -> bool:
        conn = get_connection()
        now = datetime.now(timezone.utc).isoformat()
        try:
            conn.execute(
                """
                INSERT INTO runtime_messages (
                    id, session_key, direction, status, dedupe_key,
                    payload_json, attempts, leased_until, created_at, updated_at
                ) VALUES (?, ?, ?, 'queued', ?, ?, 0, NULL, ?, ?)
                """,
                (
                    envelope.message_id,
                    envelope.session_key,
                    envelope.direction,
                    envelope.dedupe_key,
                    json.dumps(envelope.to_record(), ensure_ascii=False),
                    envelope.created_at.isoformat(),
                    now,
                ),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def mark(self, message_id: str, status: str, *, increment_attempts: bool = False) -> None:
        if status not in {"queued", "running", "done", "failed", "cancelled"}:
            raise ValueError(f"invalid runtime message status: {status}")
        conn = get_connection()
        attempts_sql = "attempts = attempts + 1," if increment_attempts else ""
        conn.execute(
            f"""
            UPDATE runtime_messages
            SET status = ?, {attempts_sql} updated_at = ?
            WHERE id = ?
            """,
            (status, datetime.now(timezone.utc).isoformat(), message_id),
        )
        conn.commit()

    def recover_inbound(self) -> list[MessageEnvelope]:
        """Requeue only work that never reached a terminal state."""
        conn = get_connection()
        rows = conn.execute(
            """
            SELECT payload_json FROM runtime_messages
            WHERE direction = 'inbound' AND status IN ('queued', 'running')
            ORDER BY created_at, id
            """
        ).fetchall()
        envelopes = [MessageEnvelope.from_record(json.loads(str(row[0]))) for row in rows]
        for envelope in envelopes:
            self.mark(envelope.message_id, "queued")
        return envelopes

    def status(self, message_id: str) -> str | None:
        row = get_connection().execute(
            "SELECT status FROM runtime_messages WHERE id = ?", (message_id,)
        ).fetchone()
        return str(row[0]) if row else None
