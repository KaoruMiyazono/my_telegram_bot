"""
Retrieval-only probe for replay-style memory experiments.

This runner keeps the AGENTS.md eval invariant for state setup:
  haystack replay -> consolidation -> invalidation

It intentionally stops before PassiveTurnPipeline QA. The output is a local
JSON trace of vector lanes, keyword lane, RRF fusion, and the rendered memory
block that would be injected. Use eval/replay_runner.py for end-to-end answer
quality after a retrieval strategy looks better here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.core.types import MemoryItem, Session
from agent.pipeline.consolidation_worker import ConsolidationWorker
from agent.pipeline.invalidation_worker import InvalidationWorker
from eval.replay_runner import (
    _EVAL_CHAT_ID,
    _EVAL_USER_ID,
    _clear_eval_state,
    _close_async_resource,
    _finalize_tail,
    _last_dialogue_pair,
    _load_replay_conversations,
    _replay_haystack,
    _save_session,
    _select_haystack,
    _source_ref_for_window,
)
from evaluation.dataset_builder import (
    DistanceType,
    EvalCase,
    EvalDataset,
    QuestionType,
)
from memory.bootstrap import build_memory_runtime
from memory.embedder import Embedder
from memory.engine import DefaultMemoryEngine
from memory.store import LONG_TERM_MEMORY_TYPES, MemoryStore
from persistence.database import init_db
from persistence.session_store import get_session_store

_DEFAULT_TOP_K = 8
_DEFAULT_RRF_K = 60
_DEFAULT_STRATEGIES = "current"
_MAX_SUMMARY_CHARS = 260


@dataclass(frozen=True)
class ProbeCase:
    case_id: str
    question: str
    gold_answer: str = ""
    question_type: str = ""
    distance_type: str = ""
    source: str = ""
    notes: str = ""
    context_sessions: list[str] = field(default_factory=list)
    inline_haystack_sessions: list[list[dict[str, Any]]] | None = None
    trace_expectations: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RetrievalStrategy:
    name: str
    use_vector: bool = True
    use_keyword: bool = True
    use_aux: bool = True
    vector_weight: float = 1.0
    keyword_weight: float = 1.0
    rrf_k: int = _DEFAULT_RRF_K


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run retrieval-only memory probe")
    parser.add_argument("--set", default="green", choices=["green", "red", "both"])
    parser.add_argument(
        "--cases-jsonl",
        type=Path,
        default=None,
        help=(
            "Optional Akashic/LangSmith-style JSONL with inputs.haystack_sessions. "
            "When provided, --set is ignored."
        ),
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--question-id",
        action="append",
        default=[],
        help="Run only matching case ids. Repeat for multiple ids.",
    )
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--top-k", type=int, default=_DEFAULT_TOP_K)
    parser.add_argument("--keyword-limit", type=int, default=0)
    parser.add_argument(
        "--strategies",
        default=_DEFAULT_STRATEGIES,
        help=(
            "Comma-separated strategies: current, no_aux, vector_only, "
            "vector_only_no_aux, keyword_only"
        ),
    )
    parser.add_argument("--rrf-k", type=int, default=_DEFAULT_RRF_K)
    parser.add_argument("--vector-weight", type=float, default=1.0)
    parser.add_argument("--keyword-weight", type=float, default=1.0)
    parser.add_argument(
        "--include-superseded",
        action="store_true",
        help="Include superseded memories during retrieval probe.",
    )
    parser.add_argument("--output", type=Path, default=None)
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if not args.fresh:
        raise SystemExit(
            "retrieval probe requires --fresh so every case starts from a clean eval DB"
        )
    if args.top_k <= 0:
        raise SystemExit("--top-k must be positive")
    if args.keyword_limit < 0:
        raise SystemExit("--keyword-limit must be zero or positive")
    _parse_strategy_specs(args)


def _parse_strategy_specs(args: argparse.Namespace) -> list[RetrievalStrategy]:
    names = [name.strip() for name in str(args.strategies or "").split(",") if name.strip()]
    if not names:
        raise SystemExit("--strategies must contain at least one strategy")

    strategies: list[RetrievalStrategy] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        if name == "current":
            strategies.append(
                RetrievalStrategy(
                    name=name,
                    use_vector=True,
                    use_keyword=True,
                    use_aux=True,
                    vector_weight=args.vector_weight,
                    keyword_weight=args.keyword_weight,
                    rrf_k=args.rrf_k,
                )
            )
        elif name == "no_aux":
            strategies.append(
                RetrievalStrategy(
                    name=name,
                    use_vector=True,
                    use_keyword=True,
                    use_aux=False,
                    vector_weight=args.vector_weight,
                    keyword_weight=args.keyword_weight,
                    rrf_k=args.rrf_k,
                )
            )
        elif name == "vector_only":
            strategies.append(
                RetrievalStrategy(
                    name=name,
                    use_vector=True,
                    use_keyword=False,
                    use_aux=True,
                    vector_weight=args.vector_weight,
                    keyword_weight=0.0,
                    rrf_k=args.rrf_k,
                )
            )
        elif name == "vector_only_no_aux":
            strategies.append(
                RetrievalStrategy(
                    name=name,
                    use_vector=True,
                    use_keyword=False,
                    use_aux=False,
                    vector_weight=args.vector_weight,
                    keyword_weight=0.0,
                    rrf_k=args.rrf_k,
                )
            )
        elif name == "keyword_only":
            strategies.append(
                RetrievalStrategy(
                    name=name,
                    use_vector=False,
                    use_keyword=True,
                    use_aux=False,
                    vector_weight=0.0,
                    keyword_weight=args.keyword_weight,
                    rrf_k=args.rrf_k,
                )
            )
        else:
            raise SystemExit(f"unknown retrieval strategy: {name}")
    return strategies


def _load_probe_cases(args: argparse.Namespace) -> list[ProbeCase]:
    if args.cases_jsonl is not None:
        cases = _load_probe_cases_jsonl(args.cases_jsonl)
    else:
        cases = _load_probe_cases_from_eval_set(args.set)

    if args.question_id:
        wanted = {str(value) for value in args.question_id}
        cases = [case for case in cases if case.case_id in wanted]
        found = {case.case_id for case in cases}
        missing = sorted(wanted - found)
        if missing:
            raise SystemExit(f"case ids not found: {', '.join(missing)}")
    if args.limit > 0:
        cases = cases[: args.limit]
    return cases


def _load_probe_cases_from_eval_set(name: str) -> list[ProbeCase]:
    dataset = EvalDataset()
    eval_cases: list[EvalCase] = []
    if name in ("green", "both"):
        eval_cases.extend(dataset.load_green_set())
    if name in ("red", "both"):
        eval_cases.extend(dataset.load_red_set())
    return [_probe_case_from_eval_case(case) for case in eval_cases]


def _probe_case_from_eval_case(case: EvalCase) -> ProbeCase:
    return ProbeCase(
        case_id=case.case_id,
        question=case.question,
        gold_answer=case.gold_answer,
        question_type=case.question_type.value,
        distance_type=case.distance_type.value,
        source=case.source,
        notes=case.notes,
        context_sessions=list(case.context_sessions),
    )


def _load_probe_cases_jsonl(path: Path) -> list[ProbeCase]:
    cases: list[ProbeCase] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"invalid JSONL at {path}:{lineno}: {exc}") from exc
        inputs = row.get("inputs") or {}
        outputs = row.get("outputs") or {}
        metadata = row.get("metadata") or {}
        trace_expectations = metadata.get("trace_expectations") or {}
        cases.append(
            ProbeCase(
                case_id=str(inputs.get("question_id") or f"line-{lineno}"),
                question=str(inputs.get("question") or ""),
                gold_answer=str(outputs.get("answer") or ""),
                question_type=str(metadata.get("original_question_type") or ""),
                distance_type=str(metadata.get("distance_type") or ""),
                source=str(metadata.get("source") or "jsonl"),
                notes=str(metadata.get("notes") or ""),
                context_sessions=[
                    str(value) for value in inputs.get("haystack_session_ids") or []
                ],
                inline_haystack_sessions=list(inputs.get("haystack_sessions") or []),
                trace_expectations=dict(trace_expectations),
                metadata=dict(metadata),
            )
        )
    return cases


async def _replay_probe_case(
    *,
    case: ProbeCase,
    conversations: list[dict[str, Any]],
    store: MemoryStore,
    embedder: Embedder,
) -> dict[str, Any]:
    if case.inline_haystack_sessions is not None:
        return await _replay_inline_haystack(case=case, store=store, embedder=embedder)

    eval_case = _eval_case_from_probe_case(case)
    selected, selection_mode = _select_haystack(eval_case, conversations)
    ingest = await _replay_haystack(
        case=eval_case,
        conversations=conversations,
        store=store,
        embedder=embedder,
        use_invalidation=True,
    )
    ingest["selection_mode"] = selection_mode
    ingest["selected_session_ids"] = [
        str(item.get("session_id") or "") for item in selected
    ]
    return ingest


def _eval_case_from_probe_case(case: ProbeCase) -> EvalCase:
    try:
        question_type = QuestionType(case.question_type)
    except ValueError:
        question_type = QuestionType.SINGLE_SESSION_FACT
    try:
        distance_type = DistanceType(case.distance_type)
    except ValueError:
        distance_type = DistanceType.SEMANTIC_SIMILARITY
    return EvalCase(
        case_id=case.case_id,
        question=case.question,
        gold_answer=case.gold_answer,
        question_type=question_type,
        context_sessions=list(case.context_sessions),
        distance_type=distance_type,
        source=case.source,
        notes=case.notes,
    )


async def _replay_inline_haystack(
    *,
    case: ProbeCase,
    store: MemoryStore,
    embedder: Embedder,
) -> dict[str, Any]:
    session = Session(user_id=_EVAL_USER_ID, chat_id=_EVAL_CHAT_ID)
    consolidation = ConsolidationWorker(keep_count=0, min_new_messages=1)
    invalidation = InvalidationWorker(store, embedder)

    try:
        n_consolidated = 0
        n_invalidated = 0
        for raw_session in case.inline_haystack_sessions or []:
            window_start = len(session.messages)
            messages = _clean_inline_messages(raw_session)
            session.messages.extend(messages)
            current_source_ref = _source_ref_for_window(
                user_id=_EVAL_USER_ID,
                chat_id=_EVAL_CHAT_ID,
                start=window_start,
                end=len(session.messages) - 1,
            )
            _save_session(session)

            n_consolidated += await consolidation.consolidate(
                session,
                store,
                _EVAL_USER_ID,
                _EVAL_CHAT_ID,
            )
            _save_session(session)

            user_msg, assistant_msg = _last_dialogue_pair(messages)
            if user_msg:
                superseded = await invalidation.run(
                    user_msg=user_msg,
                    agent_response=assistant_msg,
                    tool_calls=[],
                    user_id=_EVAL_USER_ID,
                    chat_id=_EVAL_CHAT_ID,
                    source_ref=current_source_ref,
                )
                n_invalidated += len(superseded)

        n_consolidated += await _finalize_tail(session, store)
        return {
            "selection_mode": "inline_haystack_sessions",
            "replayed_sessions": len(case.inline_haystack_sessions or []),
            "replayed_messages": len(session.messages),
            "consolidated_memories": n_consolidated,
            "invalidated_memories": n_invalidated,
            "last_consolidated": session.last_consolidated,
        }
    finally:
        await _close_async_resource(invalidation)


def _clean_inline_messages(raw_session: list[dict[str, Any]]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for turn in raw_session or []:
        content = str(turn.get("content") or "")
        if not content.strip():
            continue
        messages.append(
            {
                "role": str(turn.get("role") or "user"),
                "content": content,
            }
        )
    return messages


async def _probe_case_retrieval(
    *,
    case: ProbeCase,
    engine: DefaultMemoryEngine,
    store: MemoryStore,
    embedder: Embedder,
    strategies: list[RetrievalStrategy],
    top_k: int,
    keyword_limit: int,
    include_superseded: bool,
) -> list[dict[str, Any]]:
    query = case.question.strip()
    needs_aux = any(strategy.use_aux for strategy in strategies)
    aux_queries = await engine._build_aux_queries(query) if needs_aux else []
    results: list[dict[str, Any]] = []
    for strategy in strategies:
        strategy_aux = aux_queries if strategy.use_aux else []
        result = await _run_strategy_probe(
            case=case,
            query=query,
            aux_queries=strategy_aux,
            store=store,
            embedder=embedder,
            strategy=strategy,
            top_k=top_k,
            keyword_limit=keyword_limit,
            include_superseded=include_superseded,
        )
        results.append(result)
    return results


async def _run_strategy_probe(
    *,
    case: ProbeCase,
    query: str,
    aux_queries: list[str],
    store: MemoryStore,
    embedder: Embedder,
    strategy: RetrievalStrategy,
    top_k: int,
    keyword_limit: int,
    include_superseded: bool,
) -> dict[str, Any]:
    query_texts = _dedupe_texts([query, *aux_queries])
    vector_lanes: list[dict[str, Any]] = []
    vector_results: list[MemoryItem] = []
    vector_seen: set[str] = set()
    vector_lane_counts: list[int] = []
    vector_lane_new_counts: list[int] = []

    if strategy.use_vector:
        for lane_index, query_text in enumerate(query_texts):
            query_vec = await embedder.embed(query_text)
            lane_results = await store.vector_search(
                query_vec=query_vec,
                user_id=_EVAL_USER_ID,
                top_k=top_k,
                memory_types=LONG_TERM_MEMORY_TYPES,
                include_superseded=include_superseded,
            )
            vector_lane_counts.append(len(lane_results))
            vector_lanes.append(
                {
                    "lane_index": lane_index,
                    "query": query_text,
                    "count": len(lane_results),
                    "items": [
                        _memory_payload(mem, rank=rank)
                        for rank, mem in enumerate(lane_results, 1)
                    ],
                }
            )
            new_count = 0
            for mem in lane_results:
                mid = str(mem.id)
                if mid in vector_seen:
                    continue
                vector_seen.add(mid)
                vector_results.append(mem)
                new_count += 1
            vector_lane_new_counts.append(new_count)

    kw_results: list[MemoryItem] = []
    if strategy.use_keyword:
        kw_results = await store.keyword_search(
            terms=query,
            user_id=_EVAL_USER_ID,
            limit=keyword_limit or top_k,
            memory_types=LONG_TERM_MEMORY_TYPES,
            include_superseded=include_superseded,
        )

    fused, rrf_scores, lanes_by_id = _rrf_fuse_probe(
        vector_results,
        kw_results,
        strategy=strategy,
    )
    fused_payload = [
        _memory_payload(
            mem,
            rank=rank,
            rrf_score=rrf_scores.get(str(mem.id)),
            lanes=lanes_by_id.get(str(mem.id), []),
        )
        for rank, mem in enumerate(fused, 1)
    ]
    top_items = fused[:top_k]
    return {
        "strategy": {
            "name": strategy.name,
            "use_vector": strategy.use_vector,
            "use_keyword": strategy.use_keyword,
            "use_aux": strategy.use_aux,
            "rrf_k": strategy.rrf_k,
            "vector_weight": strategy.vector_weight,
            "keyword_weight": strategy.keyword_weight,
        },
        "query": query,
        "aux_queries": list(aux_queries),
        "trace": {
            "retrieval_mode": _retrieval_mode(strategy),
            "fusion": "rrf",
            "vector_query_count": len(query_texts) if strategy.use_vector else 0,
            "vector_count": len(vector_results),
            "keyword_count": len(kw_results),
            "fused_count": len(fused),
            "vector_lane_counts": vector_lane_counts,
            "vector_lane_new_counts": vector_lane_new_counts,
            "keyword_limit": keyword_limit or top_k,
        },
        "vector_lanes": vector_lanes,
        "vector_results": [
            _memory_payload(mem, rank=rank)
            for rank, mem in enumerate(vector_results, 1)
        ],
        "keyword_results": [
            _memory_payload(mem, rank=rank)
            for rank, mem in enumerate(kw_results, 1)
        ],
        "fused_results": fused_payload,
        "retrieved_memory_block": _render_memory_block(top_items),
        "hit": _hit_summary(case, fused_payload),
    }


def _retrieval_mode(strategy: RetrievalStrategy) -> str:
    if strategy.use_vector and strategy.use_keyword:
        return "hybrid_rrf"
    if strategy.use_vector:
        return "vector_only"
    if strategy.use_keyword:
        return "keyword_only"
    return "empty"


def _rrf_fuse_probe(
    vec_results: list[MemoryItem],
    kw_results: list[MemoryItem],
    *,
    strategy: RetrievalStrategy,
) -> tuple[list[MemoryItem], dict[str, float], dict[str, list[str]]]:
    fused: dict[str, dict[str, Any]] = {}
    lanes_by_id: dict[str, list[str]] = {}
    rrf_k = max(1, int(strategy.rrf_k))

    for rank, mem in enumerate(vec_results, 1):
        mid = str(mem.id)
        fused.setdefault(mid, {"mem": mem, "rrf_score": 0.0})
        fused[mid]["rrf_score"] += strategy.vector_weight / (rrf_k + rank)
        _append_lane(lanes_by_id, mid, "vector")
    for rank, mem in enumerate(kw_results, 1):
        mid = str(mem.id)
        fused.setdefault(mid, {"mem": mem, "rrf_score": 0.0})
        fused[mid]["rrf_score"] += strategy.keyword_weight / (rrf_k + rank)
        _append_lane(lanes_by_id, mid, "keyword")

    sorted_items = sorted(
        fused.values(),
        key=lambda item: item["rrf_score"],
        reverse=True,
    )
    items = [item["mem"] for item in sorted_items]
    scores = {
        str(item["mem"].id): round(float(item["rrf_score"]), 8)
        for item in sorted_items
    }
    return items, scores, lanes_by_id


def _append_lane(lanes_by_id: dict[str, list[str]], item_id: str, lane: str) -> None:
    lanes = lanes_by_id.setdefault(item_id, [])
    if lane not in lanes:
        lanes.append(lane)


def _memory_payload(
    mem: MemoryItem,
    *,
    rank: int,
    rrf_score: float | None = None,
    lanes: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "rank": rank,
        "id": str(mem.id),
        "type": mem.memory_type,
        "summary": mem.summary[:_MAX_SUMMARY_CHARS],
        "status": mem.status,
        "source_ref": mem.source_ref,
        "created_at": _json_safe(mem.created_at),
        "updated_at": _json_safe(mem.updated_at),
    }
    if rrf_score is not None:
        payload["rrf_score"] = round(float(rrf_score), 8)
    if lanes is not None:
        payload["lanes"] = list(lanes)
    return payload


def _render_memory_block(items: list[MemoryItem]) -> str:
    lines = []
    for mem in items:
        source = f" [{mem.source_ref}]" if mem.source_ref else ""
        lines.append(f"- {mem.summary}{source}")
    return "\n".join(lines)


def _hit_summary(case: ProbeCase, fused_payload: list[dict[str, Any]]) -> dict[str, Any]:
    expectations = _expectations_for_case(case)
    best_rank: int | None = None
    matches: list[dict[str, Any]] = []

    for item in fused_payload:
        rank = int(item.get("rank") or 0)
        summary = str(item.get("summary") or "")
        source_ref = str(item.get("source_ref") or "")
        status = str(item.get("status") or "")
        item_matches: list[str] = []

        for expected_ref in expectations["source_refs"]:
            if source_ref == expected_ref:
                item_matches.append(f"source_ref:{expected_ref}")

        normalized_summary = _normalize_for_contains(summary)
        for phrase in expectations["contains"]:
            if _normalize_for_contains(phrase) in normalized_summary:
                item_matches.append(f"contains:{phrase}")

        for term in expectations["gold_terms"]:
            if _normalize_for_contains(term) in normalized_summary:
                item_matches.append(f"gold_term:{term}")

        if status == "superseded":
            for phrase in expectations["superseded_contains"]:
                if _normalize_for_contains(phrase) in normalized_summary:
                    item_matches.append(f"superseded_contains:{phrase}")

        if item_matches:
            if best_rank is None or rank < best_rank:
                best_rank = rank
            matches.append(
                {
                    "rank": rank,
                    "id": item.get("id"),
                    "source_ref": source_ref,
                    "matches": item_matches,
                }
            )

    scored = bool(
        expectations["source_refs"]
        or expectations["contains"]
        or expectations["gold_terms"]
        or expectations["superseded_contains"]
    )
    return {
        "scored": scored,
        "best_rank": best_rank,
        "hit_at_1": best_rank is not None and best_rank <= 1,
        "hit_at_3": best_rank is not None and best_rank <= 3,
        "hit_at_8": best_rank is not None and best_rank <= 8,
        "expectations": expectations,
        "matches": matches,
    }


def _expectations_for_case(case: ProbeCase) -> dict[str, Any]:
    trace = case.trace_expectations or {}
    source_refs = _string_list(trace.get("expected_source_refs"))
    contains = _string_list(trace.get("active_contains"))
    superseded_contains = _string_list(trace.get("superseded_contains"))
    gold_terms = _gold_signal_terms(case.gold_answer)
    return {
        "source_refs": source_refs,
        "contains": contains,
        "superseded_contains": superseded_contains,
        "gold_terms": gold_terms,
        "gold_terms_are_heuristic": bool(gold_terms)
        and not (source_refs or contains or superseded_contains),
    }


def _string_list(value: Any) -> list[str]:
    items = value if isinstance(value, list) else []
    result: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _gold_signal_terms(answer: str) -> list[str]:
    text = str(answer or "").strip()
    if not text:
        return []
    terms: list[str] = []

    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", text):
        if len(token) >= 2 and token not in terms:
            terms.append(token)

    cleaned = re.sub(r"[，。！？、；：,.!?;:\s]+", " ", text)
    patterns = [
        r"(?:是|在|用|喝|推荐|偏好|喜欢|改喝|到期日是|生日是)([^ ，。！？、；：,.!?;:]+)",
        r"(?:答出|包含)([^ ，。！？、；：,.!?;:]+)",
    ]
    for pattern in patterns:
        for raw in re.findall(pattern, cleaned):
            term = _clean_gold_term(raw)
            if term and term not in terms:
                terms.append(term)

    if not terms:
        for raw in re.findall(r"[\u4e00-\u9fff]{2,8}", cleaned):
            term = _clean_gold_term(raw)
            if term and term not in terms:
                terms.append(term)
    return terms[:8]


def _clean_gold_term(value: str) -> str:
    term = str(value or "").strip()
    for prefix in (
        "你的",
        "你",
        "个",
        "一种",
        "当前",
        "现在",
        "之前",
        "以前",
        "喝",
        "用",
        "在",
    ):
        if term.startswith(prefix):
            term = term[len(prefix) :]
    for suffix in ("了", "的", "。", "，"):
        if term.endswith(suffix):
            term = term[: -len(suffix)]
    if term in {"可以", "根据", "因为", "已经", "不是", "没有", "推荐"}:
        return ""
    if len(term) == 1 and term not in {"茶"}:
        return ""
    return term


def _normalize_for_contains(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").lower())


def _dedupe_texts(texts: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in texts:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _memory_snapshot(store: MemoryStore) -> dict[str, Any]:
    memories = store.list_memories(
        user_id=_EVAL_USER_ID,
        memory_types=LONG_TERM_MEMORY_TYPES,
        include_superseded=True,
        limit=200,
    )
    return {
        "count": len(memories),
        "active_count": sum(1 for mem in memories if mem.status == "active"),
        "superseded_count": sum(1 for mem in memories if mem.status == "superseded"),
        "items": [_memory_payload(mem, rank=rank) for rank, mem in enumerate(memories, 1)],
    }


def _aggregate_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_strategy: dict[str, dict[str, Any]] = {}
    for case_result in results:
        for strategy_result in case_result.get("strategies", []):
            name = str(strategy_result.get("strategy", {}).get("name") or "unknown")
            hit = strategy_result.get("hit") or {}
            bucket = by_strategy.setdefault(
                name,
                {
                    "cases": 0,
                    "scored_cases": 0,
                    "hit_at_1": 0,
                    "hit_at_3": 0,
                    "hit_at_8": 0,
                    "misses": [],
                },
            )
            bucket["cases"] += 1
            if not hit.get("scored"):
                continue
            bucket["scored_cases"] += 1
            for key in ("hit_at_1", "hit_at_3", "hit_at_8"):
                if hit.get(key):
                    bucket[key] += 1
            if hit.get("best_rank") is None:
                bucket["misses"].append(case_result.get("case_id"))

    for bucket in by_strategy.values():
        scored = int(bucket["scored_cases"])
        for key in ("hit_at_1", "hit_at_3", "hit_at_8"):
            bucket[f"{key}_rate"] = round(bucket[key] / scored, 4) if scored else None
    return {"by_strategy": by_strategy}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(key): _json_safe(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


async def _main(args: argparse.Namespace) -> None:
    _validate_args(args)
    init_db()
    strategies = _parse_strategy_specs(args)
    cases = _load_probe_cases(args)
    if not cases:
        raise SystemExit("No probe cases loaded.")

    conversations = [] if args.cases_jsonl is not None else _load_replay_conversations()
    print(
        f"\nRetrieval Probe | {len(cases)} cases | "
        f"strategies={','.join(strategy.name for strategy in strategies)}"
    )
    print("Mode: haystack replay -> consolidation -> invalidation -> retrieval only")

    results: list[dict[str, Any]] = []
    started = time.monotonic()
    for index, case in enumerate(cases, 1):
        _clear_eval_state()
        embedder = Embedder()
        store = MemoryStore(embedder)
        try:
            ingest = await _replay_probe_case(
                case=case,
                conversations=conversations,
                store=store,
                embedder=embedder,
            )
            memory_runtime = build_memory_runtime(
                embedder=embedder,
                memory_store=store,
                session_store=get_session_store(),
            )
            engine = memory_runtime.engine
            if not isinstance(engine, DefaultMemoryEngine):
                raise TypeError("retrieval probe expects DefaultMemoryEngine")
            strategy_results = await _probe_case_retrieval(
                case=case,
                engine=engine,
                store=store,
                embedder=embedder,
                strategies=strategies,
                top_k=args.top_k,
                keyword_limit=args.keyword_limit,
                include_superseded=args.include_superseded,
            )
            snapshot = _memory_snapshot(store)
        finally:
            await _close_async_resource(embedder)

        case_result = {
            "case_id": case.case_id,
            "question": case.question,
            "gold_answer": case.gold_answer,
            "question_type": case.question_type,
            "distance_type": case.distance_type,
            "source": case.source,
            "notes": case.notes,
            "context_sessions": case.context_sessions,
            "trace_expectations": case.trace_expectations,
            "ingest": ingest,
            "memory_snapshot": snapshot,
            "strategies": strategy_results,
        }
        results.append(case_result)

        status_bits = []
        for strategy_result in strategy_results:
            hit = strategy_result["hit"]
            best = hit.get("best_rank")
            status_bits.append(
                f"{strategy_result['strategy']['name']}:"
                + (f"hit@{best}" if best is not None else "miss")
            )
        print(
            f"[{index:03d}/{len(cases)}] {case.case_id} "
            f"mem={ingest['consolidated_memories']} "
            f"sup={ingest['invalidated_memories']} "
            + " ".join(status_bits)
        )

    aggregate = _aggregate_results(results)
    elapsed = time.monotonic() - started
    print(f"\nElapsed: {elapsed:.1f}s")
    for name, bucket in aggregate["by_strategy"].items():
        print(
            f"{name}: scored={bucket['scored_cases']} "
            f"hit@1={bucket['hit_at_1_rate']} "
            f"hit@3={bucket['hit_at_3_rate']} "
            f"hit@8={bucket['hit_at_8_rate']}"
        )

    output = args.output
    if output is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output = (
            Path(__file__).parent.parent
            / "data"
            / "evaluation"
            / "results"
            / f"retrieval_probe_{ts}.json"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "retrieval_probe",
        "fresh": True,
        "top_k": args.top_k,
        "keyword_limit": args.keyword_limit or args.top_k,
        "include_superseded": args.include_superseded,
        "strategies": [_json_safe(strategy.__dict__) for strategy in strategies],
        "n_instances": len(cases),
        "aggregate": aggregate,
        "results": results,
    }
    output.write_text(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved -> {output}")


def main() -> None:
    asyncio.run(_main(_build_parser().parse_args()))


if __name__ == "__main__":
    main()
