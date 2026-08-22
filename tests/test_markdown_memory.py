import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["TG_BOT_TOKEN"] = "test_token"
os.environ["DEEPSEEK_API_KEY"] = "test_deepseek_key"
os.environ["ALIYUN_DASHSCOPE_API_KEY"] = "test_aliyun_key"
os.environ["DEEPSEEK_BASE_URL"] = "https://api.test.com"
os.environ["LLM_MODEL"] = "test-model"

from agent.pipeline.consolidation_worker import ConsolidationWorker
from memory.markdown_store import MarkdownMemoryStore


def test_markdown_store_creates_user_files_and_dedupes_writes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = MarkdownMemoryStore(Path(tmp))
        user_root = store.ensure_user(42)
        memory_dir = user_root / "memory"

        assert (memory_dir / "MEMORY.md").exists()
        assert (memory_dir / "SELF.md").exists()
        assert (memory_dir / "HISTORY.md").exists()
        assert (memory_dir / "PENDING.md").exists()
        assert (memory_dir / "RECENT_CONTEXT.md").exists()
        assert (user_root / "PROACTIVE_CONTEXT.md").exists()

        assert store.append_history_once(
            user_id=42,
            entries=["[2026-05-18 10:00] 用户喜欢 Python"],
            source_ref="session:42:7#msg:0-3",
        )
        assert not store.append_history_once(
            user_id=42,
            entries=["[2026-05-18 10:00] 用户喜欢 Python"],
            source_ref="session:42:7#msg:0-3",
        )
        history = (memory_dir / "HISTORY.md").read_text(encoding="utf-8")
        assert history.count("用户喜欢 Python") == 1

        assert store.append_pending_once(
            user_id=42,
            items=["- [preference] 用户喜欢 Python"],
            source_ref="session:42:7#msg:0-3",
        )
        pending_before = (memory_dir / "PENDING.md").read_text(encoding="utf-8")
        snapshot = store.snapshot_pending(42)
        assert "用户喜欢 Python" in snapshot
        assert "用户喜欢 Python" not in (memory_dir / "PENDING.md").read_text(encoding="utf-8")
        store.rollback_pending_snapshot(42)
        pending_after = (memory_dir / "PENDING.md").read_text(encoding="utf-8")
        assert "用户喜欢 Python" in pending_after
        assert pending_after.count("用户喜欢 Python") == pending_before.count("用户喜欢 Python")

        store.write_recent_turns(
            user_id=42,
            messages=[
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "你好，我在。"},
            ],
        )
        recent = store.read_recent_context(42)
        assert "[user] 你好" in recent
        assert "[a-preview] 你好，我在。" in recent

        print("test_markdown_store_creates_user_files_and_dedupes_writes: PASS")


async def test_consolidation_shadow_writes_markdown() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        markdown = MarkdownMemoryStore(Path(tmp))
        worker = ConsolidationWorker(
            keep_count=2,
            min_new_messages=1,
            markdown_store=markdown,
        )
        worker._llm_extract = _extract_profile
        session = SimpleNamespace(
            messages=[
                {"role": "user", "content": "我是后端工程师"},
                {"role": "assistant", "content": "好的"},
                {"role": "user", "content": "我喜欢 Python"},
                {"role": "assistant", "content": "记住了"},
            ],
            last_consolidated=0,
        )
        vector_store = _VectorStore()

        written = await worker.consolidate(
            session=session,
            store=vector_store,
            user_id=42,
            chat_id=7,
        )

        memory_dir = Path(tmp) / "users" / "42" / "memory"
        history = (memory_dir / "HISTORY.md").read_text(encoding="utf-8")
        pending = (memory_dir / "PENDING.md").read_text(encoding="utf-8")
        journal_files = list((memory_dir / "journal").glob("*.md"))

        assert written == 1
        assert vector_store.calls[0]["source_ref"] == "session:42:7#msg:0-1"
        assert "用户是后端工程师" in history
        assert "- [identity] 用户是后端工程师" in pending
        assert journal_files
        assert session.last_consolidated == 2

        print("test_consolidation_shadow_writes_markdown: PASS")


async def _extract_profile(conversation: str):
    return [{"memory_type": "profile", "summary": "用户是后端工程师"}]


class _VectorStore:
    def __init__(self) -> None:
        self.calls = []

    async def upsert_item(self, **kwargs):
        self.calls.append(kwargs)


async def main() -> None:
    test_markdown_store_creates_user_files_and_dedupes_writes()
    await test_consolidation_shadow_writes_markdown()
    print("\nAll markdown memory tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
