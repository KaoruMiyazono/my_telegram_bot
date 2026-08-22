import pytest

from src.lifecycle.context import (
    BeforeTurnCtx,
    AfterTurnCtx,
    BeforeReasoningCtx,
    AfterReasoningCtx,
    BeforeStepCtx,
    AfterStepCtx,
    MemoryRetrieveCtx,
    MemoryStoreCtx,
    SkillCallCtx,
    SessionCtx,
)


def test_before_turn_ctx():
    """测试 BeforeTurnCtx."""
    ctx = BeforeTurnCtx(
        session_key="session123",
        channel="telegram",
        chat_id="chat456",
        content="hello",
        retrieved_memory_block="old context",
    )

    assert ctx.session_key == "session123"
    assert ctx.channel == "telegram"
    assert ctx.chat_id == "chat456"
    assert ctx.content == "hello"
    assert ctx.retrieved_memory_block == "old context"
    assert ctx.skill_names == []


def test_before_turn_ctx_with_skills():
    """测试带 skill_names 的 BeforeTurnCtx."""
    ctx = BeforeTurnCtx(
        session_key="session123",
        channel="telegram",
        chat_id="chat456",
        content="hello",
        retrieved_memory_block="old context",
        skill_names=["skill1", "skill2"],
    )

    assert ctx.skill_names == ["skill1", "skill2"]
    assert len(ctx.skill_names) == 2


def test_after_turn_ctx():
    """测试 AfterTurnCtx."""
    ctx = AfterTurnCtx(
        session_key="session123",
        channel="telegram",
        chat_id="chat456",
        user_content="hello",
        bot_response="hi there",
        tokens_used=150,
    )

    assert ctx.session_key == "session123"
    assert ctx.user_content == "hello"
    assert ctx.bot_response == "hi there"
    assert ctx.tokens_used == 150
    assert ctx.skill_called is None


def test_after_turn_ctx_with_skill():
    """测试带 skill_called 的 AfterTurnCtx."""
    ctx = AfterTurnCtx(
        session_key="session123",
        channel="telegram",
        chat_id="chat456",
        user_content="hello",
        bot_response="hi there",
        skill_called="weather",
    )

    assert ctx.skill_called == "weather"


def test_before_reasoning_ctx():
    """测试 BeforeReasoningCtx."""
    ctx = BeforeReasoningCtx(
        session_key="session123",
        user_content="what's the weather?",
        memory_block="user likes sunny days",
        system_prompt="You are a helpful assistant.",
    )

    assert ctx.session_key == "session123"
    assert ctx.user_content == "what's the weather?"
    assert ctx.memory_block == "user likes sunny days"
    assert ctx.system_prompt == "You are a helpful assistant."
    assert ctx.skill_context == {}


def test_before_reasoning_ctx_with_skill():
    """测试带 skill_context 的 BeforeReasoningCtx."""
    ctx = BeforeReasoningCtx(
        session_key="session123",
        user_content="what's the weather?",
        memory_block="",
        skill_context={"skill_name": "weather", "location": "Beijing"},
    )

    assert ctx.skill_context["skill_name"] == "weather"
    assert ctx.skill_context["location"] == "Beijing"


def test_before_step_ctx():
    """测试 BeforeStepCtx."""
    ctx = BeforeStepCtx(
        session_key="session123",
        step_index=0,
        total_steps=3,
        current_thought="first thought",
        previous_results=[],
    )

    assert ctx.step_index == 0
    assert ctx.total_steps == 3
    assert ctx.current_thought == "first thought"
    assert ctx.previous_results == []


def test_after_step_ctx():
    """测试 AfterStepCtx."""
    ctx = AfterStepCtx(
        session_key="session123",
        step_index=0,
        step_result="intermediate answer",
        should_continue=True,
    )

    assert ctx.step_index == 0
    assert ctx.step_result == "intermediate answer"
    assert ctx.should_continue is True


def test_memory_retrieve_ctx():
    """测试 MemoryRetrieveCtx."""
    ctx = MemoryRetrieveCtx(
        session_key="session123",
        query="what did I say yesterday?",
        top_k=10,
        filters={"date_range": "last_7_days"},
    )

    assert ctx.session_key == "session123"
    assert ctx.query == "what did I say yesterday?"
    assert ctx.top_k == 10
    assert ctx.filters["date_range"] == "last_7_days"


def test_memory_store_ctx():
    """测试 MemoryStoreCtx."""
    ctx = MemoryStoreCtx(
        session_key="session123",
        content="important information",
        memory_type="user_preference",
        metadata={"category": "settings"},
    )

    assert ctx.session_key == "session123"
    assert ctx.content == "important information"
    assert ctx.memory_type == "user_preference"
    assert ctx.metadata["category"] == "settings"


def test_skill_call_ctx():
    """测试 SkillCallCtx."""
    ctx = SkillCallCtx(
        session_key="session123",
        skill_name="weather",
        arguments={"city": "Beijing", "units": "celsius"},
    )

    assert ctx.session_key == "session123"
    assert ctx.skill_name == "weather"
    assert ctx.arguments["city"] == "Beijing"
    assert ctx.tool_id is None


def test_session_ctx():
    """测试 SessionCtx."""
    ctx = SessionCtx(
        session_key="session123",
        user_id="user456",
        channel="telegram",
        chat_id="chat789",
        created_at="2026-01-01T00:00:00Z",
        turn_count=5,
    )

    assert ctx.session_key == "session123"
    assert ctx.user_id == "user456"
    assert ctx.channel == "telegram"
    assert ctx.chat_id == "chat789"
    assert ctx.created_at == "2026-01-01T00:00:00Z"
    assert ctx.turn_count == 5
    assert ctx.state == {}
    assert ctx.metadata == {}


def test_session_ctx_with_state_and_metadata():
    """测试带 state 和 metadata 的 SessionCtx."""
    ctx = SessionCtx(
        session_key="session123",
        user_id="user456",
        channel="telegram",
        chat_id="chat789",
        created_at="2026-01-01T00:00:00Z",
        state={"last_topic": "weather"},
        metadata={"language": "zh-CN"},
    )

    assert ctx.state["last_topic"] == "weather"
    assert ctx.metadata["language"] == "zh-CN"
