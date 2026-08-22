"""Asynchronous turn runtime public API."""

from typing import TYPE_CHECKING, Any

from agent.runtime.cancellation import CancellationRegistry, InterruptResult

if TYPE_CHECKING:
    from agent.runtime.turn_runtime import TurnRuntime

__all__ = ["CancellationRegistry", "InterruptResult", "TurnRuntime"]


def __getattr__(name: str) -> Any:
    """Keep TurnRuntime public without importing the pipeline eagerly."""

    if name == "TurnRuntime":
        from agent.runtime.turn_runtime import TurnRuntime

        return TurnRuntime
    raise AttributeError(name)
