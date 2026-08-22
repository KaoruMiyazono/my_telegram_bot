import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.core.event_bus import EventBus


async def test_subscribe_and_emit():
    bus = EventBus()
    results = []

    def sync_handler(value: int) -> None:
        results.append(("sync", value))

    async def async_handler(value: int) -> None:
        await asyncio.sleep(0)
        results.append(("async", value))

    bus.subscribe("test_event", sync_handler)
    bus.subscribe("test_event", async_handler)

    await bus.emit("test_event", value=42)

    assert results == [("sync", 42), ("async", 42)]
    print("test_subscribe_and_emit: PASS")


async def test_handler_exception():
    bus = EventBus()
    results = []

    def failing_handler() -> None:
        raise RuntimeError("handler error")

    def working_handler() -> None:
        results.append("ok")

    bus.subscribe("error_event", failing_handler)
    bus.subscribe("error_event", working_handler)

    await bus.emit("error_event")

    assert results == ["ok"]
    print("test_handler_exception: PASS")


async def test_no_subscribers():
    bus = EventBus()
    # Should not raise
    await bus.emit("nonexistent_event")
    print("test_no_subscribers: PASS")


@dataclass
class GateCtx:
    value: int
    trace: list[str]


async def test_typed_gate_priority_and_mutation():
    bus = EventBus()

    async def later(ctx: GateCtx):
        ctx.trace.append("later")
        ctx.value *= 2
        return ctx

    def first(ctx: GateCtx):
        ctx.trace.append("first")
        ctx.value += 3
        return ctx

    bus.on(GateCtx, later, priority=0)
    bus.on(GateCtx, first, priority=10)

    result = await bus.emit(GateCtx(value=2, trace=[]))

    assert result.value == 10
    assert result.trace == ["first", "later"]
    print("test_typed_gate_priority_and_mutation: PASS")


async def test_typed_tap_observe_is_fail_open():
    bus = EventBus()
    seen = []

    def failing(ctx: GateCtx):
        seen.append("failing")
        raise RuntimeError("boom")

    async def working(ctx: GateCtx):
        seen.append(ctx.value)

    bus.observe(GateCtx, failing, priority=10)
    bus.observe(GateCtx, working, priority=0)

    await bus.observe(GateCtx(value=7, trace=[]))

    assert seen == ["failing", 7]
    print("test_typed_tap_observe_is_fail_open: PASS")


async def main():
    await test_subscribe_and_emit()
    await test_handler_exception()
    await test_no_subscribers()
    await test_typed_gate_priority_and_mutation()
    await test_typed_tap_observe_is_fail_open()
    print("\nAll tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
