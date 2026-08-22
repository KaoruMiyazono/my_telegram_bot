import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["TG_BOT_TOKEN"] = "test_token"
os.environ["DEEPSEEK_API_KEY"] = "test_deepseek_key"
os.environ["ALIYUN_DASHSCOPE_API_KEY"] = "test_aliyun_key"
os.environ["DATABASE_PATH"] = tempfile.mktemp(suffix=".db")

from agent.core.event_bus import EventBus
from agent.core.types import AfterReasoningCtx, OutboundMessage, ReasonerResult, Session
from agent.pipeline.phases.after_reasoning import AfterReasoningPhase
from agent.pipeline.phases.after_turn import AfterTurnPhase
from channels.telegram.adapter import TelegramAdapter
from memory.embedder import Embedder
from memory.store import MemoryStore
from persistence.database import init_db
from uuid import UUID


class MockTelegramAdapter(TelegramAdapter):
    """Mock adapter for testing."""

    def __init__(self):
        self.sent_messages = []

    async def send(self, message: OutboundMessage) -> None:
        self.sent_messages.append(message)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


async def test_after_reasoning_build_ctx():
    """Test AfterReasoningPhase builds correct context."""
    init_db()

    with patch.object(Embedder, "embed", new=AsyncMock(return_value=[0.1] * 1024)):
        store = MemoryStore(Embedder())
        phase = AfterReasoningPhase(store)

        result = ReasonerResult(
            content="你好！有什么我可以帮你的吗？",
            tool_calls=[],
            finish_reason="stop",
        )

        session = Session(user_id=123, chat_id=456, messages=[])

        ctx = await phase.build_ctx(
            result=result,
            session=session,
            chat_id=456,
            user_id=123,
        )

        assert ctx.outbound_message.chat_id == 456
        assert ctx.outbound_message.content == "你好！有什么我可以帮你的吗？"
        assert ctx.outbound_message.format == "text"
        assert ctx.reasoner_result is result
        print("test_after_reasoning_build_ctx: PASS")


async def test_after_reasoning_persist():
    """Test raw messages are not written into the long-term vector pool."""
    init_db()
    user_id = 789

    with patch.object(Embedder, "embed", new=AsyncMock(return_value=[0.1] * 1024)):
        store = MemoryStore(Embedder())
        phase = AfterReasoningPhase(store)

        memories = await phase.persist_messages(
            session=Session(user_id=user_id, chat_id=1, messages=[]),
            user_message="你好",
            assistant_message="你好！有什么我可以帮你的吗？",
            user_id=user_id,
            chat_id=1,
        )

        assert memories == []
        print("test_after_reasoning_persist: PASS")


async def test_after_turn_integration():
    """Test AfterTurnPhase integration with EventBus and Adapter."""
    init_db()

    # Create mock adapter
    adapter = MockTelegramAdapter()

    # Create event bus
    event_bus = EventBus()

    # Track emitted events
    emitted_events = []

    async def track_event(event):
        emitted_events.append(event)

    event_bus.subscribe("turn_committed", track_event)

    # Create phase
    phase = AfterTurnPhase(event_bus, adapter)

    # Create context
    outbound = OutboundMessage(chat_id=123, content="测试消息")
    result = ReasonerResult(content="测试消息", tool_calls=[], finish_reason="stop")
    ctx = AfterReasoningCtx(reasoner_result=result, outbound_message=outbound)

    # Execute
    from uuid import uuid4
    new_memory_ids = [uuid4()]

    await phase.execute(ctx, user_id=456, new_memory_ids=new_memory_ids)

    # Verify message was sent via adapter
    assert len(adapter.sent_messages) == 1
    assert adapter.sent_messages[0].chat_id == 123
    assert adapter.sent_messages[0].content == "测试消息"

    # Verify event was emitted
    assert len(emitted_events) == 1
    event = emitted_events[0]
    assert event.user_id == 456
    assert event.outbound_message is outbound
    assert event.new_memory_ids == new_memory_ids

    print("test_after_turn_integration: PASS")


async def test_outbound_message_creation():
    """Test OutboundMessage is created correctly from ReasonerResult."""
    init_db()

    with patch.object(Embedder, "embed", new=AsyncMock(return_value=[0.1] * 1024)):
        store = MemoryStore(Embedder())
        phase = AfterReasoningPhase(store)

        # Test with different content
        for content in ["简单回复", "带\n换行\n的回复", "😊 表情符号回复"]:
            result = ReasonerResult(content=content, tool_calls=[], finish_reason="stop")
            session = Session(user_id=1, chat_id=1, messages=[])

            ctx = await phase.build_ctx(result, session, chat_id=1, user_id=1)

            assert ctx.outbound_message.content == content
            assert ctx.outbound_message.format == "text"

        print("test_outbound_message_creation: PASS")


async def main():
    await test_after_reasoning_build_ctx()
    await test_after_reasoning_persist()
    await test_after_turn_integration()
    await test_outbound_message_creation()
    print("\nAll after phases tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
