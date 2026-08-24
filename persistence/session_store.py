"""
会话持久化存储 —— 对齐 akashic-agent SessionManager 的 save 模式

akashic 模式：
  sm._cache.pop(session_key)       # 清缓存
  session = sm.get_or_create(key)  # 获取或创建
  session.add_message(role, content)  # 追加消息
  sm.save(session)                 # 持久化到 SQLite

对应我们：
  SessionStore.save(key, messages, last_consolidated=...)
  SessionStore.load_state(key)      # 加载消息列表 + consolidation 游标
  SessionStore.load(key)            # 兼容旧调用：只加载消息列表
  _sessions dict 作为内存缓存（对齐 akashic _cache）
"""

from __future__ import annotations

import json
import logging
from typing import Any

from persistence.database import get_connection

logger = logging.getLogger(__name__)


#  sessionStore.savek恶意存储
class SessionStore:
    """会话持久化存储，用 SQLite 存消息 JSON 和 consolidation 游标。"""

    def save(
        self,
        user_id: int,
        chat_id: int,
        messages: list[dict[str, Any]],
        *,
        last_consolidated: int | None = None,
        session_key: str = "",
        channel: str = "",
    ) -> None:
        """保存会话消息和 consolidation 游标到数据库（upsert）

        Args:
            user_id: 用户 ID
            chat_id: 聊天 ID
            messages: 消息列表 [{\"role\": ..., \"content\": ...}, ...]
            last_consolidated: 已完成 consolidation 的消息下标游标；未传则保留已有游标 这个last_consolidated是一个整数，表示已经完成consolidation的消息下标游标。consolidation是指将多个消息合并成一个更简洁的表示，以便后续处理。如果没有传入last_consolidated参数，则会保留已有的游标值。
        """
        conn = get_connection()
        cursor = int(last_consolidated) if last_consolidated is not None else None
        if session_key:
            conn.execute(
                """INSERT INTO channel_conversation_sessions(
                    session_key, user_id, chat_id, channel, messages_json,
                    last_consolidated, updated_at
                ) VALUES (?, ?, ?, ?, ?, COALESCE(?, 0), CURRENT_TIMESTAMP)
                ON CONFLICT(session_key) DO UPDATE SET
                    messages_json=excluded.messages_json,
                    last_consolidated=COALESCE(
                        ?, channel_conversation_sessions.last_consolidated
                    ),
                    updated_at=CURRENT_TIMESTAMP""",
                (
                    session_key,
                    user_id,
                    chat_id,
                    channel or session_key.split(":", 1)[0],
                    json.dumps(messages, ensure_ascii=False),
                    cursor,
                    cursor,
                ),
            )
            conn.commit()
            return
        conn.execute(
            """
            INSERT INTO conversation_sessions
                (user_id, chat_id, messages_json, last_consolidated, updated_at)
            VALUES (?, ?, ?, COALESCE(?, 0), CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, chat_id) DO UPDATE SET
                messages_json = excluded.messages_json,
                last_consolidated = COALESCE(?, conversation_sessions.last_consolidated),
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                user_id,
                chat_id,
                json.dumps(messages, ensure_ascii=False),
                cursor,
                cursor,
            ),
        )
        conn.commit()
        logger.debug(
            "Session saved: user=%d chat=%d messages=%d last_consolidated=%s",
            user_id,
            chat_id,
            len(messages),
            str(cursor) if cursor is not None else "preserve",
        )

    def load_state(
        self,
        user_id: int,
        chat_id: int,
        *,
        session_key: str = "",
    ) -> tuple[list[dict[str, Any]], int] | None:
        """从数据库加载会话消息和 consolidation 游标。

        Args:
            user_id: 用户 ID
            chat_id: 聊天 ID

        Returns:
            (消息列表, last_consolidated)，如果不存在返回 None
        """
        conn = get_connection()
        if session_key:
            row = conn.execute(
                """SELECT messages_json, last_consolidated
                   FROM channel_conversation_sessions WHERE session_key=?""",
                (session_key,),
            ).fetchone()
            if row is None:
                return None
            try:
                return json.loads(row[0]), int(row[1] or 0)
            except json.JSONDecodeError as error:
                logger.warning(
                    "Failed to parse channel session JSON key=%s: %s",
                    session_key,
                    error,
                )
                return None
        row = conn.execute(
            """
            SELECT messages_json, last_consolidated
            FROM conversation_sessions
            WHERE user_id = ? AND chat_id = ?
            """,
            (user_id, chat_id),
        ).fetchone()

        if row is None:
            return None

        try:
            messages = json.loads(row[0])
            last_consolidated = int(row[1] or 0)
            logger.debug(
                "Session loaded: user=%d chat=%d messages=%d last_consolidated=%d",
                user_id, chat_id, len(messages), last_consolidated,
            )
            return messages, last_consolidated
        except json.JSONDecodeError as e:
            logger.warning("Failed to parse session JSON for user=%d chat=%d: %s", user_id, chat_id, e)
            return None

    def load(self, user_id: int, chat_id: int) -> list[dict[str, Any]] | None:
        """从数据库加载会话消息，兼容旧调用。"""
        state = self.load_state(user_id, chat_id)
        if state is None:
            return None
        messages, _last_consolidated = state
        return messages

    #  获取一段记忆，以后AI可以说，我说的来自哪个user，哪个session，哪个seq的消息
    def fetch_messages(
        self,
        user_id: int,
        chat_id: int,
        *,
        seq: int | None = None,
        seq_end: int | None = None,
        context: int = 0,
        limit: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """Fetch raw messages by session and optional message seq."""
        messages = self.load(user_id, chat_id) or []
        if not messages:
            return [], 0

        if seq is None:
            selected = messages[-max(1, int(limit)) :]
            start = len(messages) - len(selected)
        else:
            ctx = max(0, int(context))
            ref_start = int(seq)
            ref_end = int(seq_end) if seq_end is not None else ref_start
            if ref_end < ref_start:
                ref_start, ref_end = ref_end, ref_start
            start = max(0, ref_start - ctx)
            end = min(len(messages), ref_end + ctx + 1)
            selected = messages[start:end]

        result = []
        for offset, message in enumerate(selected):
            actual_seq = start + offset
            result.append(
                {
                    "role": str(message.get("role") or ""),
                    "content": str(message.get("content") or ""),
                    "seq": actual_seq,
                    "source_ref": f"session:{user_id}:{chat_id}#msg:{actual_seq}",
                    "in_source_ref": (
                        seq is not None
                        and int(seq) <= actual_seq <= int(seq_end if seq_end is not None else seq)
                    ),
                }
            )
        if seq is not None:
            ref_end = int(seq_end) if seq_end is not None else int(seq)
            low = min(int(seq), ref_end)
            high = max(int(seq), ref_end)
            matched = max(0, min(high, len(messages) - 1) - max(low, 0) + 1)
        else:
            matched = len(result)
        return result, matched

    #  在数据库中找到符合条件的消息，返回消息列表和总数，比如我喜欢什么，他会回答
    def search_messages(
        self,
        query: str,
        *,
        user_id: int,
        role: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Search persisted raw session messages for a user.

        This is a small local equivalent of Akashic's search_messages tool:
        it locates original message text and returns source_ref values that
        fetch_messages can use for evidence.
        """
        term = (query or "").strip()
        if not term:
            return [], 0

        limit = max(1, min(int(limit), 50))
        offset = max(0, int(offset))
        role_filter = (role or "").strip() or None
        query_lower = term.lower()
        query_terms = [part.lower() for part in query_lower.split() if part.strip()]

        conn = get_connection()
        rows = conn.execute(
            """
            SELECT chat_id, messages_json
            FROM conversation_sessions
            WHERE user_id = ?
            ORDER BY updated_at DESC
            """,
            (user_id,),
        ).fetchall()

        matches: list[dict[str, Any]] = []
        for chat_id, messages_json in rows:
            try:
                messages = json.loads(messages_json)
            except json.JSONDecodeError:
                logger.warning("Failed to parse session JSON for user=%d chat=%s", user_id, chat_id)
                continue
            if not isinstance(messages, list):
                continue
            for seq, message in enumerate(messages):
                msg_role = str(message.get("role") or "")
                if role_filter and msg_role != role_filter:
                    continue
                content = str(message.get("content") or "")
                content_lower = content.lower()
                if query_lower not in content_lower and not any(
                    part in content_lower for part in query_terms
                ):
                    continue
                matches.append(
                    {
                        "role": msg_role,
                        "content": content,
                        "seq": seq,
                        "chat_id": int(chat_id),
                        "source_ref": f"session:{user_id}:{int(chat_id)}#msg:{seq}",
                    }
                )

        total = len(matches)
        return matches[offset : offset + limit], total

    #  删除特定记忆
    def delete(self, user_id: int, chat_id: int) -> None:
        """删除会话"""
        conn = get_connection()
        conn.execute(
            "DELETE FROM session_compactions WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id),
        )
        conn.execute(
            "DELETE FROM conversation_sessions WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id),
        )
        conn.commit()
        logger.debug("Session deleted: user=%d chat=%d", user_id, chat_id)


# 模块级单例（同一进程内共享）
_session_store: SessionStore | None = None


def get_session_store() -> SessionStore:
    """获取 SessionStore 单例"""
    global _session_store
    if _session_store is None:
        _session_store = SessionStore()
    return _session_store
