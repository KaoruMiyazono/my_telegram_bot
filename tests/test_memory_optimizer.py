import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["TG_BOT_TOKEN"] = "test_token"
os.environ["DEEPSEEK_API_KEY"] = "test_deepseek_key"
os.environ["ALIYUN_DASHSCOPE_API_KEY"] = "test_aliyun_key"
os.environ["DEEPSEEK_BASE_URL"] = "https://api.test.com"
os.environ["LLM_MODEL"] = "test-model"

from memory.markdown_store import MarkdownMemoryStore
from memory.optimizer import MemoryOptimizer, MemoryOptimizerBusy, TextResponse
from memory.markdown_vector_sync import MarkdownVectorSyncResult


def _provider_with_responses(*responses: str):
    provider = SimpleNamespace()
    provider.chat = AsyncMock(side_effect=[TextResponse(response) for response in responses])
    return provider


def _snapshot_path(root: Path, user_id: int) -> Path:
    return root / "users" / str(user_id) / "memory" / "PENDING.snapshot.md"


class FakeVectorSync:
    def __init__(self) -> None:
        self.calls = []

    async def sync_user(self, *, markdown: MarkdownMemoryStore, user_id: int):
        self.calls.append((markdown, user_id))
        return MarkdownVectorSyncResult(
            user_id=user_id,
            parsed_count=2,
            inserted_count=1,
            skipped_count=1,
        )


async def test_optimizer_skips_empty_workspace() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = MarkdownMemoryStore(Path(tmp))
        provider = _provider_with_responses()
        optimizer = MemoryOptimizer(store, provider, "test-model")

        result = await optimizer.optimize(42)

        assert result.status == "skipped"
        provider.chat.assert_not_called()
        assert not _snapshot_path(Path(tmp), 42).exists()
        print("test_optimizer_skips_empty_workspace: PASS")


async def test_optimizer_merges_pending_and_commits_snapshot() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = MarkdownMemoryStore(root)
        store.append_pending_once(
            user_id=42,
            items=["- [preference] 用户喜欢拿铁。"],
            source_ref="session:42:7#msg:0-1",
        )
        provider = _provider_with_responses(
            "# Long-term Memory\n\n## User Preferences\n- 用户喜欢拿铁。\n",
            "# Self Model\n\n## Persona\n- 稳定、简洁。\n\n## Understanding Of User\n- 当前用户重视清晰回应。\n\n## Relationship\n- 协作关系。\n",
        )
        optimizer = MemoryOptimizer(store, provider, "test-model")

        result = await optimizer.optimize(42)

        assert result.status == "merged"
        assert result.self_updated
        assert "用户喜欢拿铁" in store.read_long_term(42)
        assert "用户喜欢拿铁" not in store.read_pending(42)
        assert not _snapshot_path(root, 42).exists()
        assert provider.chat.await_count == 2
        print("test_optimizer_merges_pending_and_commits_snapshot: PASS")


async def test_optimizer_syncs_vector_after_successful_merge() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = MarkdownMemoryStore(Path(tmp))
        store.append_pending_once(
            user_id=42,
            items=["- [preference] 用户喜欢拿铁。"],
            source_ref="session:42:7#msg:0-1",
        )
        provider = _provider_with_responses(
            "# Long-term Memory\n\n## User Preferences\n- 用户喜欢拿铁。\n",
            "# Self Model\n\n## Persona\n- 稳定。\n",
        )
        vector_sync = FakeVectorSync()
        optimizer = MemoryOptimizer(
            store,
            provider,
            "test-model",
            vector_sync=vector_sync,  # type: ignore[arg-type]
        )

        result = await optimizer.optimize(42)

        assert result.status == "merged"
        assert result.vector_parsed == 2
        assert result.vector_inserted == 1
        assert result.vector_skipped == 1
        assert result.vector_error == ""
        assert vector_sync.calls == [(store, 42)]
        print("test_optimizer_syncs_vector_after_successful_merge: PASS")


async def test_optimizer_syncs_existing_memory_without_pending() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = MarkdownMemoryStore(Path(tmp))
        store.write_long_term(42, "# Long-term Memory\n\n## User Facts\n- 用户是工程师。\n")
        provider = _provider_with_responses()
        vector_sync = FakeVectorSync()
        optimizer = MemoryOptimizer(
            store,
            provider,
            "test-model",
            vector_sync=vector_sync,  # type: ignore[arg-type]
        )

        result = await optimizer.optimize(42)

        assert result.status == "synced"
        assert result.vector_inserted == 1
        provider.chat.assert_not_called()
        assert vector_sync.calls == [(store, 42)]
        print("test_optimizer_syncs_existing_memory_without_pending: PASS")


