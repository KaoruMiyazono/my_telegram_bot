import pytest

from src.lifecycle.phase import PhaseFrame, PhaseContext, PhaseModule, PhaseRunner, PhaseComposer
from src.lifecycle.types import BeforeTurnInput, BeforeTurnOutput, UserMessage


class DummyModule:
    """测试用的 Dummy 模块."""

    async def run(self, frame: PhaseFrame) -> PhaseFrame:
        frame.set("processed", True)
        return frame


class DummyModuleWithOutput:
    """测试用的 Dummy 模块，带有输出."""

    async def run(self, frame: PhaseFrame) -> PhaseFrame:
        frame.output = BeforeTurnOutput(
            processed_message=UserMessage(
                user_id="123",
                content="test",
                message_id="msg1",
            ),
        )
        return frame


@pytest.mark.asyncio
async def test_phase_frame_slots():
    """测试 PhaseFrame 的 slots 操作."""
    frame = PhaseFrame(input="test")

    assert frame.input == "test"
    assert frame.output is None
    assert frame.slots == {}

    frame.set("key1", "value1")
    assert frame.get("key1") == "value1"
    assert frame.has("key1") is True
    assert frame.get("key2") is None
    assert frame.get("key2", "default") == "default"

    frame.clear()
    assert frame.slots == {}


@pytest.mark.asyncio
async def test_phase_context():
    """测试 PhaseContext."""
    ctx = PhaseContext(user_id="user123", session_id="session456")

    assert ctx.user_id == "user123"
    assert ctx.session_id == "session456"

    ctx.set("key", "value")
    assert ctx.get("key") == "value"
    assert ctx.has("key") is True


@pytest.mark.asyncio
async def test_phase_module_protocol():
    """测试 PhaseModule 协议."""
    module = DummyModule()
    frame = PhaseFrame(input="test")

    result = await module.run(frame)
    assert result.get("processed") is True


@pytest.mark.asyncio
async def test_phase_runner():
    """测试 PhaseRunner."""
    runner = PhaseRunner("test_runner")
    module1 = DummyModule()
    module2 = DummyModule()

    frame = PhaseFrame(input="test")
    result = await runner.add_module(module1).add_module(module2).run(frame)

    assert result.get("processed") is True


@pytest.mark.asyncio
async def test_phase_runner_add_modules():
    """测试 PhaseRunner 的 add_modules."""
    runner = PhaseRunner("test_runner")
    modules = [DummyModule(), DummyModule(), DummyModule()]

    frame = PhaseFrame(input="test")
    result = await runner.add_modules(*modules).run(frame)

    assert result.get("processed") is True


@pytest.mark.asyncio
async def test_phase_composer():
    """测试 PhaseComposer."""
    composer = PhaseComposer("test_compose")

    runner1 = PhaseRunner("runner1").add_module(DummyModule())
    runner2 = PhaseRunner("runner2").add_module(DummyModule())

    frames = await composer.add_runner(runner1).add_runner(runner2).execute("test_input")

    assert len(frames) == 3  # initial + 2 runners
    assert all(f.get("processed") for f in frames[1:])


@pytest.mark.asyncio
async def test_phase_frame_with_generic_types():
    """测试 PhaseFrame 的泛型类型."""
    user_message = UserMessage(user_id="123", content="hello", message_id="msg1")
    frame: PhaseFrame[UserMessage, BeforeTurnOutput] = PhaseFrame(input=user_message)

    assert frame.input == user_message
    assert frame.output is None

    frame.output = BeforeTurnOutput(
        processed_message=user_message,
        retrieval_query="hello",
    )

    assert isinstance(frame.output, BeforeTurnOutput)
    assert frame.output.retrieval_query == "hello"
