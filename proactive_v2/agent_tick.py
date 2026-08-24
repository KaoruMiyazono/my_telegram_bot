from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from agent.tools.message_push import MessagePushTool
from proactive_v2.contracts import (
    AgentTickContext,
    DeliverInput,
    FetchInput,
    GateInput,
    JudgeInput,
    JudgeProposal,
    ProactiveMode,
    ProactivePolicy,
    ProactiveTarget,
    ProactiveTickResult,
    ResolveInput,
    TickPriority,
)
from proactive_v2.gateway import GatewayResult, ProactiveGateway
from proactive_v2.interests import AmbiguousInterestJudge
from proactive_v2.scheduler import AdaptiveScheduler
from proactive_v2.stages import (
    AckHandler,
    AckOutboxDispatcher,
    DeliverStage,
    FetchStage,
    GateStage,
    JudgeStage,
    JudgeTool,
    InterestReader,
    ResolveStage,
    DeliveryCoordinator,
)
from proactive_v2.state import ProactiveStateStore

DecisionKind = Literal["reply", "skip"]
DecisionFn = Callable[[GatewayResult], "ProactiveDecision | Awaitable[ProactiveDecision]"]


@dataclass
class ProactiveDecision:
    decision: DecisionKind
    message: str = ""
    score: float = 0.0
    reason: str = ""
    evidence: list[str] = field(default_factory=list)


class AgentTick:
    """Orchestrate one isolated Gate -> Fetch -> Judge -> Resolve -> Deliver tick."""

    def __init__(
        self,
        *,
        gateway: ProactiveGateway,
        push_tool: MessagePushTool,
        default_channel: str = "telegram",
        default_chat_id: str = "",
        threshold: float = 0.6,
        passive_busy_fn: Callable[[str], bool] | None = None,
        decision_fn: DecisionFn | None = None,
        session_key: str = "",
        user_id: str = "",
        mode: ProactiveMode = "live",
        priority: TickPriority = "normal",
        policy: ProactivePolicy | None = None,
        state_store: ProactiveStateStore | None = None,
        judge_tools: Mapping[str, JudgeTool] | None = None,
        interest_reader: InterestReader | None = None,
        ambiguous_interest_judge: AmbiguousInterestJudge | None = None,
        ack_handlers: Mapping[str, AckHandler] | None = None,
        scheduler: AdaptiveScheduler | None = None,
        ack_max_attempts: int = 5,
        ack_retry_base_seconds: int = 30,
        ack_retry_max_seconds: int = 3600,
        mode_coordinator: DeliveryCoordinator | None = None,
    ) -> None:
        self._channel = default_channel
        self._chat_id = str(default_chat_id)
        self._session_key = session_key or f"{default_channel}:{default_chat_id}"
        self._user_id = str(user_id)
        self._mode: ProactiveMode = mode
        self._priority: TickPriority = priority
        self._policy = policy or ProactivePolicy(threshold=float(threshold))
        self._state = state_store or ProactiveStateStore(":memory:")
        self._owns_state = state_store is None
        self._gate = GateStage(self._state, passive_busy_fn)
        self._fetch = FetchStage(gateway)
        self._judge = JudgeStage(
            self._adapt_legacy_decider(decision_fn) if decision_fn else None,
            tools=judge_tools,
            interest_reader=interest_reader,
            ambiguous_interest_judge=ambiguous_interest_judge,
        )
        self._resolve = ResolveStage(self._state)
        self._acks = AckOutboxDispatcher(
            self._state,
            ack_handlers,
            max_attempts=ack_max_attempts,
            retry_base_seconds=ack_retry_base_seconds,
            retry_max_seconds=ack_retry_max_seconds,
        )
        self._deliver = DeliverStage(
            self._state,
            push_tool,
            ack_dispatcher=self._acks,
            coordinator=mode_coordinator,
        )
        self._scheduler = scheduler or AdaptiveScheduler(self._policy)
        self.last_result: ProactiveTickResult | None = None

    @property
    def policy(self) -> ProactivePolicy:
        return self._policy

    async def tick(self) -> ProactiveTickResult | None:
        if not self._chat_id.strip():
            return None
        # A previous process may have crashed after delivery but before remote ACK.
        await self._acks.drain()
        context = AgentTickContext(
            target=ProactiveTarget(
                channel=self._channel,
                chat_id=self._chat_id,
                session_key=self._session_key,
                user_id=self._user_id,
            ),
            policy=self._policy,
            mode=self._mode,
            priority=self._priority,
        )
        gate = await self._gate.run(GateInput(context))
        fetched = await self._fetch.run(FetchInput(context, gate))
        judged = await self._judge.run(JudgeInput(context, fetched))
        resolved = await self._resolve.run(
            ResolveInput(context, gate, fetched, judged)
        )
        delivered = await self._deliver.run(DeliverInput(context, resolved))
        result = ProactiveTickResult(
            tick_id=context.tick_id,
            decision=resolved.action,
            sent=delivered.sent,
            score=resolved.score,
            reason=resolved.reason,
            message=resolved.message,
            evidence=list(resolved.evidence),
            reasoning_evidence=list(resolved.reasoning_evidence),
            gateway=fetched.snapshot,
            mode=context.mode,
            delivery_key=resolved.delivery_key,
            next_check_at=gate.next_check_at,
            traces=[
                gate.trace,
                fetched.trace,
                judged.trace,
                resolved.trace,
                delivered.trace,
            ],
            ack_outbox_ids=list(delivered.ack_outbox_ids),
            error=delivered.error,
        )
        schedule = self._scheduler.observe(result, now=context.started_at)
        result = replace(
            result,
            next_check_at=schedule.next_check_at,
            next_interval_seconds=schedule.interval_seconds,
            schedule_reason=schedule.reason,
        )
        self._state.record_tick(context, result)
        self.last_result = result
        return result

    def close(self) -> None:
        if self._owns_state:
            self._state.close()

    async def drain_acks(self) -> None:
        await self._acks.drain()

    @staticmethod
    def _adapt_legacy_decider(decision_fn: DecisionFn) -> Callable[[JudgeInput], Any]:
        async def decide(value: JudgeInput) -> JudgeProposal:
            decision = decision_fn(value.fetched.snapshot)
            if inspect.isawaitable(decision):
                decision = await decision
            return JudgeProposal(
                decision=decision.decision,
                message=decision.message,
                score=decision.score,
                reason=decision.reason,
                evidence=tuple(decision.evidence),
            )

        return decide


def _default_alert_decision(gateway_result: GatewayResult) -> ProactiveDecision:
    """Compatibility helper retained for callers of the original M7 scaffold."""
    if not gateway_result.alerts:
        return ProactiveDecision(decision="skip", score=0.0, reason="no_alert")
    lines: list[str] = []
    evidence: list[str] = []
    for alert in gateway_result.alerts:
        title = str(alert.get("title") or alert.get("message") or "").strip()
        body = str(alert.get("body") or alert.get("content") or "").strip()
        item_id = str(alert.get("event_id") or alert.get("id") or "").strip()
        ack_server = str(alert.get("ack_server") or "alert").strip()
        if item_id:
            evidence.append(f"{ack_server}:{item_id}")
        lines.append(f"{title}\n{body}".strip())
    message = "\n\n".join(line for line in lines if line).strip()
    return ProactiveDecision(
        decision="reply" if message else "skip",
        message=message,
        score=1.0 if message else 0.0,
        reason="alert" if message else "empty_alert",
        evidence=evidence,
    )
