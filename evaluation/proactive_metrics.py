"""Deterministic business metrics for proactive-agent evaluation fixtures."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class ProactiveEvaluationCase:
    """One labelled proactive decision and its delivery/ACK outcome."""

    case_id: str
    should_push: bool
    sent: bool
    duplicate: bool = False
    delivery_succeeded: bool = False
    provider_acked: bool = False
    in_quiet_hours: bool = False
    urgent: bool = False
    passive_interfered: bool = False


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def score_proactive_cases(
    cases: Iterable[ProactiveEvaluationCase],
) -> dict[str, Any]:
    """Aggregate the six M13 proactive metrics without calling an LLM.

    ``ACK Accuracy`` means that the provider ACK state agrees with the actual
    delivery result. A failed delivery must remain unacknowledged; acknowledging
    it is just as wrong as forgetting to ACK a successful delivery.
    """

    rows = list(cases)
    sent = [case for case in rows if case.sent]
    expected = [case for case in rows if case.should_push]
    true_pushes = sum(case.should_push for case in sent)
    missed = sum(not case.sent for case in expected)
    duplicates = sum(case.duplicate for case in sent)
    ack_cases = [case for case in rows if case.sent]
    ack_correct = sum(
        case.provider_acked == case.delivery_succeeded for case in ack_cases
    )
    quiet_violations = sum(
        case.sent and case.in_quiet_hours and not case.urgent for case in rows
    )
    passive_interference = sum(case.passive_interfered for case in rows)

    return {
        "case_count": len(rows),
        "precision_of_push": _ratio(true_pushes, len(sent)),
        "miss_rate": _ratio(missed, len(expected)),
        "duplicate_rate": _ratio(duplicates, len(sent)),
        "ack_accuracy": _ratio(ack_correct, len(ack_cases)),
        "quiet_hour_violations": quiet_violations,
        "passive_interference": passive_interference,
        "counts": {
            "sent": len(sent),
            "expected_pushes": len(expected),
            "true_pushes": true_pushes,
            "missed": missed,
            "duplicates": duplicates,
            "ack_correct": ack_correct,
        },
    }
