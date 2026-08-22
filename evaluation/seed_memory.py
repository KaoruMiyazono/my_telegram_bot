"""
记忆播种器：将模拟/真实对话数据导入 MemoryStore

用途：第3步评估前，需要把 context_sessions 对应的对话记忆
      预先写入向量数据库，这样 Bot 才能检索到上下文来回答问题。

使用方式：
    from evaluation.seed_memory import seed_from_mock_conversations

    embedder = Embedder()
    store = MemoryStore(embedder)
    seeded_ids = await seed_from_mock_conversations(store)

    # 现在 Bot 可以检索到 mock 对话的上下文了
"""

import json
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# 项目根目录下的 mock 数据路径
MOCK_DATA_PATH = Path(__file__).parent.parent / "data" / "evaluation" / "mock_conversations.jsonl"


async def seed_from_mock_conversations(
    store: Any,
    mock_path: str | None = None,
    *,
    eval_user_id: int | None = None,
) -> dict[str, list[str]]:
    """
    将 mock_conversations.jsonl 中的对话导入 MemoryStore。

    如果 eval_user_id 指定，所有记忆使用该 user_id（评估时所有用例
    共享同一用户视角），否则使用 mock 数据中的原始 user_id。

    Args:
        store: MemoryStore 实例
        mock_path: mock 数据路径
        eval_user_id: 评估时使用的统一 user_id（可选）

    Returns:
        {session_id: [memory_id, ...]}
    """
    path = Path(mock_path) if mock_path else MOCK_DATA_PATH

    if not path.exists():
        logger.warning("Mock conversations file not found: %s", path)
        return {}

    conversations = _load_jsonl(path)
    if not conversations:
        logger.warning("No conversations loaded from %s", path)
        return {}

    seeded: dict[str, list[str]] = {}

    for conv in conversations:
        session_id = conv.get("session_id", "")
        user_id = eval_user_id if eval_user_id is not None else conv.get("user_id", 0)
        messages = conv.get("messages", [])

        if not session_id or not messages:
            continue

        memory_ids = []
        for i, msg in enumerate(messages):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")

            if not content.strip():
                continue

            # 为每条消息创建记忆
            memory_type = f"mock_{role}_message"
            summary = _format_message_summary(content, role, max_len=500)

            try:
                memory_item = await store.upsert_item(
                    memory_type=memory_type,
                    summary=summary,
                    user_id=user_id,
                    source_ref=f"session:{user_id}:{session_id[:8]}",
                )
                memory_ids.append(str(memory_item.id))
            except Exception as e:
                logger.error(
                    "Failed to seed memory for session=%s msg=%d: %s",
                    session_id, i, e,
                )

        if memory_ids:
            seeded[session_id] = memory_ids

    logger.info(
        "Seeded %d sessions (%d total memories) from %s",
        len(seeded), sum(len(v) for v in seeded.values()), path,
    )
    return seeded


async def seed_from_raw_conversations(
    store: Any,
    raw_path: str | None = None,
) -> dict[str, list[str]]:
    """
    将 raw_conversations.jsonl 中的真实对话导入 MemoryStore。

    Args:
        store: MemoryStore 实例
        raw_path: 原始日志路径

    Returns:
        {turn_id: [memory_id, ...]}
    """
    path = Path(raw_path) if raw_path else (
        Path(__file__).parent.parent / "data" / "evaluation" / "raw_conversations.jsonl"
    )

    if not path.exists():
        logger.warning("Raw conversations file not found: %s", path)
        return {}

    conversations = _load_jsonl(path)
    if not conversations:
        return {}

    seeded: dict[str, list[str]] = {}

    for turn in conversations:
        turn_id = turn.get("turn_id", "")
        user_id = turn.get("user_id", 0)
        inbound = turn.get("inbound_content", "")
        outbound = turn.get("outbound_message", {}).get("content", "")

        if not turn_id:
            continue

        memory_ids = []

        # 存入用户消息
        if inbound.strip():
            try:
                mem = await store.upsert_item(
                    memory_type="user_message",
                    summary=inbound[:500],
                    user_id=user_id,
                    source_ref=turn_id,
                )
                memory_ids.append(str(mem.id))
            except Exception as e:
                logger.error("Failed to seed inbound for turn=%s: %s", turn_id, e)

        # 存入 Bot 回复
        if outbound.strip():
            try:
                mem = await store.upsert_item(
                    memory_type="assistant_message",
                    summary=outbound[:500],
                    user_id=user_id,
                    source_ref=turn_id,
                )
                memory_ids.append(str(mem.id))
            except Exception as e:
                logger.error("Failed to seed outbound for turn=%s: %s", turn_id, e)

        if memory_ids:
            seeded[turn_id] = memory_ids

    logger.info("Seeded %d turns from %s", len(seeded), path)
    return seeded


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """加载 JSONL 文件"""
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError as e:
                    logger.warning("Skipping invalid JSON line in %s: %s", path, e)
    return items


def _format_message_summary(content: str, role: str, max_len: int = 500) -> str:
    """格式化消息为记忆摘要"""
    truncated = content[:max_len] if len(content) > max_len else content
    return f"[{role}]: {truncated}"
