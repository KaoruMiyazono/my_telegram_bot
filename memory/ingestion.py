"""
后处理 Worker：对话结束后处理记忆（保护新记忆、检测否定意图、标记废弃）
参考 akashic-agent/memory2/post_response_worker.py
"""

from __future__ import annotations

import logging

from agent.core.events import MemorySupersedeEvent, TurnCommittedEvent
from memory.store import MemoryStore

logger = logging.getLogger(__name__)


class PostResponseMemoryWorker:
    """对话后处理 Worker：保护新记忆、检测否定意图、标记废弃"""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store
        self._protected_memory_ids: set[str] = set()

    async def on_turn_committed(self, event: TurnCommittedEvent) -> None:
        """Turn 完成提交时的处理"""
        # 1. 保护本轮新写入的记忆
        if event.new_memory_ids:
            self._protect_memories(event.new_memory_ids)

        # 2. 检测否定意图
        negation_detected = await self._detect_negation(event)
        if negation_detected:
            logger.info(f"检测到否定意图: user={event.user_id}")

        # 3. 如果有新记忆写入，检查是否需要废弃旧记忆
        if event.new_memory_ids:
            await self._handle_supersede(event)

    def _protect_memories(self, memory_ids: list[str]) -> None:
        """保护本轮新写入的记忆，防止被误删"""
        self._protected_memory_ids.update(memory_ids)
        logger.debug(f"保护记忆: {memory_ids}")

    def release_protection(self) -> None:
        """释放保护（通常在下一轮开始前）"""
        self._protected_memory_ids.clear()

    async def _detect_negation(self, event: TurnCommittedEvent) -> bool:
        """检测用户否定意图"""
        # 简化实现：关键词匹配
        negation_keywords = [
            "不对",
            "不是",
            "错了",
            "不是这样的",
            "不正确",
            "忘记了",
            "不要再",
            "删除",
            "取消",
            "not",
            "wrong",
            "no",
            "forget",
            "delete",
        ]

        text = event.inbound_message.get("content", "").lower()
        return any(kw in text for kw in negation_keywords)

    async def _handle_supersede(self, event: TurnCommittedEvent) -> None:
        """处理记忆废弃逻辑"""
        if not event.new_memory_ids:
            return

        # 获取新写入的记忆
        new_items = self._store.get_items_by_ids(
            user_id=event.user_id, ids=event.new_memory_ids
        )

        for new_item in new_items:
            if new_item.memory_type in ("preference", "profile"):
                # preference/profile：检查是否需要废弃同类旧记忆
                await self._supersede_similar(new_item, event)
            elif new_item.memory_type == "procedure":
                # procedure：检查是否需要废弃同工具的旧记忆
                await self._supersede_procedure(new_item, event)

    async def _supersede_similar(self, new_item, event: TurnCommittedEvent) -> None:
        """废弃相似的同类型记忆"""
        # 简化实现：按 memory_type 查找相似记忆
        similar = await self._store.vector_search(
            user_id=event.user_id,
            query_vec=await self._embed(new_item.summary),  # 需要注入 embedder
            top_k=5,
            memory_types=[new_item.memory_type],
            score_threshold=0.90,
        )

        old_to_supersede = [
            s["id"]
            for s in similar
            if s["id"] != new_item.id and s["id"] not in self._protected_memory_ids
        ]

        if old_to_supersede:
            self._store.mark_superseded_batch(old_to_supersede)
            for old_id in old_to_supersede:
                old_item = self._store.get_items_by_ids(
                    user_id=event.user_id, ids=[old_id]
                )
                if old_item:
                    self._store.record_replacement(
                        old_item_id=old_id,
                        new_item_id=new_item.id,
                        old_user_id=event.user_id,
                        new_user_id=event.user_id,
                        old_memory_type=old_item[0].memory_type,
                        new_memory_type=new_item.memory_type,
                        old_summary=old_item[0].summary,
                        new_summary=new_item.summary,
                    )
            logger.info(f"废弃记忆: {old_to_supersede} -> {new_item.id}")

    async def _supersede_procedure(self, new_item, event: TurnCommittedEvent) -> None:
        """废弃同工具的旧 procedure"""
        # 简化实现：检查 extra 中的工具名称
        new_tools = new_item.extra.get("tools", [])
        if not new_tools:
            return

        # 查找使用相同工具的旧 procedure
        procedures = self._store.list_items(
            user_id=event.user_id, memory_type="procedure", status="active"
        )

        old_to_supersede = []
        for proc in procedures:
            if proc.id == new_item.id or proc.id in self._protected_memory_ids:
                continue
            proc_tools = proc.extra.get("tools", [])
            if set(proc_tools) & set(new_tools):
                # 有工具重叠，检查是否应该废弃
                old_to_supersede.append(proc.id)

        if old_to_supersede:
            self._store.mark_superseded_batch(old_to_supersede)
            logger.info(f"废弃 procedure: {old_to_supersede} -> {new_item.id}")

    async def _embed(self, text: str) -> list[float]:
        """临时 embed 方法（实际应该注入 embedder）"""
        # 简化实现：返回零向量
        # 实际使用时应该在 Worker 初始化时注入 Embedder
        return [0.0] * 1536


class IngestionWorker:
    """记忆写入 Worker：处理显式记忆写入请求"""

    def __init__(self, store: MemoryStore, embedder) -> None:
        self._store = store
        self._embedder = embedder

    async def save_memory(
        self,
        user_id: str,
        memory_type: str,
        summary: str,
        source_ref: str | None = None,
        happened_at: str | None = None,
        emotional_weight: int = 0,
        extra: dict | None = None,
    ) -> str:
        """保存一条记忆"""
        embedding = await self._embedder.embed(summary)

        result = self._store.upsert_item(
            user_id=user_id,
            memory_type=memory_type,
            summary=summary,
            embedding=embedding,
            source_ref=source_ref,
            extra=extra or {},
            happened_at=happened_at,
            emotional_weight=emotional_weight,
        )

        logger.info(f"记忆保存: {result} type={memory_type} user={user_id}")
        return result
