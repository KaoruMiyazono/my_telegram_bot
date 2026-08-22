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
os.environ["DEEPSEEK_BASE_URL"] = "https://api.test.com"
os.environ["LLM_MODEL"] = "test-model"

from agent.core.event_bus import EventBus
from agent.core.types import InboundMessage
from agent.pipeline.passive_turn import PassiveTurnPipeline
from agent.pipeline.phases.after_reasoning import AfterReasoningPhase
from agent.pipeline.phases.after_turn import AfterTurnPhase
from agent.pipeline.phases.before_reasoning import BeforeReasoningPhase
from agent.pipeline.phases.before_turn import BeforeTurnPhase
from agent.pipeline.reasoner import Reasoner
from agent.tools import ToolRegistry
from agent.tools.memory import register_memory_tools
from channels.telegram.adapter import TelegramAdapter
from memory.bootstrap import build_memory_runtime
from memory.embedder import Embedder
from memory.store import MemoryStore
from persistence.database import init_db
from persistence.session_store import get_session_store


class MockTelegramAdapter(TelegramAdapter):
    def __init__(self):
        self.sent_messages = []

    async def send(self, message) -> None:
        self.sent_messages.append(message)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


def _mock_choice(content: str, finish_reason="stop"):
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = []
    choice.finish_reason = finish_reason
    return choice


def _memory_wiring(embedder: Embedder, store: MemoryStore):
    runtime = build_memory_runtime(
        embedder=embedder,
        memory_store=store,
        session_store=get_session_store(),
    )
    registry = ToolRegistry()
    register_memory_tools(registry, runtime.engine)
    return runtime, registry


async def test_end_to_end_pipeline():
    """Test full pipeline from inbound message to outbound message."""
    init_db()

    # Track emitted events
    emitted_events = []
    event_bus = EventBus()
    event_bus.subscribe("turn_committed", lambda **kw: emitted_events.append(kw.get("event")))

    # Create adapter
    adapter = MockTelegramAdapter()

    # Mock embedder
    with patch.object(Embedder, "embed", new=AsyncMock(return_value=[0.1] * 1024)):
        embedder = Embedder()
        store = MemoryStore(embedder)

        # Add some test memories
        await store.upsert_item("fact", "用户名字叫张三", user_id=999)
        await store.upsert_item("preference", "用户喜欢吃苹果", user_id=999)
        memory_runtime, tool_registry = _memory_wiring(embedder, store)

        # Create phases
        before_turn = BeforeTurnPhase(memory_engine=memory_runtime.engine)
        await before_turn.preheat() if hasattr(before_turn, 'preheat') else None

        before_reasoning = BeforeReasoningPhase(
            tool_registry=tool_registry,
            self_model_reader=memory_runtime.markdown.store.read_self,
            long_term_memory_reader=memory_runtime.markdown.store.read_long_term,
            recent_context_reader=memory_runtime.markdown.store.read_recent_context,
        )
        await before_reasoning.preheat()

        # Mock reasoner
        with patch("agent.pipeline.reasoner.AsyncOpenAI") as MockClient:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.choices = [_mock_choice("Hello, I remember you.")]
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
            MockClient.return_value = mock_client

            reasoner = Reasoner(tool_registry=tool_registry, event_bus=event_bus)

        after_reasoning = AfterReasoningPhase(store)
        after_turn = AfterTurnPhase(event_bus, adapter)

        # Create pipeline
        pipeline = PassiveTurnPipeline(
            before_turn=before_turn,
            before_reasoning=before_reasoning,
            reasoner=reasoner,
            after_reasoning=after_reasoning,
            after_turn=after_turn,
            store=store,
            memory_runtime=memory_runtime,
        )

        # Create inbound message
        inbound = InboundMessage(
            user_id=999,
            chat_id=123,
            content="你好，我是谁？",
        )

        # Execute pipeline
        outbound = await pipeline.execute(inbound)

        # Verify outbound message
        assert outbound.content == "Hello, I remember you."
        assert outbound.chat_id == 123
        assert outbound.format == "text"

        # Verify message was sent via adapter
        assert len(adapter.sent_messages) == 1
        assert adapter.sent_messages[0].content == "Hello, I remember you."
        recent_context = memory_runtime.markdown.store.read_recent_context(999)
        assert "[user] 你好，我是谁？" in recent_context
        assert "[a-preview] Hello, I remember you." in recent_context

        # Verify TurnCommittedEvent was emitted
        assert len(emitted_events) == 1
        event = emitted_events[0]
        assert event.user_id == 999
        assert event.outbound_message.content == "Hello, I remember you."
        # persist_messages 已改为异步，new_memory_ids 不再同步返回

        print("test_end_to_end_pipeline: PASS")


