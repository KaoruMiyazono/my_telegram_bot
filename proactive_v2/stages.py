from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from agent.tools.message_push import MessagePushTool
from proactive_v2.contracts import (
    DeliverInput,
    DeliverOutput,
    FetchInput,
    FetchOutput,
    GateInput,
    GateOutput,
    JudgeInput,
    JudgeOutput,
    JudgeProposal,
    PhaseTrace,
    ResolveInput,
    ResolveOutput,
)
from proactive_v2.gateway import GatewayResult
from proactive_v2.state import ProactiveStateStore

BusyFn = Callable[[str], bool]
JudgeFn = Callable[[JudgeInput], JudgeProposal | Awaitable[JudgeProposal]]
JudgeTool = Callable[..., Any]
AckHandler = Callable[[str, str], Any]


class GateStage:
    def __init__(self, state: ProactiveStateStore, busy_fn: BusyFn | None = None) -> None:
        self._state = state
        self._busy_fn = busy_fn

    async def run(self, value: GateInput) -> GateOutput:
        started = _now()
        context = value.context
        policy = context.policy
        urgent = context.priority == "urgent"
        allowed = True
        reason = "allowed"
        route = "urgent" if urgent else "normal"
        wait_seconds = policy.normal_interval_seconds

        if not context.target.chat_id.strip():
            allowed, reason = False, "no_target"
        elif (
            self._busy_fn is not None
            and self._busy_fn(context.target.session_key)
            and not (urgent and policy.urgent_bypass_busy)
        ):
            allowed, reason = False, "passive_busy"
        elif _in_quiet_hours(context.started_at, policy.timezone, policy.quiet_start_hour, policy.quiet_end_hour) and not (
            urgent and policy.urgent_bypass_quiet
        ):
            allowed, reason = False, "quiet_hours"
        elif self._state.count_deliveries_since(
            context.target.session_key,
            context.started_at - timedelta(seconds=max(0, policy.cooldown_seconds)),
        ) and not (urgent and policy.urgent_bypass_cooldown):
            allowed, reason = False, "cooldown"
        elif self._state.count_deliveries_since(
            context.target.session_key,
            _local_day_start_utc(context.started_at, policy.timezone),
        ) >= max(0, policy.daily_limit) and not (
            urgent and policy.urgent_bypass_daily_limit
        ):
            allowed, reason = False, "daily_limit"

        if not allowed:
            route = "blocked"
            wait_seconds = policy.blocked_interval_seconds
        next_check = context.started_at + timedelta(seconds=max(1, wait_seconds))
        return GateOutput(
            allowed=allowed,
            route=route,
            reason=reason,
            next_check_at=next_check,
            trace=_trace(
                "gate",
                started,
                "allowed" if allowed else "skipped",
                reason,
                {"route": route, "priority": context.priority},
            ),
        )


class FetchStage:
    def __init__(self, gateway: Any) -> None:
        self._gateway = gateway

    async def run(self, value: FetchInput) -> FetchOutput:
        started = _now()
        if not value.gate.allowed:
            snapshot = GatewayResult()
            outcome, reason = "skipped", f"gate:{value.gate.reason}"
        else:
            snapshot = await self._gateway.run()
            outcome, reason = "fetched", "snapshot_ready"
        return FetchOutput(
            snapshot=snapshot,
            trace=_trace(
                "fetch",
                started,
                outcome,
                reason,
                {
                    "alerts": len(snapshot.alerts),
                    "context": len(snapshot.context),
                    "content": len(snapshot.content_meta),
                },
            ),
        )


class JudgeStage:
    def __init__(
        self,
        judge_fn: JudgeFn | None = None,
        *,
        tools: Mapping[str, JudgeTool] | None = None,
    ) -> None:
        self._judge_fn = judge_fn
        self._tools = dict(tools or {})

    async def run(self, value: JudgeInput) -> JudgeOutput:
        started = _now()
        current = value
        results: list[dict[str, Any]] = list(value.tool_results)
        proposal = await self._decide(current)
        steps = 0
        max_steps = max(0, value.context.policy.max_judge_steps)
        while proposal.tool_calls and steps < max_steps:
            steps += 1
            for call in proposal.tool_calls:
                tool = self._tools.get(call.name)
                if tool is None:
                    results.append(
                        {"tool": call.name, "ok": False, "error": "tool_not_whitelisted"}
                    )
                    continue
                try:
                    result = tool(**call.arguments)
                    if inspect.isawaitable(result):
                        result = await result
                    results.append({"tool": call.name, "ok": True, "result": result})
                except Exception as exc:
                    results.append({"tool": call.name, "ok": False, "error": str(exc)})
            current = JudgeInput(
                context=value.context,
                fetched=value.fetched,
                tool_results=tuple(results),
            )
            proposal = await self._decide(current)

        if proposal.tool_calls:
            proposal = JudgeProposal(
                decision="skip",
                score=0.0,
                reason="judge_tool_limit",
                evidence=proposal.evidence,
            )
        return JudgeOutput(
            proposal=proposal,
            tool_results=tuple(results),
            steps=steps,
            trace=_trace(
                "judge",
                started,
                proposal.decision,
                proposal.reason,
                {"score": proposal.score, "tool_steps": steps},
            ),
        )

    async def _decide(self, value: JudgeInput) -> JudgeProposal:
        if self._judge_fn is None:
            return _default_judge(value)
        result = self._judge_fn(value)
        if inspect.isawaitable(result):
            result = await result
        return result


