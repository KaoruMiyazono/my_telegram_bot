"""Stable identifiers shared by every passive Agent turn."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Any
from uuid import uuid4


@dataclass(frozen=True)
class TurnIdentity:
    """Identifiers that connect one request across logs and pipeline phases."""

    turn_id: str
    session_key: str
    trace_id: str


def build_session_key(
    *,
    channel: str,
    chat_id: int | str,
    user_id: int | str,
    account_id: int | str | None = None,
    thread_id: int | str | None = None,
) -> str:
    """Build the canonical cross-channel session isolation key."""

    normalized_channel = str(channel or "telegram").strip().lower() or "telegram"
    if account_id is not None or thread_id is not None:
        account = str(account_id if account_id is not None else user_id).strip() or str(user_id)
        thread = str(thread_id if thread_id is not None else "main").strip() or "main"
        return f"{normalized_channel}:{account}:{chat_id}:{thread}"
    return f"{normalized_channel}:{chat_id}:{user_id}"


def new_turn_id() -> str:
    return f"turn:{uuid4()}"


def new_trace_id() -> str:
    return f"trace:{uuid4()}"


def identity_for_message(
    *,
    user_id: int | str,
    chat_id: int | str,
    channel: str = "telegram",
    metadata: Mapping[str, Any] | None = None,
    turn_id: str = "",
    trace_id: str = "",
) -> TurnIdentity:
    """Create an identity, preserving caller-provided IDs for replay/tests."""

    values = metadata or {}
    effective_channel = str(values.get("channel") or channel or "telegram")
    return TurnIdentity(
        turn_id=turn_id or str(values.get("turn_id") or "") or new_turn_id(),
        session_key=build_session_key(
            channel=effective_channel,
            chat_id=chat_id,
            user_id=user_id,
            account_id=values.get("account_id"),
            thread_id=values.get("thread_id"),
        ),
        trace_id=trace_id or str(values.get("trace_id") or "") or new_trace_id(),
    )
