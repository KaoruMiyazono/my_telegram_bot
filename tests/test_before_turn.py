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

from agent.core.types import InboundMessage, MemoryItem, Session
from agent.pipeline.phases.before_turn import BeforeTurnPhase
from memory.engine import MemoryRetrieveResult
from persistence.database import init_db
from persistence.session_store import get_session_store


class FakeMemoryEngine:
    def __init__(self, items=None, text_block: str = "", trace: dict | None = None):
        self.items = list(items or [])
        self.text_block = text_block
        self.trace = trace or {"hyde_used": False, "hypothesis": ""}
        self.requests = []

    async def retrieve(self, request):
        self.requests.append(request)
        return MemoryRetrieveResult(
            items=self.items,
            text_block=self.text_block,
            trace=self.trace,
        )


def _memory(summary: str, user_id: int, memory_type: str = "fact") -> MemoryItem:
    return MemoryItem(
        id=uuid4(),
        user_id=user_id,
        memory_type=memory_type,
        summary=summary,
        embedding=None,
        status="active",
        source_ref=f"session:{user_id}:1",
    )


async def test_acquire_session():
    """Test session acquisition creates and reuses sessions."""
    from agent.pipeline.phases.before_turn import _sessions

    # Clear sessions
    _sessions.clear()

    init_db()
    phase = BeforeTurnPhase(memory_engine=FakeMemoryEngine())

    msg1 = InboundMessage(user_id=123, chat_id=456, content="hello")
    session1 = await phase.acquire_session(msg1)

    assert session1.user_id == 123
    assert session1.chat_id == 456
    assert len(session1.messages) == 0

    # Same user/chat should return same session
    msg2 = InboundMessage(user_id=123, chat_id=456, content="world")
    session2 = await phase.acquire_session(msg2)

    assert session1 is session2
    print("test_acquire_session: PASS")


async def test_acquire_session_restores_consolidation_cursor():
    """Test session acquisition restores persisted last_consolidated."""
    from agent.pipeline.phases.before_turn import _sessions

    _sessions.clear()

    init_db()
    get_session_store().save(
        321,
        654,
        [
            {"role": "user", "content": "第一条"},
            {"role": "assistant", "content": "第二条"},
        ],
        last_consolidated=2,
    )
    phase = BeforeTurnPhase(memory_engine=FakeMemoryEngine())

    session = await phase.acquire_session(
        InboundMessage(user_id=321, chat_id=654, content="hello")
    )

    assert len(session.messages) == 2
    assert session.last_consolidated == 2
    print("test_acquire_session_restores_consolidation_cursor: PASS")


async def test_prepare_context():
    """Test memory retrieval using vector and keyword search."""
    init_db()
    user_id = 999

    engine = FakeMemoryEngine(
        [_memory("用户喜欢吃苹果", user_id)],
        text_block="- 用户喜欢吃苹果 [session:999:1]",
        trace={"hyde_used": False, "hypothesis": "", "retrieval_mode": "hybrid_rrf"},
    )
    phase = BeforeTurnPhase(memory_engine=engine)
    session = Session(user_id=user_id, chat_id=1, messages=[])

    memories = await phase.prepare_context(
        session=session,
        query_text="苹果",
        user_id=user_id,
    )

    summaries = [m.summary for m in memories]
    assert any("苹果" in s for s in summaries)
    assert phase.last_retrieved_memory_block == "- 用户喜欢吃苹果 [session:999:1]"
    assert phase.last_retrieval_trace["retrieval_mode"] == "hybrid_rrf"
    assert engine.requests[-1].scope.user_id == user_id
    assert engine.requests[-1].scope.chat_id == 1
    print(f"Found memories: {summaries}")
    print("test_prepare_context: PASS")


async def test_build_ctx():
    """Test building BeforeTurnCtx with query from session history."""
    from agent.pipeline.phases.before_turn import _sessions

    _sessions.clear()
    init_db()
    user_id = 888

    engine = FakeMemoryEngine(
        [_memory("用户喜欢吃红色水果", user_id)],
        text_block="- 用户喜欢吃红色水果 [session:888:100]",
        trace={"hyde_used": False, "hypothesis": "", "retrieval_mode": "hybrid_rrf"},
    )
    phase = BeforeTurnPhase(memory_engine=engine)

    session = Session(user_id=user_id, chat_id=100, messages=[
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好！"},
        {"role": "user", "content": "我喜欢吃水果"},
    ])
    _sessions[(user_id, 100)] = session

    inbound = InboundMessage(user_id=user_id, chat_id=100, content="特别是红色的")

    ctx = await phase.build_ctx(inbound)

    assert ctx.inbound_message is inbound
    assert ctx.session is session
    assert "我喜欢吃水果" in engine.requests[-1].query
    assert "特别是红色的" in engine.requests[-1].query
    assert len(ctx.retrieved_memories) == 1
    assert ctx.retrieved_memory_block == "- 用户喜欢吃红色水果 [session:888:100]"
    assert ctx.retrieval_trace_raw["retrieval_mode"] == "hybrid_rrf"
    print(f"Retrieved {len(ctx.retrieved_memories)} memories")
    print("test_build_ctx: PASS")


async def main():
    await test_acquire_session()
    await test_acquire_session_restores_consolidation_cursor()
    await test_prepare_context()
    await test_build_ctx()
    print("\nAll before_turn tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
