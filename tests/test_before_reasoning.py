import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["TG_BOT_TOKEN"] = "test_token"
os.environ["DEEPSEEK_API_KEY"] = "test_deepseek_key"
os.environ["ALIYUN_DASHSCOPE_API_KEY"] = "test_aliyun_key"
os.environ["DATABASE_PATH"] = tempfile.mktemp(suffix=".db")

from agent.core.types import BeforeTurnCtx, InboundMessage, MemoryItem, Session
from agent.pipeline.phases.before_reasoning import BeforeReasoningPhase
from datetime import datetime
from uuid import uuid4

from memory.markdown_store import MarkdownMemoryStore


async def test_preheat():
    """Test preheat is a no-op."""
    phase = BeforeReasoningPhase()
    await phase.preheat()  # Should not raise
    print("test_preheat: PASS")


async def test_build_ctx_with_memories():
    """Test build_ctx includes memories in system prompt."""
    phase = BeforeReasoningPhase()

    # Create mock memories
    memories = [
        MemoryItem(
            id=uuid4(),
            user_id=123,
            memory_type="fact",
            summary="用户喜欢吃苹果",
            embedding=None,
            status="active",
            source_ref=None,
        ),
        MemoryItem(
            id=uuid4(),
            user_id=123,
            memory_type="preference",
            summary="用户喜欢红色",
            embedding=None,
            status="active",
            source_ref=None,
        ),
    ]

    # Create BeforeTurnCtx with session history
    session = Session(
        user_id=123,
        chat_id=456,
        messages=[
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！有什么我可以帮你的吗？"},
        ],
    )

    inbound = InboundMessage(user_id=123, chat_id=456, content="我喜欢什么水果？")
    turn_ctx = BeforeTurnCtx(
        inbound_message=inbound,
        session=session,
        retrieved_memories=memories,
        retrieved_memory_block="- 用户喜欢吃苹果\n- 用户喜欢红色",
    )

    # Build context
    reasoning_ctx = await phase.build_ctx(turn_ctx)

    # Verify messages structure
    assert len(reasoning_ctx.messages) == 4  # system + 2 history + user
    assert reasoning_ctx.messages[0]["role"] == "system"

    # Verify system prompt contains memory summaries
    system_prompt = reasoning_ctx.messages[0]["content"]
    assert "用户喜欢吃苹果" in system_prompt
    assert "用户喜欢红色" in system_prompt
    assert "记忆" in system_prompt

    # Verify last message is current user input
    assert reasoning_ctx.messages[-1]["role"] == "user"
    assert reasoning_ctx.messages[-1]["content"] == "我喜欢什么水果？"

    # Verify tools are included
    assert len(reasoning_ctx.tools) >= 1
    tool_names = [t["function"]["name"] for t in reasoning_ctx.tools]
    assert "memorize" in tool_names
    assert "recall_memory" in tool_names
    assert "search_messages" in tool_names

    print(f"System prompt:\n{system_prompt}")
    print("test_build_ctx_with_memories: PASS")


async def test_build_ctx_no_memories():
    """Test build_ctx works without memories."""
    phase = BeforeReasoningPhase()

    session = Session(user_id=123, chat_id=456, messages=[])
    inbound = InboundMessage(user_id=123, chat_id=456, content="你好")
    turn_ctx = BeforeTurnCtx(
        inbound_message=inbound,
        session=session,
        retrieved_memories=[],
    )

    reasoning_ctx = await phase.build_ctx(turn_ctx)

    # Should have default system prompt
    system_prompt = reasoning_ctx.messages[0]["content"]
    assert "友好的 AI 助手" in system_prompt

    # Verify messages
    assert len(reasoning_ctx.messages) == 2  # system + user
    assert reasoning_ctx.messages[-1]["content"] == "你好"

    print("test_build_ctx_no_memories: PASS")


async def test_benchmark_prompt_requires_memory_tools():
    """Test benchmark mode adds Akashic-style mandatory memory instructions."""
    phase = BeforeReasoningPhase(benchmark_mode=True)

    session = Session(user_id=123, chat_id=456, messages=[])
    inbound = InboundMessage(user_id=123, chat_id=456, content="我是什么职业？")
    turn_ctx = BeforeTurnCtx(
        inbound_message=inbound,
        session=session,
        retrieved_memories=[],
    )

    reasoning_ctx = await phase.build_ctx(turn_ctx)
    system_prompt = reasoning_ctx.messages[0]["content"]

    assert "Benchmark Mode" in system_prompt
    assert "必须先调用 recall_memory" in system_prompt
    assert "必须继续调用 search_messages" in system_prompt
    assert "必须先调用 fetch_messages" in system_prompt
    assert "recall_memory 返回的是摘要线索，不是原文证据" in system_prompt
    assert "禁止只凭 recall 摘要或 search 预览直接作答" in system_prompt

    recall_tool = next(
        t for t in reasoning_ctx.tools
        if t["function"]["name"] == "recall_memory"
    )
    assert "不是原文证据" in recall_tool["function"]["description"]
    memory_types = recall_tool["function"]["parameters"]["properties"]["memory_type"]["enum"]
    assert "profile" in memory_types
    assert "procedure" in memory_types

    fetch_tool = next(
        t for t in reasoning_ctx.tools
        if t["function"]["name"] == "fetch_messages"
    )
    fetch_desc = fetch_tool["function"]["description"]
    assert "唯一可以直接作为最终证据的工具" in fetch_desc
    assert "不要猜" in fetch_desc
    assert "source_refs" in fetch_tool["function"]["parameters"]["properties"]

    print("test_benchmark_prompt_requires_memory_tools: PASS")