async def test_pipeline_with_empty_session():
    """Test pipeline with no prior session history."""
    init_db()

    event_bus = EventBus()
    adapter = MockTelegramAdapter()

    with patch.object(Embedder, "embed", new=AsyncMock(return_value=[0.1] * 1024)):
        embedder = Embedder()
        store = MemoryStore(embedder)
        memory_runtime, tool_registry = _memory_wiring(embedder, store)

        before_turn = BeforeTurnPhase(memory_engine=memory_runtime.engine)
        before_reasoning = BeforeReasoningPhase(
            tool_registry=tool_registry,
            self_model_reader=memory_runtime.markdown.store.read_self,
            long_term_memory_reader=memory_runtime.markdown.store.read_long_term,
            recent_context_reader=memory_runtime.markdown.store.read_recent_context,
        )
        await before_reasoning.preheat()

        with patch("agent.pipeline.reasoner.AsyncOpenAI") as MockClient:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.choices = [_mock_choice("Hi there!")]
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
            MockClient.return_value = mock_client

            reasoner = Reasoner(tool_registry=tool_registry, event_bus=event_bus)

        after_reasoning = AfterReasoningPhase(store)
        after_turn = AfterTurnPhase(event_bus, adapter)

        pipeline = PassiveTurnPipeline(
            before_turn=before_turn,
            before_reasoning=before_reasoning,
            reasoner=reasoner,
            after_reasoning=after_reasoning,
            after_turn=after_turn,
            store=store,
            memory_runtime=memory_runtime,
        )

        inbound = InboundMessage(user_id=1, chat_id=1, content="Hello")
        outbound = await pipeline.execute(inbound)

        assert outbound.content == "Hi there!"
        assert len(adapter.sent_messages) == 1

        print("test_pipeline_with_empty_session: PASS")


async def test_pipeline_session_persistence():
    """Test that session persists across multiple turns."""
    init_db()

    event_bus = EventBus()
    adapter = MockTelegramAdapter()

    with patch.object(Embedder, "embed", new=AsyncMock(return_value=[0.1] * 1024)):
        embedder = Embedder()
        store = MemoryStore(embedder)
        memory_runtime, tool_registry = _memory_wiring(embedder, store)

        before_turn = BeforeTurnPhase(memory_engine=memory_runtime.engine)
        before_reasoning = BeforeReasoningPhase(
            tool_registry=tool_registry,
            self_model_reader=memory_runtime.markdown.store.read_self,
            long_term_memory_reader=memory_runtime.markdown.store.read_long_term,
            recent_context_reader=memory_runtime.markdown.store.read_recent_context,
        )
        await before_reasoning.preheat()

        with patch("agent.pipeline.reasoner.AsyncOpenAI") as MockClient:
            mock_client = AsyncMock()
            mock_client.chat.completions.create = AsyncMock(
                return_value=MagicMock(choices=[_mock_choice("Response")])
            )
            MockClient.return_value = mock_client

            reasoner = Reasoner(tool_registry=tool_registry, event_bus=event_bus)

        after_reasoning = AfterReasoningPhase(store)
        after_turn = AfterTurnPhase(event_bus, adapter)

        pipeline = PassiveTurnPipeline(
            before_turn=before_turn,
            before_reasoning=before_reasoning,
            reasoner=reasoner,
            after_reasoning=after_reasoning,
            after_turn=after_turn,
            store=store,
            memory_runtime=memory_runtime,
        )

        user_id, chat_id = 555, 666

        # First turn
        await pipeline.execute(InboundMessage(user_id, chat_id, "First message"))

        # Second turn - should have session history
        await pipeline.execute(InboundMessage(user_id, chat_id, "Second message"))

        # Verify session has 4 messages (2 user + 2 assistant)
        from agent.pipeline.phases.before_turn import _sessions
        session = _sessions.get((user_id, chat_id))
        assert session is not None
        assert len(session.messages) == 4
        assert session.messages[0]["content"] == "First message"
        assert session.messages[1]["content"] == "Response"
        assert session.messages[2]["content"] == "Second message"
        assert session.messages[3]["content"] == "Response"

        print("test_pipeline_session_persistence: PASS")


async def main():
    await test_end_to_end_pipeline()
    await test_pipeline_with_empty_session()
    await test_pipeline_session_persistence()
    print("\nAll pipeline integration tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