async def test_optimizer_rolls_back_pending_when_merge_is_empty() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = MarkdownMemoryStore(root)
        store.write_long_term(42, "# Long-term Memory\n\n## User Facts\n- 旧事实。\n")
        store.append_pending_once(
            user_id=42,
            items=["- [identity] 用户是后端工程师。"],
            source_ref="session:42:7#msg:2-3",
        )
        provider = _provider_with_responses("")
        optimizer = MemoryOptimizer(store, provider, "test-model")

        result = await optimizer.optimize(42)

        assert result.status == "rolled_back"
        assert result.error == "empty_memory_merge"
        assert "旧事实" in store.read_long_term(42)
        assert "用户是后端工程师" in store.read_pending(42)
        assert not _snapshot_path(root, 42).exists()
        print("test_optimizer_rolls_back_pending_when_merge_is_empty: PASS")


async def test_optimizer_self_prompt_uses_pending_not_history() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = MarkdownMemoryStore(Path(tmp))
        store.write_self(42, "# Self Model\n\n## Persona\n- 可靠。\n")
        store.append_pending_once(
            user_id=42,
            items=["- [preference] 用户喜欢简洁回答。"],
            source_ref="session:42:7#msg:0-1",
        )
        store.append_history_once(
            user_id=42,
            entries=["[2026-05-18 10:00] 这段 HISTORY 不该进入 SELF prompt"],
            source_ref="session:42:7#msg:0-1",
        )
        provider = _provider_with_responses(
            "# Long-term Memory\n\n## User Preferences\n- 用户喜欢简洁回答。\n",
            "# Self Model\n\n## Persona\n- 可靠。\n\n## Understanding Of User\n- 当前用户喜欢直接清晰的交流。\n\n## Relationship\n- 协作关系。\n",
        )
        optimizer = MemoryOptimizer(store, provider, "test-model")

        await optimizer.optimize(42)

        self_prompt = provider.chat.await_args_list[1].kwargs["messages"][1]["content"]
        assert "- [preference] 用户喜欢简洁回答。" in self_prompt
        assert "HISTORY 不该进入 SELF prompt" not in self_prompt
        assert "当前用户喜欢直接清晰的交流" in store.read_self(42)
        print("test_optimizer_self_prompt_uses_pending_not_history: PASS")


async def test_optimizer_rejects_self_user_fact_pollution() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = MarkdownMemoryStore(Path(tmp))
        store.write_self(42, "# Self Model\n\n## Persona\n- 可靠。\n")
        store.append_pending_once(
            user_id=42,
            items=["- [identity] 用户是后端工程师。"],
            source_ref="session:42:7#msg:0-1",
        )
        provider = _provider_with_responses(
            "# Long-term Memory\n\n## User Facts\n- 用户是后端工程师。\n",
            "# Self Model\n\n## Understanding Of User\n- 用户是后端工程师。\n",
        )
        optimizer = MemoryOptimizer(store, provider, "test-model")

        result = await optimizer.optimize(42)

        assert result.status == "merged"
        assert not result.self_updated
        assert "用户是后端工程师" not in store.read_self(42)
        assert "用户是后端工程师" in store.read_long_term(42)
        print("test_optimizer_rejects_self_user_fact_pollution: PASS")


async def test_optimizer_busy_reports_error() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = MarkdownMemoryStore(Path(tmp))
        provider = _provider_with_responses()
        optimizer = MemoryOptimizer(store, provider, "test-model")
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked_optimize(user_id: int):
            started.set()
            await release.wait()

        optimizer._optimize = blocked_optimize  # type: ignore[method-assign]
        running = asyncio.create_task(optimizer.optimize(42))
        await started.wait()

        assert optimizer.is_running
        try:
            await optimizer.optimize(42)
            raise AssertionError("expected MemoryOptimizerBusy")
        except MemoryOptimizerBusy:
            pass

        release.set()
        await running
        print("test_optimizer_busy_reports_error: PASS")


async def main() -> None:
    await test_optimizer_skips_empty_workspace()
    await test_optimizer_merges_pending_and_commits_snapshot()
    await test_optimizer_syncs_vector_after_successful_merge()
    await test_optimizer_syncs_existing_memory_without_pending()
    await test_optimizer_rolls_back_pending_when_merge_is_empty()
    await test_optimizer_self_prompt_uses_pending_not_history()
    await test_optimizer_rejects_self_user_fact_pollution()
    await test_optimizer_busy_reports_error()
    print("\nAll memory optimizer tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
