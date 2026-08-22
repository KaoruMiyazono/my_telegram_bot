import asyncio
import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["TG_BOT_TOKEN"] = "test_token"
os.environ["DEEPSEEK_API_KEY"] = "test_deepseek_key"
os.environ["ALIYUN_DASHSCOPE_API_KEY"] = "test_aliyun_key"
os.environ["DATABASE_PATH"] = tempfile.mktemp(suffix=".db")

from agent.core.types import MemoryItem
from memory.engine import (
    DefaultMemoryEngine,
    ExplicitRetrievalRequest,
    MemoryRetrieveRequest,
    MemoryScope,
)


def _memory(summary: str, memory_type: str = "profile") -> MemoryItem:
    return MemoryItem(
        id=uuid4(),
        user_id=1,
        memory_type=memory_type,
        summary=summary,
        embedding=[0.1],
        status="active",
        source_ref="session:1:1#msg:0",
    )


class FakeEmbedder:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.texts.append(text)
        return [float(len(self.texts))]


class FakeStore:
    def __init__(
        self,
        *,
        vector_groups: list[list[MemoryItem]],
        keyword_hits: list[MemoryItem],
    ) -> None:
        self.vector_groups = list(vector_groups)
        self.keyword_hits = list(keyword_hits)
        self.vector_calls: list[dict] = []
        self.keyword_calls: list[dict] = []

    async def vector_search(self, **kwargs):
        self.vector_calls.append(kwargs)
        if not self.vector_groups:
            return []
        return self.vector_groups.pop(0)

    async def keyword_search(self, **kwargs):
        self.keyword_calls.append(kwargs)
        return list(self.keyword_hits)


async def _aux_queries(_query: str) -> list[str]:
    return ["用户家打印机型号是 Brother HL-L2460DW"]


async def _no_aux_queries(_query: str) -> list[str]:
    return []


def _engine(store: FakeStore, embedder: FakeEmbedder, aux_builder):
    return DefaultMemoryEngine(
        store=store,  # type: ignore[arg-type]
        embedder=embedder,  # type: ignore[arg-type]
        session_store=object(),  # type: ignore[arg-type]
        aux_query_builder=aux_builder,
    )


async def test_passive_and_explicit_use_same_hybrid_chain():
    raw_hit = _memory("用户家里有一台打印机")
    aux_hit = _memory("用户家打印机型号是 Brother HL-L2460DW")
    keyword_hit = _memory("用户家打印机型号是 Brother HL-L2460DW")

    passive_store = FakeStore(
        vector_groups=[[raw_hit], [aux_hit]],
        keyword_hits=[keyword_hit],
    )
    passive_embedder = FakeEmbedder()
    passive_engine = _engine(passive_store, passive_embedder, _aux_queries)

    passive = await passive_engine.retrieve(
        MemoryRetrieveRequest(
            query="我家打印机是什么型号？",
            scope=MemoryScope(user_id=1, chat_id=1),
            top_k=5,
            memory_types=["profile"],
        )
    )

    explicit_store = FakeStore(
        vector_groups=[[raw_hit], [aux_hit]],
        keyword_hits=[keyword_hit],
    )
    explicit_embedder = FakeEmbedder()
    explicit_engine = _engine(explicit_store, explicit_embedder, _aux_queries)

    explicit = await explicit_engine.retrieve_explicit(
        ExplicitRetrievalRequest(
            query="我家打印机是什么型号？",
            scope=MemoryScope(user_id=1, chat_id=1),
            memory_type="profile",
            limit=5,
        )
    )

    assert passive.trace["retrieval_mode"] == "hybrid_rrf"
    assert explicit.trace["retrieval_mode"] == "hybrid_rrf"
    assert passive.trace["aux_queries"] == explicit.trace["aux_queries"]
    assert "Brother HL-L2460DW" in passive.text_block
    assert passive.trace["vector_lane_counts"] == [1, 1]
    assert explicit.trace["vector_lane_counts"] == [1, 1]
    assert len(passive_store.vector_calls) == 2
    assert len(explicit_store.vector_calls) == 2
    assert passive_store.keyword_calls[0]["terms"] == "我家打印机是什么型号？"
    assert explicit_store.keyword_calls[0]["terms"] == "我家打印机是什么型号？"
    assert explicit.items[0]["rrf_score"] > 0
    print("test_passive_and_explicit_use_same_hybrid_chain: PASS")


async def test_explicit_recall_rrf_promotes_dual_lane_hit():
    vector_only = _memory("用户家里有一台打印机")
    both = _memory("用户家打印机型号是 Brother HL-L2460DW")
    keyword_only = _memory("Brother HL-L2460DW 是用户家打印机型号")

    store = FakeStore(
        vector_groups=[[vector_only, both]],
        keyword_hits=[both, keyword_only],
    )
    engine = _engine(store, FakeEmbedder(), _no_aux_queries)

    result = await engine.retrieve_explicit(
        ExplicitRetrievalRequest(
            query="Brother HL-L2460DW",
            scope=MemoryScope(user_id=1, chat_id=1),
            memory_type="profile",
            limit=3,
        )
    )

    summaries = [item["summary"] for item in result.items]
    assert summaries[0] == both.summary
    assert result.items[0]["lanes"] == ["vector", "keyword"]
    assert result.trace["fusion"] == "rrf"
    assert result.trace["vector_count"] == 2
    assert result.trace["keyword_count"] == 2
    print("test_explicit_recall_rrf_promotes_dual_lane_hit: PASS")


async def main():
    await test_passive_and_explicit_use_same_hybrid_chain()
    await test_explicit_recall_rrf_promotes_dual_lane_hit()
    print("\nAll memory engine retrieval tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