class ResolveStage:
    def __init__(self, state: ProactiveStateStore) -> None:
        self._state = state

    async def run(self, value: ResolveInput) -> ResolveOutput:
        started = _now()
        context = value.context
        proposal = value.judged.proposal
        action = proposal.decision
        reason = proposal.reason
        message = proposal.message.strip()
        evidence = tuple(dict.fromkeys(proposal.evidence))
        delivery_key = _delivery_key(evidence, message)

        if not value.gate.allowed:
            action, reason = "skip", f"gate:{value.gate.reason}"
        elif action != "reply":
            action, reason = "skip", reason or "judge_skip"
        elif not message:
            action, reason = "skip", "empty_message"
        elif proposal.score < context.policy.threshold:
            action, reason = "skip", "below_threshold"
        elif self._state.count_deliveries_since(
            context.target.session_key,
            context.started_at
            - timedelta(seconds=max(0, context.policy.cooldown_seconds)),
        ) and not (
            context.priority == "urgent"
            and context.policy.urgent_bypass_cooldown
        ):
            action, reason = "skip", "cooldown"
        elif self._state.count_deliveries_since(
            context.target.session_key,
            _local_day_start_utc(context.started_at, context.policy.timezone),
        ) >= max(0, context.policy.daily_limit) and not (
            context.priority == "urgent"
            and context.policy.urgent_bypass_daily_limit
        ):
            action, reason = "skip", "daily_limit"
        elif evidence and self._state.delivered_events(
            context.target.session_key, evidence
        ) == set(evidence):
            action, reason = "skip", "event_duplicate"
        elif self._state.delivery_key_seen(
            context.target.session_key,
            delivery_key,
            now=context.started_at,
            window_hours=context.policy.delivery_dedupe_hours,
        ):
            action, reason = "skip", "delivery_duplicate"
        elif any(
            _similarity(message, previous)
            >= context.policy.semantic_similarity_threshold
            for previous in self._state.recent_messages(
                context.target.session_key,
                now=context.started_at,
                window_hours=context.policy.semantic_dedupe_hours,
            )
        ):
            action, reason = "skip", "semantic_duplicate"

        return ResolveOutput(
            action=action,
            reason=reason,
            message=message,
            score=proposal.score,
            evidence=evidence,
            delivery_key=delivery_key,
            trace=_trace(
                "resolve",
                started,
                action,
                reason,
                {"delivery_key": delivery_key, "evidence_count": len(evidence)},
            ),
        )


class AckOutboxDispatcher:
    def __init__(
        self,
        state: ProactiveStateStore,
        handlers: Mapping[str, AckHandler] | None = None,
    ) -> None:
        self._state = state
        self._handlers = dict(handlers or {})

    async def drain(self, ack_ids: tuple[int, ...] | None = None) -> None:
        selected = set(ack_ids or ())
        for row in self._state.pending_acks():
            ack_id = int(row["id"])
            if selected and ack_id not in selected:
                continue
            handler = self._handlers.get(str(row["source_id"]))
            if handler is None:
                self._state.settle_ack(ack_id, success=False, error="ack_handler_missing")
                continue
            try:
                result = handler(str(row["event_id"]), str(row["decision"]))
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                self._state.settle_ack(ack_id, success=False, error=str(exc))
            else:
                self._state.settle_ack(ack_id, success=True)


