import pytest

from src.lifecycle.phase import PhaseFrame, PhaseModule
from src.lifecycle.types import (
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
    UserMessage,
    BotMessage,
)
from src.lifecycle.phases import (
    BeforeTurnPhase,
    BeforeReasoningPhase,
    BeforeStepPhase,
    AfterStepPhase,
    AfterReasoningPhase,
    AfterTurnPhase,
)


class DummyPhaseModule(PhaseModule):
    """测试用的 PhaseModule."""

    def __init__(self, key: str, value: str):
        self.key = key
        self.value = value

    async def run(self, frame: PhaseFrame) -> PhaseFrame:
        frame.set(self.key, self.value)
        return frame


@pytest.mark.asyncio
async def test_before_turn_phase_default_output():
    """测试 BeforeTurnPhase 的默认输出."""
    user_msg = UserMessage(user_id="123", content="hello", message_id="msg1")
    input_data = BeforeTurnInput(user_message=user_msg)

    phase = BeforeTurnPhase()
    frame = await phase.execute(input_data)

    assert frame.input == input_data
    assert isinstance(frame.output, BeforeTurnOutput)
    assert frame.output.processed_message == user_msg
    assert frame.output.retrieval_query == "hello"


@pytest.mark.asyncio
async def test_before_turn_phase_with_modules():
    """测试 BeforeTurnPhase 带模块."""
    user_msg = UserMessage(user_id="123", content="hello", message_id="msg1")
    input_data = BeforeTurnInput(user_message=user_msg)

    phase = BeforeTurnPhase()
    phase.add_module(DummyPhaseModule("key1", "value1"))
    phase.add_module(DummyPhaseModule("key2", "value2"))

    frame = await phase.execute(input_data)

    assert frame.get("key1") == "value1"
    assert frame.get("key2") == "value2"


@pytest.mark.asyncio
async def test_before_turn_phase_add_module_chain():
    """测试 BeforeTurnPhase 的链式调用."""
    user_msg = UserMessage(user_id="123", content="hello", message_id="msg1")
    input_data = BeforeTurnInput(user_message=user_msg)

    module = DummyPhaseModule("chain_key", "chain_value")
    phase = BeforeTurnPhase().add_module(module)

    frame = await phase.execute(input_data)

    assert frame.get("chain_key") == "chain_value"


@pytest.mark.asyncio
async def test_before_reasoning_phase_default_output():
    """测试 BeforeReasoningPhase 的默认输出."""
    user_msg = UserMessage(user_id="123", content="what's the weather?", message_id="msg1")
    input_data = BeforeReasoningInput(
        user_message=user_msg,
        retrieved_memories=[{"content": "user lives in Beijing"}],
    )

    phase = BeforeReasoningPhase()
    frame = await phase.execute(input_data)

    assert frame.input == input_data
    assert isinstance(frame.output, BeforeReasoningOutput)
    assert frame.output.llm_input == "what's the weather?"


@pytest.mark.asyncio
async def test_before_reasoning_phase_with_retrieved_memories():
    """测试 BeforeReasoningPhase 带检索的记忆."""
    user_msg = UserMessage(user_id="123", content="what's the weather?", message_id="msg1")
    memories = [
        {"content": "user lives in Beijing"},
        {"content": "user likes sunny days"},
    ]
    input_data = BeforeReasoningInput(user_message=user_msg, retrieved_memories=memories)

    phase = BeforeReasoningPhase()
    frame = await phase.execute(input_data)

    assert frame.input.retrieved_memories == memories


@pytest.mark.asyncio
async def test_before_step_phase():
    """测试 BeforeStepPhase."""
    input_data = BeforeStepInput(
        step_index=0,
        total_steps=3,
        current_thought="I need to check the weather",
    )

    phase = BeforeStepPhase()
    frame = await phase.execute(input_data)

    assert isinstance(frame.output, BeforeStepOutput)
    assert frame.output.processed_thought == "I need to check the weather"


@pytest.mark.asyncio
async def test_before_step_phase_with_modules():
    """测试 BeforeStepPhase 带模块."""
    input_data = BeforeStepInput(
        step_index=0,
        total_steps=3,
        current_thought="first step",
    )

    phase = BeforeStepPhase()
    phase.add_module(DummyPhaseModule("step_info", "processing"))

    frame = await phase.execute(input_data)

    assert frame.get("step_info") == "processing"


@pytest.mark.asyncio
async def test_after_step_phase_default_output():
    """测试 AfterStepPhase 的默认输出."""
    input_data = AfterStepInput(
        step_index=0,
        step_output="Beijing is sunny today",
    )

    phase = AfterStepPhase()
    frame = await phase.execute(input_data)

    assert isinstance(frame.output, AfterStepOutput)
    assert frame.output.processed_output == "Beijing is sunny today"
    assert frame.output.should_continue is True


@pytest.mark.asyncio
async def test_after_step_phase_with_modules():
    """测试 AfterStepPhase 带模块."""
    input_data = AfterStepInput(
        step_index=0,
        step_output="step result",
    )

    phase = AfterStepPhase()
    phase.add_module(DummyPhaseModule("result_processed", "yes"))

    frame = await phase.execute(input_data)

    assert frame.get("result_processed") == "yes"


@pytest.mark.asyncio
async def test_after_reasoning_phase_default_output():
    """测试 AfterReasoningPhase 的默认输出."""
    input_data = AfterReasoningInput(
        llm_output="The weather in Beijing is sunny today.",
    )

    phase = AfterReasoningPhase()
    frame = await phase.execute(input_data)

    assert isinstance(frame.output, AfterReasoningOutput)
    assert frame.output.bot_message.content == "The weather in Beijing is sunny today."
    assert frame.output.memories_to_store == []


@pytest.mark.asyncio
async def test_after_turn_phase_default_output():
    """测试 AfterTurnPhase 的默认输出."""
    user_msg = UserMessage(user_id="123", content="hello", message_id="msg1")
    bot_msg = BotMessage(content="hi there")

    input_data = AfterTurnInput(
        user_message=user_msg,
        bot_message=bot_msg,
    )

    phase = AfterTurnPhase()
    frame = await phase.execute(input_data)

    assert isinstance(frame.output, AfterTurnOutput)
    assert frame.output.should_send is True
    assert frame.output.final_message == bot_msg


@pytest.mark.asyncio
async def test_after_turn_phase_with_modules():
    """测试 AfterTurnPhase 带模块."""
    user_msg = UserMessage(user_id="123", content="hello", message_id="msg1")
    bot_msg = BotMessage(content="hi there")

    input_data = AfterTurnInput(
        user_message=user_msg,
        bot_message=bot_msg,
    )

    phase = AfterTurnPhase()
    phase.add_module(DummyPhaseModule("logged", "true"))

    frame = await phase.execute(input_data)

    assert frame.get("logged") == "true"
