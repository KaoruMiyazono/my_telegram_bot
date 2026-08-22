import pytest
from datetime import datetime

from src.lifecycle.types import (
    UserMessage,
    BotMessage,
    BeforeTurnInput,
    BeforeTurnOutput,
    BeforeReasoningInput,
    BeforeReasoningOutput,
    BeforeStepInput,
    BeforeStepOutput,
    AfterStepInput,
    AfterStepOutput,
    AfterReasoningInput,
    AfterReasoningOutput,
    AfterTurnInput,
    AfterTurnOutput,
)


def test_user_message():
    """测试 UserMessage."""
    msg = UserMessage(user_id="123", content="hello", message_id="msg1")

    assert msg.user_id == "123"
    assert msg.content == "hello"
    assert msg.message_id == "msg1"
    assert isinstance(msg.timestamp, datetime)
    assert msg.metadata == {}


def test_user_message_with_metadata():
    """测试带 metadata 的 UserMessage."""
    msg = UserMessage(
        user_id="123",
        content="hello",
        message_id="msg1",
        metadata={"source": "telegram"},
    )

    assert msg.metadata["source"] == "telegram"


def test_bot_message():
    """测试 BotMessage."""
    msg = BotMessage(content="hi there")

    assert msg.content == "hi there"
    assert msg.metadata == {}


def test_before_turn_input():
    """测试 BeforeTurnInput."""
    user_msg = UserMessage(user_id="123", content="hello", message_id="msg1")
    input_data = BeforeTurnInput(user_message=user_msg)

    assert input_data.user_message == user_msg
    assert input_data.context == {}


def test_before_turn_output():
    """测试 BeforeTurnOutput."""
    user_msg = UserMessage(user_id="123", content="hello", message_id="msg1")
    output = BeforeTurnOutput(
        processed_message=user_msg,
        retrieval_query="hello world",
        context_updates={"key": "value"},
    )

    assert output.processed_message == user_msg
    assert output.retrieval_query == "hello world"
    assert output.context_updates["key"] == "value"


def test_before_reasoning_input():
    """测试 BeforeReasoningInput."""
    user_msg = UserMessage(user_id="123", content="hello", message_id="msg1")
    input_data = BeforeReasoningInput(
        user_message=user_msg,
        retrieved_memories=[{"content": "old memory"}],
    )

    assert input_data.user_message == user_msg
    assert len(input_data.retrieved_memories) == 1
    assert input_data.retrieved_memories[0]["content"] == "old memory"


def test_before_step_input():
    """测试 BeforeStepInput."""
    input_data = BeforeStepInput(
        step_index=0,
        total_steps=3,
        current_thought="think step 1",
    )

    assert input_data.step_index == 0
    assert input_data.total_steps == 3
    assert input_data.current_thought == "think step 1"


def test_after_step_output():
    """测试 AfterStepOutput."""
    output = AfterStepOutput(
        processed_output="step result",
        should_continue=True,
        step_context_updates={"partial": "data"},
    )

    assert output.processed_output == "step result"
    assert output.should_continue is True
    assert output.step_context_updates["partial"] == "data"


def test_after_reasoning_output():
    """测试 AfterReasoningOutput."""
    output = AfterReasoningOutput(
        bot_message=BotMessage(content="final answer"),
        memories_to_store=[{"content": "new memory"}],
    )

    assert output.bot_message.content == "final answer"
    assert len(output.memories_to_store) == 1


def test_after_turn_output():
    """测试 AfterTurnOutput."""
    bot_msg = BotMessage(content="response")
    output = AfterTurnOutput(
        should_send=True,
        final_message=bot_msg,
    )

    assert output.should_send is True
    assert output.final_message == bot_msg
