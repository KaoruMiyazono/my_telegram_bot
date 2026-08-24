from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from proactive_v2.contracts import ProactivePolicy, ProactiveTickResult
from proactive_v2.gateway import GatewayResult


@dataclass(frozen=True)
class ScheduleDecision:
    interval_seconds: int
    next_check_at: datetime
    reason: str
    factors: dict[str, float]


class AdaptiveScheduler:
    """Turn Tick outcomes into bounded, explainable polling intervals."""

    def __init__(self, policy: ProactivePolicy) -> None:
        self._policy = policy
        self._empty_streak = 0
        self._error_streak = 0

    @property
    def empty_streak(self) -> int:
        return self._empty_streak

    @property
    def error_streak(self) -> int:
        return self._error_streak

    def observe(
        self,
        result: ProactiveTickResult,
        *,
        now: datetime | None = None,
    ) -> ScheduleDecision:
        current = _aware(now or datetime.now(timezone.utc))
        snapshot = result.gateway if isinstance(result.gateway, GatewayResult) else GatewayResult()
        factors = {
            "idle_factor": 1.0,
            "empty_tick_backoff": 1.0,
            "user_activity_factor": 1.0,
            "error_backoff": 1.0,
            "source_freshness_factor": 1.0,
        }

        if result.reason.startswith("gate:"):
            self._error_streak = 0
            reason = result.reason
            interval = self._policy.blocked_interval_seconds
            factors["user_activity_factor"] = (
                2.0 if result.reason == "gate:passive_busy" else 1.0
            )
            interval = int(interval * factors["user_activity_factor"])
        elif snapshot.source_failures and not _has_candidates(snapshot):
            self._error_streak += 1
            self._empty_streak = 0
            reason = "source_error_backoff"
            interval = min(
                self._policy.error_backoff_max_seconds,
                int(
                    self._policy.error_backoff_base_seconds
                    * (2 ** max(0, self._error_streak - 1))
                ),
            )
            factors["error_backoff"] = interval / max(
                1, self._policy.normal_interval_seconds
            )
        elif snapshot.alerts:
            self._empty_streak = 0
            self._error_streak = 0
            reason = "alert_freshness"
            interval = self._policy.alert_interval_seconds
            factors["source_freshness_factor"] = interval / max(
                1, self._policy.normal_interval_seconds
            )
        elif not _has_candidates(snapshot):
            self._empty_streak += 1
            self._error_streak = 0
            reason = "empty_tick_backoff"
            interval = min(
                self._policy.empty_backoff_max_seconds,
                int(
                    self._policy.empty_interval_seconds
                    * (
                        self._policy.empty_backoff_multiplier
                        ** max(0, self._empty_streak - 1)
                    )
                ),
            )
            factors["empty_tick_backoff"] = interval / max(
                1, self._policy.empty_interval_seconds
            )
        else:
            self._empty_streak = 0
            self._error_streak = 0
            reason = "normal_candidates"
            interval = self._policy.normal_interval_seconds
            freshness = _freshness_factor(snapshot, current)
            factors["source_freshness_factor"] = freshness
            interval = int(interval * freshness)

        interval = _with_deterministic_jitter(
            max(1, interval),
            result.tick_id,
            self._policy.schedule_jitter_ratio,
        )
        return ScheduleDecision(
            interval_seconds=interval,
            next_check_at=current + timedelta(seconds=interval),
            reason=reason,
            factors=factors,
        )


def _has_candidates(snapshot: GatewayResult) -> bool:
    # Context is supporting evidence and cannot trigger a delivery by itself.
    return bool(snapshot.alerts or snapshot.content_meta)


def _freshness_factor(snapshot: GatewayResult, now: datetime) -> float:
    timestamps: list[datetime] = []
    for item in [*snapshot.alerts, *snapshot.content_meta]:
        for key in ("triggered_at", "published_at", "first_seen_at"):
            raw = item.get(key)
            if not raw:
                continue
            try:
                parsed = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
            except (TypeError, ValueError):
                continue
            timestamps.append(_aware(parsed))
            break
    if not timestamps:
        return 1.0
    age = min(max(0.0, (now - value).total_seconds()) for value in timestamps)
    if age <= 300:
        return 0.5
    if age >= 86_400:
        return 2.0
    return 1.0


def _with_deterministic_jitter(seconds: int, key: str, ratio: float) -> int:
    bounded = max(0.0, min(float(ratio), 0.5))
    if bounded == 0.0:
        return max(1, seconds)
    digest = hashlib.sha256(str(key).encode("utf-8")).digest()
    unit = int.from_bytes(digest[:8], "big") / float(2**64 - 1)
    multiplier = 1.0 + ((unit * 2.0) - 1.0) * bounded
    return max(1, int(round(seconds * multiplier)))


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
