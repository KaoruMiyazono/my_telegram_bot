"""Convert local mock eval cases to Akashic LongMemEval JSON.

The output shape matches ~/akashic-agent/eval/longmemeval/dataset.py:
  question_id, question_type, question, answer, question_date,
  haystack_session_ids, haystack_dates, haystack_sessions, answer_session_ids
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.dataset_builder import EvalCase, EvalDataset, QuestionType

DEFAULT_MOCK_PATH = ROOT / "data" / "evaluation" / "mock_conversations.jsonl"
DEFAULT_OUTPUT_PATH = ROOT / "eval" / "longmemeval" / "data" / "local_mock_longmemeval.json"

QUESTION_TYPE_MAP = {
    QuestionType.SINGLE_SESSION_FACT: "single-session-user",
    QuestionType.CROSS_SESSION_PREFERENCE: "single-session-preference",
    QuestionType.KNOWLEDGE_UPDATE: "knowledge-update",
    QuestionType.USER_IDENTITY: "single-session-user",
    QuestionType.MULTI_TURN_CONTEXT: "single-session-user",
}


def load_mock_conversations(path: Path | str = DEFAULT_MOCK_PATH) -> dict[str, dict[str, Any]]:
    """Load mock conversation JSONL keyed by session_id."""
    conversations: dict[str, dict[str, Any]] = {}
    path = Path(path)
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        session_id = str(row.get("session_id") or "")
        if not session_id:
            raise ValueError(f"Missing session_id in {path}:{line_no}")
        conversations[session_id] = row
    return conversations


def load_eval_cases(set_names: Iterable[str], data_dir: Path | str | None = None) -> list[EvalCase]:
    """Load green/red EvalCase sets in the requested order."""
    dataset = EvalDataset(data_dir=str(data_dir or ROOT / "data" / "evaluation"))
    cases: list[EvalCase] = []
    for set_name in set_names:
        if set_name == "green":
            cases.extend(dataset.load_green_set())
        elif set_name == "red":
            cases.extend(dataset.load_red_set())
        else:
            raise ValueError(f"Unsupported set name: {set_name}")
    return cases


def _normalize_turns(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    turns: list[dict[str, str]] = []
    for msg in messages:
        role = str(msg.get("role") or "user")
        content = str(msg.get("content") or "")
        if content.strip():
            turns.append({"role": role, "content": content})
    return turns


def eval_case_to_longmemeval(
    case: EvalCase,
    conversations_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Convert one local EvalCase into the Akashic LongMemEval JSON shape."""
    missing = [sid for sid in case.context_sessions if sid not in conversations_by_id]
    if missing:
        raise ValueError(f"{case.case_id} references unknown mock sessions: {missing}")
    if not case.context_sessions:
        raise ValueError(f"{case.case_id} has no context_sessions")

    selected = [conversations_by_id[sid] for sid in case.context_sessions]
    haystack_dates = [str(conv.get("timestamp") or "") for conv in selected]
    question_date = max((date for date in haystack_dates if date), default="")

    return {
        "question_id": case.case_id,
        "question_type": QUESTION_TYPE_MAP[case.question_type],
        "question": case.question,
        "answer": case.gold_answer,
        "question_date": question_date,
        "haystack_session_ids": list(case.context_sessions),
        "haystack_dates": haystack_dates,
        "haystack_sessions": [
            _normalize_turns(list(conv.get("messages") or []))
            for conv in selected
        ],
        "answer_session_ids": list(case.context_sessions),
        "metadata": {
            "original_question_type": case.question_type.value,
            "distance_type": case.distance_type.value,
            "source": case.source,
            "notes": case.notes,
        },
    }


def convert_mock_cases(
    *,
    set_names: Iterable[str] = ("green",),
    mock_path: Path | str = DEFAULT_MOCK_PATH,
    data_dir: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Convert local mock cases to a LongMemEval-compatible list."""
    conversations = load_mock_conversations(mock_path)
    cases = load_eval_cases(set_names, data_dir=data_dir)
    return [eval_case_to_longmemeval(case, conversations) for case in cases]


def _parse_set(raw: str) -> list[str]:
    if raw == "both":
        return ["green", "red"]
    return [raw]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert local mock eval cases to LongMemEval JSON."
    )
    parser.add_argument("--set", choices=["green", "red", "both"], default="both")
    parser.add_argument("--mock-path", type=Path, default=DEFAULT_MOCK_PATH)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data" / "evaluation")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--compact", action="store_true", help="Write compact JSON.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    instances = convert_mock_cases(
        set_names=_parse_set(args.set),
        mock_path=args.mock_path,
        data_dir=args.data_dir,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            instances,
            ensure_ascii=False,
            indent=None if args.compact else 2,
            separators=(",", ":") if args.compact else None,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {len(instances)} LongMemEval instances -> {args.output}")


if __name__ == "__main__":
    main()