async def test_recent_context_prompt_block_strips_recent_turns():
    """Test P6 injects only stable RECENT_CONTEXT.md sections."""
    recent_context = """# Recent Context

## Compression
- 用户最近在迁移 Akashic 记忆架构。

## Ongoing Threads
- P6 只注入稳定摘要。

## User State
- 用户希望保持 eval 链路可比。

## Recent Turns
<!-- a-preview = assistant reply preview only -->
[user] 这句不能进入 prompt
[a-preview] 这句也不能进入 prompt
"""
    phase = BeforeReasoningPhase(
        recent_context_reader=lambda user_id: recent_context,
    )

    session = Session(user_id=123, chat_id=456, messages=[])
    inbound = InboundMessage(user_id=123, chat_id=456, content="继续")
    turn_ctx = BeforeTurnCtx(
        inbound_message=inbound,
        session=session,
        retrieved_memories=[],
    )

    reasoning_ctx = await phase.build_ctx(turn_ctx)
    system_prompt = reasoning_ctx.messages[0]["content"]
    section_names = [section.name for section in reasoning_ctx.prompt_sections]

    assert "recent_context" in section_names
    assert "## Compression" in system_prompt
    assert "用户最近在迁移 Akashic 记忆架构" in system_prompt
    assert "## Ongoing Threads" in system_prompt
    assert "## User State" in system_prompt
    assert "## Recent Turns" not in system_prompt
    assert "这句不能进入 prompt" not in system_prompt
    assert "这句也不能进入 prompt" not in system_prompt
    print("test_recent_context_prompt_block_strips_recent_turns: PASS")


async def test_memory_profile_prompt_blocks_inject_self_and_memory_only():
    """Test P8 injects SELF.md and MEMORY.md without pending/history leakage."""
    with tempfile.TemporaryDirectory() as tmp:
        markdown = MarkdownMemoryStore(Path(tmp))
        markdown.write_self(
            123,
            "# Self Model\n\n## Persona\n- 稳定、简洁。",
        )
        markdown.write_long_term(
            123,
            "# Long-term Memory\n\n## User Preferences\n- 用户喜欢拿铁。",
        )
        markdown.append_pending_once(
            user_id=123,
            items=["- [preference] 这条 pending 不能进入 prompt"],
            source_ref="session:123:456#msg:0-1",
        )
        markdown.append_history_once(
            user_id=123,
            entries=["[2026-05-18 10:00] 这条 history 不能进入 prompt"],
            source_ref="session:123:456#msg:0-1",
        )

        phase = BeforeReasoningPhase(
            self_model_reader=markdown.read_self,
            long_term_memory_reader=markdown.read_long_term,
            recent_context_reader=markdown.read_recent_context,
        )
        session = Session(user_id=123, chat_id=456, messages=[])
        inbound = InboundMessage(user_id=123, chat_id=456, content="我喜欢喝什么？")
        turn_ctx = BeforeTurnCtx(
            inbound_message=inbound,
            session=session,
            retrieved_memories=[],
        )

        reasoning_ctx = await phase.build_ctx(turn_ctx)
        system_prompt = reasoning_ctx.messages[0]["content"]
        section_names = [section.name for section in reasoning_ctx.prompt_sections]

        assert section_names[:4] == [
            "assistant_base",
            "self_model",
            "long_term_memory",
            "recent_context",
        ]
        assert "## Self Model" in system_prompt
        assert "稳定、简洁" in system_prompt
        assert "## Long-term Memory" in system_prompt
        assert "用户喜欢拿铁" in system_prompt
        assert "这条 pending 不能进入 prompt" not in system_prompt
        assert "这条 history 不能进入 prompt" not in system_prompt
        print("test_memory_profile_prompt_blocks_inject_self_and_memory_only: PASS")


async def test_messages_format_openai():
    """Test messages conform to OpenAI format."""
    phase = BeforeReasoningPhase()

    session = Session(
        user_id=123,
        chat_id=456,
        messages=[
            {"role": "user", "content": "first message"},
            {"role": "assistant", "content": "first response"},
        ],
    )
    inbound = InboundMessage(user_id=123, chat_id=456, content="second message")
    turn_ctx = BeforeTurnCtx(
        inbound_message=inbound,
        session=session,
        retrieved_memories=[],
    )

    reasoning_ctx = await phase.build_ctx(turn_ctx)

    # Verify OpenAI message format
    for msg in reasoning_ctx.messages:
        assert "role" in msg
        assert "content" in msg
        assert msg["role"] in {"system", "user", "assistant"}
        assert isinstance(msg["content"], str)

    print("test_messages_format_openai: PASS")


async def main():
    await test_preheat()
    await test_build_ctx_with_memories()
    await test_build_ctx_no_memories()
    await test_benchmark_prompt_requires_memory_tools()
    await test_recent_context_prompt_block_strips_recent_turns()
    await test_memory_profile_prompt_blocks_inject_self_and_memory_only()
    await test_messages_format_openai()
    print("\nAll before_reasoning tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
