"""Asynchronous turn runtime public API."""

from typing import TYPE_CHECKING, Any

from agent.runtime.cancellation import CancellationRegistry, InterruptResult
from agent.runtime.idle_tasks import IdleTask, IdleTaskResult, IdleTaskRuntime, IdleTaskStore
from agent.runtime.mode_coordinator import ModeCoordinator, ModeTransition
from agent.runtime.stream_events import (
    StreamEvent,
    StreamEventBroker,
    StreamEventStore,
    StreamSubscription,
)

if TYPE_CHECKING:
    from agent.runtime.turn_runtime import TurnRuntime

__all__ = [
    "CancellationRegistry",
    "IdleTask",
    "IdleTaskResult",
    "IdleTaskRuntime",
    "IdleTaskStore",
    "InterruptResult",
    "ModeCoordinator",
    "ModeTransition",
    "StreamEvent",
    "StreamEventBroker",
    "StreamEventStore",
    "StreamSubscription",
    "TurnRuntime",
]


def __getattr__(name: str) -> Any:
    """Keep TurnRuntime public without importing the pipeline eagerly."""

    if name == "TurnRuntime":
        from agent.runtime.turn_runtime import TurnRuntime

        return TurnRuntime
    raise AttributeError(name)
