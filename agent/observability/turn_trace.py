"""Privacy-safe trace for one passive pipeline execution."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from agent.core.ids import TurnIdentity


def estimate_tokens(messages: Iterable[dict[str, Any]]) -> int:
    """Cheap deterministic estimate used for regression traces, not billing."""

    characters = sum(len(str(message.get("content") or "")) for message in messages)
    return (characters + 3) // 4


def tool_names_from_schemas(tools: Iterable[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if name and str(name) not in names:
            names.append(str(name))
    return names


def tool_names_from_calls(calls: Iterable[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for call in calls:
        function = call.get("function") if isinstance(call, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if name:
            names.append(str(name))
    return names


@dataclass
class TurnTrace:
    """Small trace envelope deliberately excluding prompts and tool payloads."""

    identity: TurnIdentity
    started_monotonic: float = field(default_factory=time.monotonic, repr=False)
    phases: list[str] = field(default_factory=list)
    tools_visible: list[str] = field(default_factory=list)
    tools_called: list[str] = field(default_factory=list)
    context_tokens_before: int = 0
    context_tokens_after: int = 0
    finish_reason: str = ""
    latency_ms: int = 0
    status: str = "running"
    error_type: str = ""
    retrieval_mode: str = ""
    retrieved_count: int = 0

    def mark_phase(self, phase: str) -> None:
        if phase not in self.phases:
            self.phases.append(phase)

    def complete(
        self,
        *,
        finish_reason: str,
        tool_calls: Iterable[dict[str, Any]] = (),
    ) -> None:
        self.finish_reason = finish_reason
        self.tools_called = tool_names_from_calls(tool_calls)
        self.status = "completed"
        self.latency_ms = max(0, int((time.monotonic() - self.started_monotonic) * 1000))

    def fail(self, error: BaseException) -> None:
        self.status = "failed"
        self.error_type = type(error).__name__
        self.latency_ms = max(0, int((time.monotonic() - self.started_monotonic) * 1000))

    def to_dict(self) -> dict[str, Any]:
        """Return the production trace. No message/tool body is included."""

        return {
            "turn_id": self.identity.turn_id,
            "session_key": self.identity.session_key,
            "trace_id": self.identity.trace_id,
            "phases": list(self.phases),
            "tools_visible": list(self.tools_visible),
            "tools_called": list(self.tools_called),
            "context_tokens_before": self.context_tokens_before,
            "context_tokens_after": self.context_tokens_after,
            "finish_reason": self.finish_reason,
            "latency_ms": self.latency_ms,
            "status": self.status,
            "error_type": self.error_type,
            "retrieval": {
                "mode": self.retrieval_mode,
                "count": self.retrieved_count,
            },
        }

    def golden_snapshot(self) -> dict[str, Any]:
        """Normalize unstable IDs and timing for version-controlled regression data."""

        payload = self.to_dict()
        payload["turn_id"] = "<turn_id>"
        payload["trace_id"] = "<trace_id>"
        payload["latency_ms"] = "<latency_ms>"
        return payload
