from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from agent.tools.message_push import MessagePushTool
from proactive_v2.gateway import DataGateway, GatewayResult


DecisionKind = Literal["reply", "skip"]
DecisionFn = Callable[[GatewayResult], "ProactiveDecision | Awaitable[ProactiveDecision]"]


@dataclass
class ProactiveDecision:
    decision: DecisionKind
    message: str = ""
    score: float = 0.0
    reason: str = ""
    evidence: list[str] = field(default_factory=list)


@dataclass
class ProactiveTickResult:
    tick_id: str
    decision: DecisionKind
    sent: bool
    score: float
    reason: str
    message: str = ""
    evidence: list[str] = field(default_factory=list)
    gateway: GatewayResult | None = None


class AgentTick:
    """Single proactive decision tick.

    This is the minimal current-project equivalent of Akashic's AgentTick:
    gateway snapshot -> decision -> message_push. It is intentionally
    dependency-injected so tests and future evals can run without Telegram or LLM.
    """

    def __init__(
        self,
        *,
        gateway: DataGateway,
        push_tool: MessagePushTool,
        default_channel: str = "telegram",
        default_chat_id: str = "",
        threshold: float = 0.6,
        passive_busy_fn: Callable[[str], bool] | None = None,
        decision_fn: DecisionFn | None = None,
        session_key: str = "",
    ) -> None:
        self._gateway = gateway
        self._push_tool = push_tool
        self._default_channel = default_channel
        self._default_chat_id = str(default_chat_id)
        self._threshold = float(threshold)
        self._passive_busy_fn = passive_busy_fn
        self._decision_fn = decision_fn
        self._session_key = session_key or f"{default_channel}:{default_chat_id}"
        self.last_result: ProactiveTickResult | None = None

    async def tick(self) -> ProactiveTickResult | None:
        tick_id = str(uuid4())
        if not self._default_chat_id.strip():
            return None
        if self._passive_busy_fn is not None and self._passive_busy_fn(self._session_key):
            result = ProactiveTickResult(
                tick_id=tick_id,
                decision="skip",
                sent=False,
                score=0.0,
                reason="busy",
            )
            self.last_result = result
            return result

        gateway_result = await self._gateway.run()
        decision = await self._decide(gateway_result)
        should_send = (
            decision.decision == "reply"
            and bool(decision.message.strip())
            and decision.score >= self._threshold
        )
        if should_send:
            await self._push_tool.execute(
                channel=self._default_channel,
                chat_id=self._default_chat_id,
                message=decision.message,
            )

        result = ProactiveTickResult(
            tick_id=tick_id,
            decision=decision.decision,
            sent=should_send,
            score=decision.score,
            reason=decision.reason,
            message=decision.message,
            evidence=list(decision.evidence),
            gateway=gateway_result,
        )
        self.last_result = result
        return result

    async def _decide(self, gateway_result: GatewayResult) -> ProactiveDecision:
        if self._decision_fn is not None:
            decision = self._decision_fn(gateway_result)
            if inspect.isawaitable(decision):
                decision = await decision
            return decision
        return _default_alert_decision(gateway_result)


def _default_alert_decision(gateway_result: GatewayResult) -> ProactiveDecision:
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
        if title and body:
            lines.append(f"{title}\n{body}")
        elif title or body:
            lines.append(title or body)
    message = "\n\n".join(lines).strip()
    return ProactiveDecision(
        decision="reply" if message else "skip",
        message=message,
        score=1.0 if message else 0.0,
        reason="alert" if message else "empty_alert",
        evidence=evidence,
    )
