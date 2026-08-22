import asyncio
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["TG_BOT_TOKEN"] = "test_token"
os.environ["DEEPSEEK_API_KEY"] = "test_key"
os.environ["ALIYUN_DASHSCOPE_API_KEY"] = "test_key"
os.environ["DATABASE_PATH"] = tempfile.mktemp(suffix=".db")
os.environ["DEEPSEEK_BASE_URL"] = "https://api.test.com"
os.environ["LLM_MODEL"] = "test-model"

from agent.pipeline.invalidation_worker import InvalidationWorker
from memory.embedder import Embedder
from memory.store import MemoryStore
from persistence.database import init_db


def _status_by_id(*ids) -> dict[str, str]:
    conn = sqlite3.connect(os.environ["DATABASE_PATH"])
    placeholders = ",".join(["?"] * len(ids))
    rows = conn.execute(
        f"SELECT id, status FROM memory_items WHERE id IN ({placeholders})",
        [str(i) for i in ids],
    ).fetchall()
    conn.close()
    return {row[0]: row[1] for row in rows}


async def test_invalidation_supersedes_corrected_memory():
    init_db()
    with patch.object(Embedder, "embed", new=AsyncMock(return_value=[0.2] * 1024)):
        embedder = Embedder()
        store = MemoryStore(embedder)
        old = await store.upsert_item(
            "preference",
            "用户喜欢喝咖啡。",
            user_id=42,
            source_ref="session:42:42",
        )
        unrelated = await store.upsert_item(
            "profile",
            "用户住在北京。",
            user_id=42,
            source_ref="session:42:42",
        )

        worker = InvalidationWorker(store, embedder)
        worker._extract_invalidation_topics = AsyncMock(
            return_value=(["用户的饮品偏好"], 800)
        )
        worker._check_invalidate = AsyncMock(
            return_value=([str(old.id)], 700)
        )

        superseded = await worker.run(
            user_msg="不对，我喜欢茶。",
            agent_response="收到。",
            tool_calls=[],
            user_id=42,
            chat_id=42,
            source_ref="session:42:42",
        )

        statuses = _status_by_id(old.id, unrelated.id)
        assert superseded == [str(old.id)]
        assert statuses[str(old.id)] == "superseded"
        assert statuses[str(unrelated.id)] == "active"
        print("test_invalidation_supersedes_corrected_memory: PASS")


async def test_invalidation_protects_current_memorize_result():
    init_db()
    with patch.object(Embedder, "embed", new=AsyncMock(return_value=[0.3] * 1024)):
        embedder = Embedder()
        store = MemoryStore(embedder)
        old = await store.upsert_item("preference", "用户喜欢喝咖啡。", user_id=43)
        new = await store.upsert_item("preference", "用户喜欢喝茶。", user_id=43)

        worker = InvalidationWorker(store, embedder)
        worker._extract_invalidation_topics = AsyncMock(
            return_value=(["用户的饮品偏好"], 800)
        )
        worker._check_invalidate = AsyncMock(
            return_value=([str(old.id), str(new.id)], 700)
        )

        superseded = await worker.run(
            user_msg="不对，我喜欢茶。",
            agent_response="已记住。",
            tool_calls=[
                {
                    "function": {"name": "memorize"},
                    "result": f'{{"status":"saved","item_id":"{new.id}"}}',
                }
            ],
            user_id=43,
            chat_id=43,
            source_ref="session:43:43",
        )

        statuses = _status_by_id(old.id, new.id)
        assert superseded == [str(old.id)]
        assert statuses[str(old.id)] == "superseded"
        assert statuses[str(new.id)] == "active"
        print("test_invalidation_protects_current_memorize_result: PASS")


async def test_invalidation_skips_when_extractor_returns_no_topics():
    init_db()
    with patch.object(Embedder, "embed", new=AsyncMock(return_value=[0.4] * 1024)):
        embedder = Embedder()
        store = MemoryStore(embedder)
        worker = InvalidationWorker(store, embedder)
        worker._extract_invalidation_topics = AsyncMock(return_value=([], 800))

        result = await worker.run(
            user_msg="我今天喝了茶。",
            agent_response="听起来不错。",
            tool_calls=[],
            user_id=44,
            chat_id=44,
            source_ref="session:44:44",
        )

        assert result == []
        worker._extract_invalidation_topics.assert_awaited_once()
        print("test_invalidation_skips_when_extractor_returns_no_topics: PASS")


async def main():
    await test_invalidation_supersedes_corrected_memory()
    await test_invalidation_protects_current_memorize_result()
    await test_invalidation_skips_when_extractor_returns_no_topics()
    print("\nAll invalidation tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
