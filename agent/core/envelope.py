"""Durable message contracts used by the asynchronous agent runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any, Literal, cast
from uuid import uuid4

from agent.core.ids import build_session_key
from agent.core.types import InboundMessage, OutboundMessage


class MessagePriority(IntEnum):
    """Lower values leave the bus first."""

    INTERRUPT = 0
    NORMAL = 10
    SYSTEM = 20


@dataclass(frozen=True)
class MessageEnvelope:
    """Channel-neutral, persistable wrapper around one runtime message."""

    message_id: str
    session_key: str
    channel: str
    user_id: int
    chat_id: int
    client_message_id: str | None
    payload: dict[str, Any]
    created_at: datetime
    priority: MessagePriority = MessagePriority.NORMAL
    direction: Literal["inbound", "outbound"] = "inbound"

    @property
    def dedupe_key(self) -> str | None:
        if not self.client_message_id:
            return None
        return f"{self.session_key}:{self.client_message_id}"

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["created_at"] = self.created_at.isoformat()
        record["priority"] = int(self.priority)
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "MessageEnvelope":
        created_at = datetime.fromisoformat(str(record["created_at"]))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        direction = cast(Literal["inbound", "outbound"], str(record.get("direction", "inbound")))
        return cls(
            message_id=str(record["message_id"]),
            session_key=str(record["session_key"]),
            channel=str(record["channel"]),
            user_id=int(record["user_id"]),
            chat_id=int(record["chat_id"]),
            client_message_id=(
                str(record["client_message_id"])
                if record.get("client_message_id") is not None
                else None
            ),
            payload=dict(record.get("payload") or {}),
            created_at=created_at,
            priority=MessagePriority(int(record.get("priority", MessagePriority.NORMAL))),
            direction=direction,
        )

    def as_inbound(self) -> InboundMessage:
        if self.direction != "inbound":
            raise ValueError("outbound envelope cannot become InboundMessage")
        return InboundMessage(
            user_id=self.user_id,
            chat_id=self.chat_id,
            content=str(self.payload.get("content") or ""),
            metadata=dict(self.payload.get("metadata") or {}),
            channel=self.channel,
            turn_id=str(self.payload.get("turn_id") or ""),
            trace_id=str(self.payload.get("trace_id") or ""),
        )

    def as_outbound(self) -> OutboundMessage:
        if self.direction != "outbound":
            raise ValueError("inbound envelope cannot become OutboundMessage")
        return OutboundMessage(
            chat_id=self.chat_id,
            content=str(self.payload.get("content") or ""),
            format=str(self.payload.get("format") or "text"),
            turn_id=str(self.payload.get("turn_id") or ""),
            trace_id=str(self.payload.get("trace_id") or ""),
            metadata=dict(self.payload.get("metadata") or {}),
        )


def envelope_from_inbound(
    message: InboundMessage,
    *,
    client_message_id: str | None = None,
    priority: MessagePriority = MessagePriority.NORMAL,
) -> MessageEnvelope:
    session_key = build_session_key(
        channel=message.channel,
        chat_id=message.chat_id,
        user_id=message.user_id,
    )
    return MessageEnvelope(
        message_id=f"message:{uuid4().hex}",
        session_key=session_key,
        channel=message.channel,
        user_id=message.user_id,
        chat_id=message.chat_id,
        client_message_id=client_message_id,
        payload={
            "content": message.content,
            "metadata": dict(message.metadata),
            "turn_id": message.turn_id,
            "trace_id": message.trace_id,
        },
        created_at=datetime.now(timezone.utc),
        priority=priority,
        direction="inbound",
    )


def envelope_from_outbound(
    message: OutboundMessage,
    *,
    inbound: MessageEnvelope,
) -> MessageEnvelope:
    return MessageEnvelope(
        message_id=f"message:{uuid4().hex}",
        session_key=inbound.session_key,
        channel=inbound.channel,
        user_id=inbound.user_id,
        chat_id=message.chat_id,
        client_message_id=None,
        payload={
            "content": message.content,
            "format": message.format,
            "metadata": dict(message.metadata),
            "turn_id": message.turn_id,
            "trace_id": message.trace_id,
        },
        created_at=datetime.now(timezone.utc),
        priority=MessagePriority.NORMAL,
        direction="outbound",
    )
