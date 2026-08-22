"""
Legacy 记忆检索器：历史 RAG 检索草稿。

生产链路请使用 memory.engine.DefaultMemoryEngine。被动注入和 recall_memory
都应通过 MemoryEngine 的统一检索 core，避免这里的历史实现与当前策略漂移。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from memory.store import MemoryStore

if TYPE_CHECKING:
    from memory.embedder import Embedder

logger = logging.getLogger(__name__)


class MemoryRetriever:
    """Legacy compatibility stub; not wired into the current runtime."""

    def __init__(self, store: MemoryStore, embedder: "Embedder") -> None:
        self._store = store
        self._embedder = embedder

    async def prepare_context(
        self,
        user_id: str,
        query_text: str,
        memory_window: int = 40,
    ) -> dict:
        """准备记忆上下文：向量检索 + 关键词检索融合"""
        # 1. 向量检索（语义）
        embedding = await self._embedder.embed(query_text)
        semantic_hits = await self._store.vector_search(
            user_id=user_id,
            query_vec=embedding,
            top_k=5,
            memory_types=["profile", "preference", "procedure", "event"],
            score_threshold=0.6,
        )

        # 2. 关键词检索（补充）
        keyword_hits = self._keyword_search(
            user_id=user_id,
            terms=self._extract_keywords(query_text),
            limit=3,
        )

        # 3. RRF 融合
        fused_hits = self._rrf_fusion(semantic_hits, keyword_hits, k=60)

        return {
            "memories": fused_hits[:memory_window],
            "query_text": query_text,
            "semantic_count": len(semantic_hits),
            "keyword_count": len(keyword_hits),
        }

    def _extract_keywords(self, text: str) -> list[str]:
        """从文本中提取关键词"""
        # 简化实现：提取中英文单词和短语
        # 实际可接入 jieba 等分词工具
        words = re.findall(r"[一-鿿]+|[a-zA-Z]{2,}", text.lower())
        return [w for w in words if len(w) >= 2]

    def _keyword_search(
        self,
        user_id: str,
        terms: list[str],
        limit: int = 10,
    ) -> list[dict]:
        """关键词检索"""
        if not terms:
            return []

        # 从 store 中按 summary LIKE 搜索
        items = self._store.list_items(user_id=user_id, status="active", limit=500)

        scored = []
        for item in items:
            summary_lower = item.summary.lower()
            score = sum(1 for t in terms if t.lower() in summary_lower)
            if score > 0:
                scored.append(
                    {
                        "id": item.id,
                        "memory_type": item.memory_type,
                        "summary": item.summary,
                        "extra": item.extra,
                        "source_ref": item.source_ref,
                        "happened_at": item.happened_at,
                        "reinforcement": item.reinforcement,
                        "emotional_weight": item.emotional_weight,
                        "score": float(score),
                    }
                )

        scored.sort(key=lambda x: (x["score"], x["reinforcement"]), reverse=True)
        return scored[:limit]

    def _rrf_fusion(
        self,
        semantic_hits: list[dict],
        keyword_hits: list[dict],
        k: int = 60,
    ) -> list[dict]:
        """Reciprocal Rank Fusion (RRF) 融合两种检索结果"""
        fused: dict[str, dict] = {}

        # 语义检索结果
        for rank, item in enumerate(semantic_hits, 1):
            item_id = item["id"]
            if item_id not in fused:
                fused[item_id] = item.copy()
                fused[item_id]["rrf_score"] = 0.0
            fused[item_id]["rrf_score"] += 1.0 / (k + rank)
            fused[item_id]["_semantic_rank"] = rank

        # 关键词检索结果
        for rank, item in enumerate(keyword_hits, 1):
            item_id = item["id"]
            if item_id not in fused:
                fused[item_id] = item.copy()
                fused[item_id]["rrf_score"] = 0.0
            fused[item_id]["rrf_score"] += 1.0 / (k + rank)
            fused[item_id]["_keyword_rank"] = rank

        # 按 RRF 分数排序
        results = list(fused.values())
        results.sort(key=lambda x: x["rrf_score"], reverse=True)

        return results
