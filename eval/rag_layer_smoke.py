"""
Deterministic RAG layer smoke checks.

This is intentionally separate from the live RAG eval runner. It does not call
DeepSeek or DashScope; instead it verifies the two lifecycle behaviors that must
keep working in CI:
  1. consolidation writes PENDING first, then optimizer commits Markdown/vector
  2. invalidation retires an explicitly corrected old memory
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

os.environ.setdefault("TG_BOT_TOKEN", "test_token")
os.environ.setdefault("DEEPSEEK_API_KEY", "test_key")
os.environ.setdefault("ALIYUN_DASHSCOPE_API_KEY", "test_key")
os.environ.setdefault("DEEPSEEK_BASE_URL", "https://api.test.com")
os.environ.setdefault("LLM_MODEL", "test-model")
os.environ["DATABASE_PATH"] = tempfile.mktemp(suffix=".db")

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.core.types import Session
from agent.pipeline.consolidation_worker import ConsolidationWorker
from agent.pipeline.invalidation_worker import InvalidationWorker
from memory.markdown_store import MarkdownMemoryStore
from memory.markdown_vector_sync import MarkdownVectorSync
from memory.optimizer import MemoryOptimizer, TextResponse
from memory.store import MemoryStore
from persistence.database import init_db


class _FakeEmbedder:
    async def embed(self, text: str) -> list[float]:
        return [0.1] * 1024


def _active_rows(user_id: int) -> list[tuple[str, str, str, str | None]]:
    conn = sqlite3.connect(os.environ["DATABASE_PATH"])
    rows = conn.execute(
        """
        SELECT id, memory_type, summary, source_ref
        FROM memory_items
        WHERE user_id = ? AND status = 'active'
        ORDER BY created_at
        """,
        (user_id,),
    ).fetchall()
    conn.close()
    return rows


def _status_by_summary(user_id: int) -> dict[str, str]:
    conn = sqlite3.connect(os.environ["DATABASE_PATH"])
    rows = conn.execute(
        """
        SELECT summary, status
        FROM memory_items
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchall()
    conn.close()
    return {summary: status for summary, status in rows}


async def _check_consolidation_window() -> dict[str, object]:
    store = MemoryStore(_FakeEmbedder())
    markdown = MarkdownMemoryStore(Path(tempfile.mkdtemp(prefix="rag-smoke-memory-")))
    worker = ConsolidationWorker(
        keep_count=10,
        min_new_messages=6,
        markdown_store=markdown,
    )
    session = Session(
        user_id=7001,
        chat_id=7001,
        messages=[
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i}"}
            for i in range(16)
        ],
        last_consolidated=0,
    )
    worker._llm_extract = AsyncMock(
        return_value=[
            {
                "memory_type": "preference",
                "summary": "用户喜欢喝茶。",
            }
        ]
    )

    assert worker.should_consolidate(session)
    stable_hash_before = markdown.stable_prompt_hash(7001)
    written = await worker.consolidate(session, store, user_id=7001, chat_id=7001)
    pending = markdown.read_pending(7001)
    rows_before_optimize = _active_rows(7001)

    assert written == 1
    assert session.last_consolidated == 6
    assert rows_before_optimize == []
    assert "用户喜欢喝茶。" in pending
    assert "[↗session:7001:7001#msg:0-5]" in pending
    assert markdown.stable_prompt_hash(7001) == stable_hash_before

    provider = AsyncMock()
    provider.chat = AsyncMock(
        side_effect=[
            TextResponse(
                "# Long-term Memory\n\n"
                "## User Preferences\n"
                "- 用户喜欢喝茶。\n"
            ),
            TextResponse("# Self Model\n\n## Persona\n- 稳定、可靠。\n"),
        ]
    )
    optimizer = MemoryOptimizer(
        markdown,
        provider,
        "test-model",
        vector_sync=MarkdownVectorSync(store),
    )
    optimized = await optimizer.optimize(7001)
    rows_after_optimize = _active_rows(7001)

    assert optimized.status == "merged"
    assert markdown.read_pending(7001) == ""
    assert markdown.stable_prompt_hash(7001) != stable_hash_before
    assert len(rows_after_optimize) == 1
    assert rows_after_optimize == [
        (
            rows_after_optimize[0][0],
            "preference",
            "用户喜欢喝茶。",
            "session:7001:7001#msg:0-5",
        )
    ]
    return {
        "pending_candidates": written,
        "last_consolidated": session.last_consolidated,
        "active_before_optimizer": len(rows_before_optimize),
        "active_after_optimizer": len(rows_after_optimize),
        "optimizer_status": optimized.status,
    }


async def _check_invalidation() -> dict[str, object]:
    embedder = _FakeEmbedder()
    store = MemoryStore(embedder)
    old = await store.upsert_item(
        "preference",
        "用户喜欢喝咖啡。",
        user_id=7002,
        source_ref="session:7002:7002",
    )
    new = await store.upsert_item(
        "preference",
        "用户喜欢喝茶。",
        user_id=7002,
        source_ref="session:7002:7002",
    )

    worker = InvalidationWorker(store, embedder)
    worker._extract_invalidation_topics = AsyncMock(
        return_value=(["用户的饮品偏好"], 800)
    )
    worker._check_invalidate = AsyncMock(
        return_value=([str(old.id), str(new.id)], 700)
    )

    superseded = await worker.run(
        user_msg="不对，我喜欢茶。",
        agent_response="已更新。",
        tool_calls=[
            {
                "function": {"name": "memorize"},
                "result": json.dumps({"status": "saved", "item_id": str(new.id)}),
            }
        ],
        user_id=7002,
        chat_id=7002,
        source_ref="session:7002:7002",
    )
    statuses = _status_by_summary(7002)

    assert superseded == [str(old.id)]
    assert statuses["用户喜欢喝咖啡。"] == "superseded"
    assert statuses["用户喜欢喝茶。"] == "active"
    return {
        "superseded": superseded,
        "old_status": statuses["用户喜欢喝咖啡。"],
        "new_status": statuses["用户喜欢喝茶。"],
    }


async def main() -> None:
    init_db()
    result = {
        "consolidation": await _check_consolidation_window(),
        "invalidation": await _check_invalidation(),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("RAG layer smoke checks passed!")


if __name__ == "__main__":
    asyncio.run(main())
