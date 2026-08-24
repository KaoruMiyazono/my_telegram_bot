"""M11 acceptance tests for Passive/Proactive/Idle coordination."""

from __future__ import annotations

import asyncio

import pytest

from agent.core.envelope import envelope_from_inbound
from agent.core.message_bus import MessageBus
from agent.core.types import InboundMessage, OutboundMessage
from agent.runtime.idle_tasks import IdleTaskResult, IdleTaskRuntime, IdleTaskStore
from agent.runtime.mode_coordinator import ModeCoordinator
from agent.runtime.turn_runtime import TurnRuntime
from persistence.database import init_db
from persistence.runtime_message_store import RuntimeMessageStore


async def _eventually(predicate, *, timeout: float = 2.0) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout=timeout)


async def test_passive_pauses_idle_at_checkpoint_then_resumes(tmp_path) -> None:
    coordinator = ModeCoordinator()
    store = IdleTaskStore(tmp_path / "idle.db")
    runtime = IdleTaskRuntime(store, coordinator, poll_seconds=0.01)
    coordinator.attach_idle(runtime)
    completed_steps: list[int] = []

    async def resumable(context) -> IdleTaskResult:
        start = int(context.checkpoint.get("next_step", 0))
        for step in range(start, 4):
            context.save_checkpoint({"next_step": step})
            await asyncio.sleep(0.04)
            completed_steps.append(step)
            context.save_checkpoint({"next_step": step + 1})
        return IdleTaskResult(checkpoint={"next_step": 4})

    runtime.register("index", resumable, permission="local_maintenance")
    task = runtime.enqueue("index", trace_id="trace:idle")
    await runtime.start()
    try:
        await _eventually(lambda: store.get(task.task_id).status == "running")  # type: ignore[union-attr]
        await coordinator.enter_passive("telegram:1:1", trace_id="trace:turn")
        assert store.get(task.task_id).status == "paused"  # type: ignore[union-attr]
        checkpoint = store.get(task.task_id).checkpoint  # type: ignore[union-attr]
        assert "next_step" in checkpoint

        coordinator.exit_passive("telegram:1:1", trace_id="trace:turn")
        await _eventually(lambda: store.get(task.task_id).status == "done")  # type: ignore[union-attr]
        assert store.get(task.task_id).checkpoint == {"next_step": 4}  # type: ignore[union-attr]
        assert completed_steps == [0, 1, 2, 3]
    finally:
        await runtime.close()


async def test_passive_waits_for_inflight_delivery_and_blocks_new_proactive() -> None:
    coordinator = ModeCoordinator()
    session = "telegram:2:2"
    assert coordinator.try_enter_proactive(session, trace_id="tick:1")

    entering = asyncio.create_task(
        coordinator.enter_passive(session, trace_id="turn:1")
    )
    await asyncio.sleep(0)
    assert coordinator.is_passive_active(session)
    assert not entering.done()
    assert not coordinator.try_enter_proactive(session, trace_id="tick:2")

    coordinator.exit_proactive(session, trace_id="tick:1")
    await entering
    assert not coordinator.try_enter_proactive(session, trace_id="tick:3")
    coordinator.exit_passive(session, trace_id="turn:1")
    assert coordinator.try_enter_proactive(session, trace_id="tick:4")
    coordinator.exit_proactive(session, trace_id="tick:4")


async def test_passive_completion_wakes_proactive_scheduler() -> None:
    coordinator = ModeCoordinator()
    wakes: list[str] = []
    coordinator.attach_proactive_waker(lambda: wakes.append("wake"))
    await coordinator.enter_passive("telegram:3:3")
    coordinator.exit_passive("telegram:3:3")
    assert wakes == ["wake"]


