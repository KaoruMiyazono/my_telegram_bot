"""Channel-neutral adapter contract for the shared TurnRuntime."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent.core.envelope import MessagePriority, envelope_from_inbound
from agent.core.types import InboundMessage
from config.settings import settings

if TYPE_CHECKING:
    from agent.runtime.turn_runtime import TurnRuntime


@dataclass(frozen=True)
class ChannelRequest:
    channel: str
    account_id: str
    chat_id: int
    thread_id: str
    content: str
    user_id: int
    client_message_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChannelReceipt:
    accepted: bool
    message_id: str
    turn_id: str
    trace_id: str
    session_key: str


class ChannelIdentityStore:
    """Default-isolated identity mapping with explicit cross-channel binding."""

    _VIRTUAL_USER_START = 8_000_000_000_000_000

    def __init__(self, path: str | Path | None = None) -> None:
        self._db = sqlite3.connect(str(path or settings.DATABASE_PATH))
        self._db.row_factory = sqlite3.Row
        self._ensure_schema()

    def resolve(
        self,
        channel: str,
        account_id: str,
        *,
        trusted_native_user_id: int | None = None,
    ) -> int:
        row = self._db.execute(
            "SELECT user_id FROM channel_identities WHERE channel=? AND account_id=?",
            (channel, account_id),
        ).fetchone()
        if row is not None:
            return int(row[0])
        if trusted_native_user_id is not None:
            user_id = int(trusted_native_user_id)
            explicit = 1
        else:
            maximum = self._db.execute(
                "SELECT MAX(user_id) FROM channel_identities WHERE user_id>=?",
                (self._VIRTUAL_USER_START,),
            ).fetchone()[0]
            user_id = int(maximum or self._VIRTUAL_USER_START - 1) + 1
            explicit = 0
        now = datetime.now(timezone.utc).isoformat()
        self._db.execute(
            """INSERT INTO channel_identities(
                channel, account_id, user_id, explicit_binding, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (channel, account_id, user_id, explicit, now, now),
        )
        self._db.commit()
        return user_id

    def bind(self, channel: str, account_id: str, user_id: int) -> int:
        now = datetime.now(timezone.utc).isoformat()
        self._db.execute(
            """INSERT INTO channel_identities(
                channel, account_id, user_id, explicit_binding, created_at, updated_at
            ) VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(channel, account_id) DO UPDATE SET
                user_id=excluded.user_id, explicit_binding=1, updated_at=excluded.updated_at""",
            (channel, account_id, int(user_id), now, now),
        )
        self._db.commit()
        return int(user_id)

    def close(self) -> None:
        self._db.close()

    def _ensure_schema(self) -> None:
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS channel_identities (
                channel TEXT NOT NULL,
                account_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                explicit_binding INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(channel, account_id)
            )"""
        )
        self._db.commit()


class RuntimeChannelAdapter:
    channel = "unknown"

    def __init__(self, runtime: "TurnRuntime", identities: ChannelIdentityStore) -> None:
        self.runtime = runtime
        self.identities = identities

    async def submit(self, request: ChannelRequest) -> ChannelReceipt:
        metadata = {
            **request.metadata,
            "account_id": request.account_id,
            "thread_id": request.thread_id or "main",
        }
        inbound = InboundMessage(
            user_id=request.user_id,
            chat_id=request.chat_id,
            content=request.content,
            metadata=metadata,
            channel=request.channel,
        )
        envelope = envelope_from_inbound(
            inbound, client_message_id=request.client_message_id
        )
        accepted = await self.runtime.bus.publish_inbound(envelope)
        return ChannelReceipt(
            accepted=accepted,
            message_id=envelope.message_id,
            turn_id=str(envelope.payload["turn_id"]),
            trace_id=str(envelope.payload["trace_id"]),
            session_key=envelope.session_key,
        )

    async def cancel(self, request: ChannelRequest) -> ChannelReceipt:
        interrupt = ChannelRequest(
            channel=request.channel,
            account_id=request.account_id,
            chat_id=request.chat_id,
            thread_id=request.thread_id,
            content="/stop",
            user_id=request.user_id,
            client_message_id=request.client_message_id,
            metadata=request.metadata,
        )
        metadata = {
            **interrupt.metadata,
            "account_id": interrupt.account_id,
            "thread_id": interrupt.thread_id or "main",
        }
        envelope = envelope_from_inbound(
            InboundMessage(
                user_id=interrupt.user_id,
                chat_id=interrupt.chat_id,
                content="/stop",
                metadata=metadata,
                channel=interrupt.channel,
            ),
            client_message_id=interrupt.client_message_id,
            priority=MessagePriority.INTERRUPT,
        )
        accepted = await self.runtime.bus.publish_inbound(envelope)
        return ChannelReceipt(
            accepted,
            envelope.message_id,
            str(envelope.payload["turn_id"]),
            str(envelope.payload["trace_id"]),
            envelope.session_key,
        )
