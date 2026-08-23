from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from persistence.database import get_connection


@dataclass(frozen=True)
class SessionCompaction:
    user_id: int
    chat_id: int
    generation: int
    parent_generation: int
    summary: str
    source_ref: str
    source_from_seq: int
    consolidated_through_seq: int
    source_message_ids: tuple[str, ...]
    retained_tail: tuple[dict[str, Any], ...]
    trigger: str
    context_window: int
    soft_limit_tokens: int
    hard_input_tokens: int
    keep_recent_tokens: int
    estimated_tokens_before: int
    estimated_tokens_after: int
    model: str
    summary_usage: dict[str, Any] | None = None


class SessionCompactionConflictError(RuntimeError):
    """The durable session compaction head changed before commit."""


class SessionCompactionStore:
    """SQLite owner for immutable session compaction generations."""

    def get_head(self, user_id: int, chat_id: int) -> int:
        row = get_connection().execute(
            """
            SELECT last_compaction_generation
            FROM conversation_sessions
            WHERE user_id = ? AND chat_id = ?
            """,
            (user_id, chat_id),
        ).fetchone()
        return int(row[0] or 0) if row is not None else 0

    def get_active(self, user_id: int, chat_id: int) -> SessionCompaction | None:
        row = get_connection().execute(
            """
            SELECT
                c.user_id, c.chat_id, c.generation, c.parent_generation,
                c.summary, c.source_ref, c.source_from_seq,
                c.consolidated_through_seq, c.source_message_ids_json,
                c.retained_tail_json, c.trigger, c.context_window,
                c.soft_limit_tokens, c.hard_input_tokens,
                c.keep_recent_tokens, c.estimated_tokens_before,
                c.estimated_tokens_after, c.model, c.summary_usage_json
            FROM session_compactions AS c
            JOIN conversation_sessions AS s
              ON s.user_id = c.user_id AND s.chat_id = c.chat_id
             AND s.last_compaction_generation = c.generation
            WHERE c.user_id = ? AND c.chat_id = ?
            """,
            (user_id, chat_id),
        ).fetchone()
        return _decode_row(row) if row is not None else None

    def list_generations(self, user_id: int, chat_id: int) -> list[SessionCompaction]:
        rows = get_connection().execute(
            """
            SELECT
                user_id, chat_id, generation, parent_generation,
                summary, source_ref, source_from_seq,
                consolidated_through_seq, source_message_ids_json,
                retained_tail_json, trigger, context_window,
                soft_limit_tokens, hard_input_tokens,
                keep_recent_tokens, estimated_tokens_before,
                estimated_tokens_after, model, summary_usage_json
            FROM session_compactions
            WHERE user_id = ? AND chat_id = ?
            ORDER BY generation
            """,
            (user_id, chat_id),
        ).fetchall()
        return [_decode_row(row) for row in rows]

    def commit(
        self,
        checkpoint: SessionCompaction,
        *,
        expected_parent_generation: int,
    ) -> SessionCompaction:
        """Atomically insert one immutable generation and advance its cursor."""

        if checkpoint.parent_generation != expected_parent_generation:
            raise ValueError("checkpoint parent_generation与预期不一致")
        if checkpoint.generation != expected_parent_generation + 1:
            raise ValueError("checkpoint generation必须连续递增")
        conn = get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT last_compaction_generation
                FROM conversation_sessions
                WHERE user_id = ? AND chat_id = ?
                """,
                (checkpoint.user_id, checkpoint.chat_id),
            ).fetchone()
            if row is None:
                raise SessionCompactionConflictError("session不存在，不能提交压缩账本")
            current = int(row[0] or 0)
            if current != expected_parent_generation:
                raise SessionCompactionConflictError(
                    "session compaction head已变化: "
                    f"expected={expected_parent_generation} actual={current}"
                )
            conn.execute(
                """
                INSERT INTO session_compactions (
                    user_id, chat_id, generation, parent_generation,
                    summary, source_ref, source_from_seq,
                    consolidated_through_seq, source_message_ids_json,
                    retained_tail_json, trigger, context_window,
                    soft_limit_tokens, hard_input_tokens,
                    keep_recent_tokens, estimated_tokens_before,
                    estimated_tokens_after, model, summary_usage_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint.user_id,
                    checkpoint.chat_id,
                    checkpoint.generation,
                    checkpoint.parent_generation,
                    checkpoint.summary,
                    checkpoint.source_ref,
                    checkpoint.source_from_seq,
                    checkpoint.consolidated_through_seq,
                    json.dumps(checkpoint.source_message_ids, ensure_ascii=False),
                    json.dumps(checkpoint.retained_tail, ensure_ascii=False),
                    checkpoint.trigger,
                    checkpoint.context_window,
                    checkpoint.soft_limit_tokens,
                    checkpoint.hard_input_tokens,
                    checkpoint.keep_recent_tokens,
                    checkpoint.estimated_tokens_before,
                    checkpoint.estimated_tokens_after,
                    checkpoint.model,
                    (
                        json.dumps(checkpoint.summary_usage, ensure_ascii=False)
                        if checkpoint.summary_usage is not None
                        else None
                    ),
                ),
            )
            updated = conn.execute(
                """
                UPDATE conversation_sessions
                SET last_compaction_generation = ?, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ? AND chat_id = ?
                  AND last_compaction_generation = ?
                """,
                (
                    checkpoint.generation,
                    checkpoint.user_id,
                    checkpoint.chat_id,
                    expected_parent_generation,
                ),
            )
            if updated.rowcount != 1:
                raise SessionCompactionConflictError("压缩cursor原子推进失败")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return checkpoint


def _decode_row(row: tuple[Any, ...]) -> SessionCompaction:
    source_ids = json.loads(str(row[8]))
    retained_tail = json.loads(str(row[9]))
    usage = json.loads(str(row[18])) if row[18] is not None else None
    if not isinstance(source_ids, list) or not all(isinstance(item, str) for item in source_ids):
        raise ValueError("session compaction source_message_ids无效")
    if not isinstance(retained_tail, list) or not all(
        isinstance(item, dict) for item in retained_tail
    ):
        raise ValueError("session compaction retained_tail无效")
    if usage is not None and not isinstance(usage, dict):
        raise ValueError("session compaction summary_usage无效")
    return SessionCompaction(
        user_id=int(row[0]),
        chat_id=int(row[1]),
        generation=int(row[2]),
        parent_generation=int(row[3]),
        summary=str(row[4]),
        source_ref=str(row[5]),
        source_from_seq=int(row[6]),
        consolidated_through_seq=int(row[7]),
        source_message_ids=tuple(source_ids),
        retained_tail=tuple(dict(item) for item in retained_tail),
        trigger=str(row[10]),
        context_window=int(row[11]),
        soft_limit_tokens=int(row[12]),
        hard_input_tokens=int(row[13]),
        keep_recent_tokens=int(row[14]),
        estimated_tokens_before=int(row[15]),
        estimated_tokens_after=int(row[16]),
        model=str(row[17]),
        summary_usage=usage,
    )


_store: SessionCompactionStore | None = None


def get_session_compaction_store() -> SessionCompactionStore:
    global _store
    if _store is None:
        _store = SessionCompactionStore()
    return _store