def test_running_task_recovers_as_paused_after_restart(tmp_path) -> None:
    path = tmp_path / "idle.db"
    first = IdleTaskStore(path)
    task = first.enqueue("health")
    claimed = first.claim_next()
    assert claimed is not None and claimed.status == "running"
    first.close()

    restarted = IdleTaskStore(path)
    try:
        assert restarted.recover_running() == 1
        recovered = restarted.get(task.task_id)
        assert recovered is not None and recovered.status == "paused"
        assert recovered.attempts == 1
    finally:
        restarted.close()


def test_idle_runtime_rejects_external_side_effect_permission(tmp_path) -> None:
    runtime = IdleTaskRuntime(
        IdleTaskStore(tmp_path / "idle.db"), ModeCoordinator()
    )
    with pytest.raises(ValueError, match="cannot send messages"):
        runtime.register("send_news", lambda _ctx: None, permission="external")  # type: ignore[arg-type]
    runtime.store.close()


async def test_idle_failure_is_isolated_and_next_task_still_runs(tmp_path) -> None:
    coordinator = ModeCoordinator()
    store = IdleTaskStore(tmp_path / "idle.db")
    runtime = IdleTaskRuntime(store, coordinator, poll_seconds=0.01)
    ran: list[str] = []

    async def broken(_context) -> None:
        raise RuntimeError("broken cache cleanup")

    async def healthy(_context) -> None:
        ran.append("healthy")

    runtime.register("broken", broken, permission="local_maintenance")
    runtime.register("healthy", healthy, permission="local_read")
    failed = runtime.enqueue("broken", priority=10)
    succeeded = runtime.enqueue("healthy", priority=20)
    await runtime.start()
    try:
        await _eventually(lambda: store.get(succeeded.task_id).status == "done")  # type: ignore[union-attr]
        assert store.get(failed.task_id).status == "failed"  # type: ignore[union-attr]
        assert "broken cache cleanup" in store.get(failed.task_id).last_error  # type: ignore[union-attr]
        assert ran == ["healthy"]
    finally:
        await runtime.close()


class _Pipeline:
    async def execute(self, message: InboundMessage) -> OutboundMessage:
        await asyncio.sleep(0.01)
        return OutboundMessage(
            chat_id=message.chat_id,
            content=f"reply:{message.content}",
            turn_id=message.turn_id,
            trace_id=message.trace_id,
        )


async def test_turn_keeps_passive_mode_until_real_delivery_finishes() -> None:
    init_db()
    coordinator = ModeCoordinator()
    bus = MessageBus(RuntimeMessageStore())
    runtime = TurnRuntime(
        bus=bus,
        pipeline=_Pipeline(),  # type: ignore[arg-type]
        mode_coordinator=coordinator,
    )
    delivery_started = asyncio.Event()
    release_delivery = asyncio.Event()
    delivered: list[str] = []

    async def slow_adapter(envelope) -> None:
        delivery_started.set()
        await release_delivery.wait()
        delivered.append(envelope.as_outbound().content)

    bus.subscribe_outbound("telegram", slow_adapter)
    await runtime.start()
    envelope = envelope_from_inbound(
        InboundMessage(11, 22, "hello", trace_id="trace:passive"),
        client_message_id="m11-delivery",
    )
    try:
        assert await bus.publish_inbound(envelope)
        await delivery_started.wait()
        assert coordinator.is_passive_active(envelope.session_key)
        assert not coordinator.try_enter_proactive(envelope.session_key)
        release_delivery.set()
        await _eventually(lambda: delivered == ["reply:hello"])
        await _eventually(lambda: not coordinator.is_passive_active(envelope.session_key))
    finally:
        release_delivery.set()
        await runtime.stop()


async def test_mode_trace_uses_shared_trace_ids() -> None:
    coordinator = ModeCoordinator()
    await coordinator.enter_passive("telegram:4:4", trace_id="trace:shared")
    coordinator.exit_passive("telegram:4:4", trace_id="trace:shared")
    assert [item.trace_id for item in coordinator.transitions] == [
        "trace:shared",
        "trace:shared",
        "trace:shared",
    ]
    assert [item.action for item in coordinator.transitions] == [
        "waiting",
        "started",
        "finished",
    ]
