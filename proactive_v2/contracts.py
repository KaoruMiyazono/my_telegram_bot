from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

DecisionKind = Literal["reply", "skip"]
ProactiveMode = Literal["live", "shadow"]
TickPriority = Literal["normal", "urgent"]


@dataclass(frozen=True)
class ProactiveTarget:
    channel: str
    chat_id: str
    session_key: str
    user_id: str = ""


@dataclass(frozen=True)
class ProactivePolicy:
    threshold: float = 0.6
    cooldown_seconds: int = 3600
    daily_limit: int = 3
    quiet_start_hour: int = 22
    quiet_end_hour: int = 8
    timezone: str = "Asia/Shanghai"
    urgent_bypass_busy: bool = False
    urgent_bypass_cooldown: bool = True
    urgent_bypass_quiet: bool = True
    urgent_bypass_daily_limit: bool = False
    delivery_dedupe_hours: int = 48
    semantic_dedupe_hours: int = 24
    semantic_similarity_threshold: float = 0.88
    content_dedupe_hours: int = 168
    max_judge_steps: int = 2
    cold_start_threshold: float = 0.9
    normal_interval_seconds: int = 300
    blocked_interval_seconds: int = 60
    empty_interval_seconds: int = 600
    empty_backoff_multiplier: float = 2.0
    empty_backoff_max_seconds: int = 2400
    error_backoff_base_seconds: int = 60
    error_backoff_max_seconds: int = 1800
    alert_interval_seconds: int = 60
    schedule_jitter_ratio: float = 0.1


@dataclass(frozen=True)
class AgentTickContext:
    target: ProactiveTarget
    policy: ProactivePolicy = field(default_factory=ProactivePolicy)
    mode: ProactiveMode = "shadow"
    priority: TickPriority = "normal"
    tick_id: str = field(default_factory=lambda: f"tick:{uuid4()}")
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class PhaseTrace:
    phase: str
    started_at: str
    finished_at: str
    outcome: str
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UserInterestContext:
    """Small, user-scoped projection of long-term memory for proactive judging."""

    user_id: str = ""
    positive_topics: tuple[str, ...] = ()
    negative_topics: tuple[str, ...] = ()
    important_rules: tuple[str, ...] = ()
    profile_facts: tuple[str, ...] = ()
    memory_evidence: tuple[str, ...] = ()
    source_count: int = 0
    truncated: bool = False

    @property
    def cold_start(self) -> bool:
        return not (
            self.positive_topics
            or self.negative_topics
            or self.important_rules
        )


@dataclass(frozen=True)
class GateInput:
    context: AgentTickContext


@dataclass(frozen=True)
class GateOutput:
    allowed: bool
    route: Literal["normal", "urgent", "blocked"]
    reason: str
    next_check_at: datetime
    trace: PhaseTrace


@dataclass(frozen=True)
class FetchInput:
    context: AgentTickContext
    gate: GateOutput


@dataclass(frozen=True)
class FetchOutput:
    snapshot: Any
    trace: PhaseTrace


@dataclass(frozen=True)
class JudgeToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JudgeProposal:
    decision: DecisionKind
    message: str = ""
    score: float = 0.0
    reason: str = ""
    evidence: tuple[str, ...] = ()
    reasoning_evidence: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)
    tool_calls: tuple[JudgeToolCall, ...] = ()


@dataclass(frozen=True)
class JudgeInput:
    context: AgentTickContext
    fetched: FetchOutput
    interests: UserInterestContext = field(default_factory=UserInterestContext)
    tool_results: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class JudgeOutput:
    proposal: JudgeProposal
    tool_results: tuple[dict[str, Any], ...]
    steps: int
    trace: PhaseTrace


@dataclass(frozen=True)
class ResolveInput:
    context: AgentTickContext
    gate: GateOutput
    fetched: FetchOutput
    judged: JudgeOutput


@dataclass(frozen=True)
class ResolveOutput:
    action: DecisionKind
    reason: str
    message: str
    score: float
    evidence: tuple[str, ...]
    reasoning_evidence: tuple[str, ...]
    delivery_key: str
    trace: PhaseTrace


@dataclass(frozen=True)
class DeliverInput:
    context: AgentTickContext
    resolved: ResolveOutput


@dataclass(frozen=True)
class DeliverOutput:
    sent: bool
    shadow: bool
    delivery_id: int | None
    ack_outbox_ids: tuple[int, ...]
    error: str
    trace: PhaseTrace


@dataclass(frozen=True)
class ProactiveTickResult:
    tick_id: str
    decision: DecisionKind
    sent: bool
    score: float
    reason: str
    message: str = ""
    evidence: list[str] = field(default_factory=list)
    reasoning_evidence: list[str] = field(default_factory=list)
    gateway: Any = None
    mode: ProactiveMode = "shadow"
    delivery_key: str = ""
    next_check_at: datetime | None = None
    next_interval_seconds: int = 0
    schedule_reason: str = ""
    traces: list[PhaseTrace] = field(default_factory=list)
    ack_outbox_ids: list[int] = field(default_factory=list)
    error: str = ""
