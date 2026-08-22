"""
集成测试：验证 consolidation 窗口端到端生效

不调真实 API，MockReasoner 模拟 LLM 回复。
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["TG_BOT_TOKEN"] = "test_token"
os.environ["DEEPSEEK_API_KEY"] = "test_key"
os.environ["ALIYUN_DASHSCOPE_API_KEY"] = "test_key"
os.environ["DATABASE_PATH"] = tempfile.mktemp(suffix=".db")
os.environ["DEEPSEEK_BASE_URL"] = "https://api.test.com"
os.environ["LLM_MODEL"] = "test-model"

from agent.core.types import InboundMessage, ReasonerResult
from agent.pipeline.passive_turn import PassiveTurnPipeline
from agent.pipeline.phases.after_reasoning import AfterReasoningPhase
from agent.pipeline.phases.after_turn import AfterTurnPhase
from agent.pipeline.phases.before_reasoning import BeforeReasoningPhase
from agent.pipeline.phases.before_turn import BeforeTurnPhase, _sessions
from agent.pipeline.reasoner import Reasoner
from agent.pipeline.consolidation_worker import ConsolidationWorker
from memory.bootstrap import build_memory_runtime
from persistence.database import init_db
from persistence.session_store import get_session_store


class FakeReasoner(Reasoner):
    """不调真实 API，固定返回 mock 回复。"""
    async def run_turn(self, ctx):
        return ReasonerResult(
            content=f"这是第{len(ctx.session.messages)//2 + 1}轮回复。",
            tool_calls=[],
            finish_reason="stop",
        )


async def test_consolidation_triggers():
    """
    16 轮对话 → 验证 consolidation 触发，last_consolidated 推进。
    """
    init_db()
    _sessions.pop((1, 1), None)

    mock_embedder = AsyncMock()
    mock_embedder.embed = AsyncMock(return_value=[0.1] * 1024)

    from memory.store import MemoryStore
    store = MemoryStore(mock_embedder)
    memory_runtime = build_memory_runtime(
        embedder=mock_embedder,
        memory_store=store,
        session_store=get_session_store(),
    )

    # 追踪 consolidation 写入
    upsert_calls = []
    original_upsert = store.upsert_item

    async def tracking_upsert(**kwargs):
        upsert_calls.append(kwargs)
        return await original_upsert(**kwargs)

    store.upsert_item = tracking_upsert

    before_turn = BeforeTurnPhase(memory_engine=memory_runtime.engine)
    before_reasoning = BeforeReasoningPhase()
    reasoner = FakeReasoner()
    after_reasoning = AfterReasoningPhase(store)
    # Mock event bus
    event_bus = MagicMock()
    event_bus.emit = AsyncMock()
    after_turn = AfterTurnPhase(event_bus, None)

    consolidation = ConsolidationWorker(keep_count=10, min_new_messages=6)

    pipeline = PassiveTurnPipeline(
        before_turn=before_turn,
        before_reasoning=before_reasoning,
        reasoner=reasoner,
        after_reasoning=after_reasoning,
        after_turn=after_turn,
        store=store,
        consolidation_worker=consolidation,
        memory_runtime=memory_runtime,
    )

    # 模拟 consolidation LLM 调用（patch AsyncOpenAI）
    with patch("agent.pipeline.consolidation_worker.AsyncOpenAI") as MockClient:
        # Mock LLM 返回空 JSON（避免真实 API 调用）
        async def mock_create(**kwargs):
            resp = MagicMock()
            choice = MagicMock()
            choice.message.content = '{"profile":[],"preference":[],"event":[]}'
            resp.choices = [choice]
            return resp

        mock_client = MagicMock()
        mock_client.chat.completions.create = mock_create
        MockClient.return_value = mock_client

        # 跑 16 轮
        for i in range(16):
            msg = InboundMessage(
                user_id=1, chat_id=1,
                content=f"我是程序员，喜欢Vim。第{i+1}轮。",
            )
            await pipeline.execute(msg)
            await asyncio.sleep(0.01)

    await asyncio.sleep(0.5)

    session_key = (1, 1)
    session = _sessions.get(session_key)
    assert session is not None
    assert len(session.messages) == 32, f"Expected 32, got {len(session.messages)}"

    # keep=10, total=32 → consolidate_up_to = 22
    assert session.last_consolidated == 22, (
        f"last_consolidated should be 22, got {session.last_consolidated}"
    )
    persisted = get_session_store().load_state(1, 1)
    assert persisted is not None
    assert persisted[1] == 22

    print("test_consolidation_triggers: PASS")
    print(f"  session messages: {len(session.messages)}")
    print(f"  last_consolidated: {session.last_consolidated}")


async def test_consolidation_skips():
    """5 轮 = 10条消息，不超过 keep_count → 不触发。"""
    init_db()
    _sessions.pop((2, 2), None)

    mock_embedder = AsyncMock()
    mock_embedder.embed = AsyncMock(return_value=[0.1] * 1024)

    from memory.store import MemoryStore
    store = MemoryStore(mock_embedder)
    memory_runtime = build_memory_runtime(
        embedder=mock_embedder,
        memory_store=store,
        session_store=get_session_store(),
    )

    before_turn = BeforeTurnPhase(memory_engine=memory_runtime.engine)
    before_reasoning = BeforeReasoningPhase()
    reasoner = FakeReasoner()
    after_reasoning = AfterReasoningPhase(store)
    event_bus = MagicMock()
    event_bus.emit = AsyncMock()
    after_turn = AfterTurnPhase(event_bus, None)

    consolidation = ConsolidationWorker(keep_count=10, min_new_messages=6)
    pipeline = PassiveTurnPipeline(
        before_turn=before_turn,
        before_reasoning=before_reasoning,
        reasoner=reasoner,
        after_reasoning=after_reasoning,
        after_turn=after_turn,
        store=store,
        consolidation_worker=consolidation,
        memory_runtime=memory_runtime,
    )

    for i in range(5):
        msg = InboundMessage(user_id=2, chat_id=2, content=f"消息{i}")
        await pipeline.execute(msg)
        await asyncio.sleep(0.01)

    await asyncio.sleep(0.3)

    session = _sessions.get((2, 2))
    assert session is not None
    assert session.last_consolidated == 0, (
        f"Should be 0, got {session.last_consolidated}"
    )

    print("test_consolidation_skips: PASS")
    print(f"  session messages: {len(session.messages)}")
    print(f"  last_consolidated: {session.last_consolidated}")


if __name__ == "__main__":
    asyncio.run(test_consolidation_triggers())
    asyncio.run(test_consolidation_skips())
    print("\nAll consolidation integration tests passed!")
