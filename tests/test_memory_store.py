import asyncio
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["TG_BOT_TOKEN"] = "test_token"
os.environ["DEEPSEEK_API_KEY"] = "test_deepseek_key"
os.environ["ALIYUN_DASHSCOPE_API_KEY"] = "test_dashscope_key"
os.environ["DATABASE_PATH"] = tempfile.mktemp(suffix=".db")

from memory.embedder import Embedder
from memory.store import MemoryStore
from persistence.database import init_db


async def test_upsert_and_vector_search():
    """Test inserting memory and retrieving it via vector search."""
    init_db()

    # Mock embedder to return consistent embeddings
    with patch.object(Embedder, "embed", new=AsyncMock()) as mock_embed:
        # Different summaries get different embeddings
        mock_embed.side_effect = [
            [0.1] * 1024,  # First memory
            [0.9] * 1024,  # Similar query vector
        ]

        store = MemoryStore(Embedder())
        user_id = 12345

        # Insert a memory
        memory = await store.upsert_item(
            memory_type="fact",
            summary="用户喜欢吃苹果",
            user_id=user_id,
        )

        print(f"Inserted memory: {memory.id}, summary: {memory.summary}")

        # Search with similar vector
        results = await store.vector_search(
            query_vec=[0.9] * 1024,
            user_id=user_id,
            top_k=5,
        )

        assert len(results) >= 1
        assert results[0].summary == "用户喜欢吃苹果"
        assert results[0].status == "active"
        print(f"Found memory: {results[0].summary}")
        print("test_upsert_and_vector_search: PASS")


async def test_supersede():
    """Test superseding old memories."""
    init_db()

    with patch.object(Embedder, "embed", new=AsyncMock(return_value=[0.5] * 1024)):
        store = MemoryStore(Embedder())
        user_id = 12345

        # Insert two memories
        mem1 = await store.upsert_item("fact", "旧记忆1", user_id)
        mem2 = await store.upsert_item("fact", "旧记忆2", user_id)
        new_mem = await store.upsert_item("fact", "新合并记忆", user_id)

        print(f"Created memories: {mem1.id}, {mem2.id}, {new_mem.id}")

        # Supersede old memories
        await store.supersede(old_ids=[mem1.id, mem2.id], new_id=new_mem.id)

        # Direct query to check superseded status
        db_path = os.environ["DATABASE_PATH"]
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, status FROM memory_items WHERE id IN (?, ?, ?)",
            (str(mem1.id), str(mem2.id), str(new_mem.id)),
        )
        statuses = {row[0]: row[1] for row in cursor.fetchall()}
        conn.close()

        assert statuses[str(mem1.id)] == "superseded"
        assert statuses[str(mem2.id)] == "superseded"
        assert statuses[str(new_mem.id)] == "active"

        # Vector search should only return active memories
        results = await store.vector_search(
            query_vec=[0.5] * 1024,
            user_id=user_id,
            top_k=10,
        )
        result_ids = [r.id for r in results]
        assert new_mem.id in result_ids
        assert mem1.id not in result_ids
        assert mem2.id not in result_ids

        history_results = await store.vector_search(
            query_vec=[0.5] * 1024,
            user_id=user_id,
            top_k=10,
            include_superseded=True,
        )
        history_ids = [r.id for r in history_results]
        assert mem1.id in history_ids
        assert mem2.id in history_ids

        print("test_supersede: PASS")


async def test_keyword_search():
    """Test keyword search with LIKE."""
    init_db()

    with patch.object(Embedder, "embed", new=AsyncMock(return_value=[0.3] * 1024)):
        store = MemoryStore(Embedder())
        user_id = 12345

        # Insert memories
        await store.upsert_item("fact", "用户名字叫张三", user_id)
        await store.upsert_item("fact", "用户喜欢吃苹果", user_id)
        await store.upsert_item("fact", "今天是星期五", user_id)

        # Keyword search
        results = await store.keyword_search(terms="张三", user_id=user_id)

        assert len(results) == 1
        assert "张三" in results[0].summary
        print(f"Keyword search result: {results[0].summary}")
        print("test_keyword_search: PASS")


async def main():
    await test_upsert_and_vector_search()
    await test_supersede()
    await test_keyword_search()
    print("\nAll memory store tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
