"""
会话管理：维护用户的对话上下文
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memory.store import MemoryStore

logger = logging.getLogger(__name__)


@dataclass
class Message:
    role: str  # "user" | "assistant" | "system"
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    tool_calls: list[dict] = field(default_factory=list)
    tool_outputs: list[dict] = field(default_factory=list)
    evidence_item_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        result = {
            "role": self.role,
            "content": self.content,
        }
        if self.tool_calls:
            result["tool_calls"] = self.tool_calls
        return result


@dataclass
class Session:
    user_id: str
    channel: str
    chat_id: str
    messages: deque[Message] = field(default_factory=lambda: deque(maxlen=100))
    metadata: dict = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def add_message(
        self,
        role: str,
        content: str,
        tool_calls: list[dict] | None = None,
        evidence_item_ids: list[str] | None = None,
    ) -> Message:
        msg = Message(
            role=role,
            content=content,
            tool_calls=tool_calls or [],
            evidence_item_ids=evidence_item_ids or [],
        )
        self.messages.append(msg)
        self.updated_at = datetime.now(timezone.utc).isoformat()
        return msg

    def get_recent_messages(self, limit: int | None = None) -> list[Message]:
        msgs = list(self.messages)
        if limit:
            return msgs[-limit:]
        return msgs

    def to_api_messages(self, limit: int | None = None) -> list[dict]:
        """转换为 API 格式的消息列表"""
        msgs = self.get_recent_messages(limit)
        return [m.to_dict() for m in msgs]


class SessionManager:
    """会话管理器"""

    def __init__(self, memory_window: int = 40) -> None:
        self._sessions: dict[str, Session] = {}
        self._memory_window = memory_window

    def get_or_create(
        self,
        session_key: str,
        user_id: str | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
    ) -> Session:
        """获取或创建会话"""
        if session_key not in self._sessions:
            if not user_id or not channel or not chat_id:
                raise ValueError("创建新会话需要 user_id, channel, chat_id")
            self._sessions[session_key] = Session(
                user_id=user_id,
                channel=channel,
                chat_id=chat_id,
                messages=deque(maxlen=self._memory_window * 2),
            )
            logger.info(f"创建新会话: {session_key}")
        return self._sessions[session_key]

    def get(self, session_key: str) -> Session | None:
        """获取会话"""
        return self._sessions.get(session_key)

    def remove(self, session_key: str) -> None:
        """移除会话"""
        if session_key in self._sessions:
            del self._sessions[session_key]
            logger.info(f"移除会话: {session_key}")

    def list_sessions(self) -> list[tuple[str, Session]]:
        """列出所有会话"""
        return list(self._sessions.items())

    def cleanup_idle(self, idle_hours: int = 24) -> list[str]:
        """清理闲置会话"""
        now = datetime.now(timezone.utc)
        removed = []
        for key, session in list(self._sessions.items()):
            try:
                updated = datetime.fromisoformat(session.updated_at)
                if (now - updated).total_seconds() > idle_hours * 3600:
                    self.remove(key)
                    removed.append(key)
            except Exception:
                pass
        return removed