class DeliverStage:
    def __init__(
        self,
        state: ProactiveStateStore,
        push_tool: MessagePushTool,
        *,
        ack_dispatcher: AckOutboxDispatcher | None = None,
    ) -> None:
        self._state = state
        self._push_tool = push_tool
        self._acks = ack_dispatcher or AckOutboxDispatcher(state)

    async def run(self, value: DeliverInput) -> DeliverOutput:
        started = _now()
        context, resolved = value.context, value.resolved
        if resolved.action != "reply":
            return self._result(started, False, False, None, (), "", resolved.reason)
        if context.mode == "shadow":
            return self._result(started, False, True, None, (), "", "shadow_mode")

        send_result = await self._push_tool.execute(
            channel=context.target.channel,
            chat_id=context.target.chat_id,
            message=resolved.message,
        )
        if not _push_succeeded(send_result):
            return self._result(
                started, False, False, None, (), send_result, "send_failed"
            )

        delivery_id, ack_ids = self._state.record_delivery_and_enqueue_acks(
            context=context,
            delivery_key=resolved.delivery_key,
            message=resolved.message,
            evidence=resolved.evidence,
        )
        await self._acks.drain(ack_ids)
        return self._result(
            started, True, False, delivery_id, ack_ids, "", "delivered"
        )

    @staticmethod
    def _result(
        started: datetime,
        sent: bool,
        shadow: bool,
        delivery_id: int | None,
        ack_ids: tuple[int, ...],
        error: str,
        reason: str,
    ) -> DeliverOutput:
        return DeliverOutput(
            sent=sent,
            shadow=shadow,
            delivery_id=delivery_id,
            ack_outbox_ids=ack_ids,
            error=error,
            trace=_trace(
                "deliver",
                started,
                "sent" if sent else ("shadow" if shadow else "skipped"),
                reason,
                {"delivery_id": delivery_id, "ack_count": len(ack_ids)},
            ),
        )


def _default_judge(value: JudgeInput) -> JudgeProposal:
    snapshot = value.fetched.snapshot
    alert_lines: list[str] = []
    evidence: list[str] = []
    for alert in snapshot.alerts:
        title = str(alert.get("title") or alert.get("message") or "").strip()
        body = str(alert.get("body") or alert.get("content") or "").strip()
        event_id = str(alert.get("event_id") or alert.get("id") or "").strip()
        source = str(alert.get("ack_server") or "alert").strip()
        if event_id:
            evidence.append(f"{source}:{event_id}")
        line = f"{title}\n{body}".strip()
        if line:
            alert_lines.append(line)
    if alert_lines:
        return JudgeProposal(
            decision="reply",
            message="\n\n".join(alert_lines),
            score=1.0,
            reason="alert",
            evidence=tuple(evidence),
        )

    content_lines: list[str] = []
    for item in snapshot.content_meta:
        score = float(item.get("relevance_score") or item.get("score") or 0.0)
        if not (item.get("interesting") is True or score >= value.context.policy.threshold):
            continue
        item_id = str(item.get("id") or "")
        title = str(item.get("title") or "").strip()
        body = str(snapshot.content_store.get(item_id) or "").strip()
        if item_id:
            evidence.append(item_id)
        content_lines.append(f"{title}\n{body}".strip()[:2000])
    message = "\n\n".join(line for line in content_lines if line).strip()
    if message:
        return JudgeProposal(
            decision="reply",
            message=message,
            score=0.8,
            reason="relevant_content",
            evidence=tuple(evidence),
        )
    # Context is only supporting evidence; it must never create a push itself.
    return JudgeProposal(
        decision="skip",
        score=0.0,
        reason="context_only" if snapshot.context else "no_candidate",
    )


def _trace(
    phase: str,
    started: datetime,
    outcome: str,
    reason: str,
    details: dict[str, Any],
) -> PhaseTrace:
    finished = _now()
    details = dict(details)
    details["duration_ms"] = round((finished - started).total_seconds() * 1000, 3)
    return PhaseTrace(
        phase=phase,
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        outcome=outcome,
        reason=reason,
        details=details,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _local_day_start_utc(now: datetime, timezone_name: str) -> datetime:
    local = _aware(now).astimezone(ZoneInfo(timezone_name))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def _in_quiet_hours(now: datetime, timezone_name: str, start: int, end: int) -> bool:
    hour = _aware(now).astimezone(ZoneInfo(timezone_name)).hour
    start, end = start % 24, end % 24
    if start == end:
        return False
    return start <= hour < end if start < end else hour >= start or hour < end


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _delivery_key(evidence: tuple[str, ...], message: str) -> str:
    payload = {"evidence": sorted(evidence), "message": _normalize(message)}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _normalize(text: str) -> str:
    return " ".join(str(text).lower().split())


def _similarity(left: str, right: str) -> float:
    def tokens(text: str) -> set[str]:
        normalized = re.sub(r"\s+", "", text.lower())
        if len(normalized) < 2:
            return {normalized} if normalized else set()
        return {normalized[index : index + 2] for index in range(len(normalized) - 1)}

    a, b = tokens(left), tokens(right)
    return len(a & b) / len(a | b) if a and b else 0.0


def _push_succeeded(result: str) -> bool:
    return "文本已发送" in result and not any(
        marker in result for marker in ("失败", "错误", "不支持", "未注册", "没有")
    )
