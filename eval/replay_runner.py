"""
Akashic-style replay eval for local curated conversations.

This runner treats each EvalCase as an agentic memory benchmark instance:
  1. replay haystack conversations into the session
  2. run consolidation at session boundaries
  3. run post-response invalidation at session boundaries
  4. ask the question through the normal PassiveTurnPipeline
  5. score the final answer and optionally compare with a baseline

There is intentionally no mock execution mode here: no mock embedder, no mock
reasoner, no mock consolidation, and no direct MemoryStore seeding shortcut.
It is separate from eval/runner.py, which is only a faster legacy seeded
regression check.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.core.event_bus import EventBus
from agent.core.types import InboundMessage, OutboundMessage, Session
from agent.pipeline.consolidation_worker import ConsolidationWorker
from agent.pipeline.invalidation_worker import InvalidationWorker
from agent.pipeline.passive_turn import PassiveTurnPipeline
from agent.pipeline.phases.after_reasoning import AfterReasoningPhase
from agent.pipeline.phases.after_turn import AfterTurnPhase
from agent.pipeline.phases.before_reasoning import BeforeReasoningPhase
from agent.pipeline.phases.before_turn import BeforeTurnPhase, _sessions
from agent.pipeline.reasoner import Reasoner
from agent.plugins import PluginManager
from agent.tool_hooks import ToolExecutor
from agent.tools import ToolRegistry
from agent.tools.memory import register_memory_tools
from evaluation.dataset_builder import EvalCase, EvalDataset, QuestionType
from evaluation.metrics import evaluate_single, format_score_report, score_results
from memory.bootstrap import build_memory_runtime, default_markdown_memory_root
from memory.embedder import Embedder
from memory.store import MemoryStore
from persistence.database import get_connection, init_db
from persistence.session_store import get_session_store

_DEFAULT_TIMEOUT_S = 90.0
_EVAL_USER_ID = 9000
_EVAL_CHAT_ID = 9000
_EVAL_QA_CHAT_ID = 9001
_REPLAY_CONVERSATIONS_PATH = (
    Path(__file__).parent.parent / "data" / "evaluation" / "mock_conversations.jsonl"
)
_EVAL_SOURCE_REF = f"session:{_EVAL_USER_ID}:{_EVAL_CHAT_ID}"
logger = logging.getLogger(__name__)


class EvalAdapter:
    def __init__(self) -> None:
        self.sent_messages: list[OutboundMessage] = []

    async def send(self, message: OutboundMessage) -> None:
        self.sent_messages.append(message)


async def _close_async_resource(resource: object | None) -> None:
    if resource is None:
        return
    close = getattr(resource, "close", None) or getattr(resource, "aclose", None)
    if close is None:
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        logger.warning("close failed: %s", exc)


async def _close_pipeline(pipeline: PassiveTurnPipeline) -> None:
    plugin_manager = getattr(pipeline, "plugin_manager", None)
    if plugin_manager is not None:
        await plugin_manager.terminate_all()
    await _close_async_resource(getattr(pipeline, "reasoner", None))


async def _create_pipeline(
    *,
    store: MemoryStore,
    embedder: Embedder,
) -> PassiveTurnPipeline:
    # Use the same passive-chain wiring shape as main.py, but keep eval-local
    # runtime objects so per-case --fresh isolation is not polluted by previous
    # plugin handlers or TAP observers.
    event_bus = EventBus()
    tool_registry = ToolRegistry()
    tool_executor = ToolExecutor()
    session_store = get_session_store()
    memory_runtime = build_memory_runtime(
        embedder=embedder,
        memory_store=store,
        session_store=session_store,
    )
    reasoner = Reasoner(
        tool_registry=tool_registry,
        tool_executor=tool_executor,
        event_bus=event_bus,
    )
    register_memory_tools(tool_registry, memory_runtime.engine)

    plugin_manager = PluginManager(
        [Path.cwd() / "plugins"],
        event_bus=event_bus,
        tool_registry=tool_registry,
        workspace=Path.cwd(),
        memory_engine=memory_runtime.engine,
    )
    await plugin_manager.load_all()
    tool_executor.add_hooks(plugin_manager.tool_hooks)
    reasoner.set_step_modules(
        before_step=plugin_manager.before_step_modules,
        after_step=plugin_manager.after_step_modules,
    )

    before_turn = BeforeTurnPhase(
        event_bus=event_bus,
        plugin_modules=plugin_manager.before_turn_modules,
        memory_engine=memory_runtime.engine,
    )
    before_reasoning = BeforeReasoningPhase(
        benchmark_mode=True,
        tool_registry=tool_registry,
        event_bus=event_bus,
        plugin_modules=plugin_manager.before_reasoning_modules,
        prompt_render_modules=plugin_manager.prompt_render_modules,
        self_model_reader=memory_runtime.markdown.store.read_self,
        long_term_memory_reader=memory_runtime.markdown.store.read_long_term,
        recent_context_reader=memory_runtime.markdown.store.read_recent_context,
    )
    await before_reasoning.preheat()
    after_reasoning = AfterReasoningPhase(
        store,
        event_bus=event_bus,
        plugin_modules=plugin_manager.after_reasoning_modules,
    )
    after_turn = AfterTurnPhase(
        event_bus,
        EvalAdapter(),
        plugin_modules=plugin_manager.after_turn_modules,
    )
    pipeline = PassiveTurnPipeline(
        before_turn=before_turn,
        before_reasoning=before_reasoning,
        reasoner=reasoner,
        after_reasoning=after_reasoning,
        after_turn=after_turn,
        store=store,
        consolidation_worker=None,
        invalidation_worker=None,
        memory_runtime=memory_runtime,
    )
    pipeline.plugin_manager = plugin_manager  # type: ignore[attr-defined]
    return pipeline


def _load_replay_conversations(path: Path = _REPLAY_CONVERSATIONS_PATH) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _select_haystack(case: EvalCase, conversations: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    by_id = {str(c.get("session_id")): c for c in conversations}
    selected = [by_id[sid] for sid in case.context_sessions if sid in by_id]
    if selected:
        return selected, "context_sessions"
    # Existing green/red sets may reference older local corpus ids. Fall back to
    # the full curated corpus so the runner still exercises realistic haystack
    # noise instead of silently evaluating with no history.
    return list(conversations), "all_conversations_fallback"


def _clear_eval_state() -> None:
    conn = get_connection()
    conn.execute("DELETE FROM vec_items")
    conn.execute("DELETE FROM memory_replacements")
    conn.execute("DELETE FROM memory_items")
    conn.execute("DELETE FROM conversation_sessions")
    conn.commit()
    _sessions.clear()
    shutil.rmtree(
        default_markdown_memory_root() / "users" / str(_EVAL_USER_ID),
        ignore_errors=True,
    )


def _save_session(session: Session) -> None:
    get_session_store().save(
        session.user_id,
        session.chat_id,
        session.messages,
        last_consolidated=session.last_consolidated,
    )
    _sessions[(session.user_id, session.chat_id)] = session


async def _finalize_tail(session: Session, store: MemoryStore) -> int:
    """Archive the not-yet-consolidated tail, matching akashic's eval finalization."""
    remaining = session.messages[session.last_consolidated :]
    if not remaining:
        return 0
    temp = Session(
        user_id=session.user_id,
        chat_id=session.chat_id,
        messages=list(remaining),
        last_consolidated=0,
    )
    worker = ConsolidationWorker(keep_count=0, min_new_messages=1)
    written = await worker.consolidate(temp, store, session.user_id, session.chat_id)
    session.last_consolidated = len(session.messages)
    _save_session(session)
    return written


def _last_dialogue_pair(messages: list[dict[str, Any]]) -> tuple[str, str]:
    user_msg = ""
    assistant_msg = ""
    for msg in messages:
        if msg.get("role") == "user":
            user_msg = str(msg.get("content") or "")
            assistant_msg = ""
        elif msg.get("role") == "assistant" and user_msg:
            assistant_msg = str(msg.get("content") or "")
    return user_msg, assistant_msg


async def _replay_haystack(
    *,
    case: EvalCase,
    conversations: list[dict[str, Any]],
    store: MemoryStore,
    embedder: Embedder,
    use_invalidation: bool,
) -> dict[str, Any]:
    session = Session(user_id=_EVAL_USER_ID, chat_id=_EVAL_CHAT_ID)
    # Akashic LongMemEval consolidates at every haystack session boundary, then
    # runs post-response invalidation. Use an eval-specific eager worker so short
    # local sessions still create old memories before an update turn can supersede
    # them.
    consolidation = ConsolidationWorker(keep_count=0, min_new_messages=1)
    invalidation = InvalidationWorker(store, embedder) if use_invalidation else None

    try:
        selected, selection_mode = _select_haystack(case, conversations)
        n_consolidated = 0
        n_invalidated = 0

        for idx, conv in enumerate(selected):
            messages = list(conv.get("messages") or [])
            window_start = len(session.messages)
            session.messages.extend(messages)
            current_source_ref = _source_ref_for_window(
                user_id=_EVAL_USER_ID,
                chat_id=_EVAL_CHAT_ID,
                start=window_start,
                end=len(session.messages) - 1,
            )
            _save_session(session)

            n_consolidated += await consolidation.consolidate(
                session, store, _EVAL_USER_ID, _EVAL_CHAT_ID,
            )
            _save_session(session)

            if invalidation is not None:
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
            "selection_mode": selection_mode,
            "replayed_sessions": len(selected),
            "replayed_messages": len(session.messages),
            "consolidated_memories": n_consolidated,
            "invalidated_memories": n_invalidated,
            "last_consolidated": session.last_consolidated,
        }
    finally:
        await _close_async_resource(invalidation)


def _source_ref_for_window(*, user_id: int, chat_id: int, start: int, end: int) -> str:
    if end < start:
        return f"session:{user_id}:{chat_id}"
    if start == end:
        return f"session:{user_id}:{chat_id}#msg:{start}"
    return f"session:{user_id}:{chat_id}#msg:{start}-{end}"



async def _run_qa(
    pipeline: PassiveTurnPipeline,
    case: EvalCase,
    *,
    qa_chat_id: int,
    timeout_s: float,
    trace: bool,
) -> dict[str, Any]:
    t0 = time.monotonic()
    error: str | None = None
    predicted = ""
    try:
        outbound = await asyncio.wait_for(
            pipeline.execute(
                InboundMessage(
                    user_id=_EVAL_USER_ID,
                    chat_id=qa_chat_id,
                    content=case.question,
                )
            ),
            timeout=timeout_s,
        )
        predicted = outbound.content if outbound else ""
    except asyncio.TimeoutError:
        error = f"timeout after {timeout_s}s"
    except Exception as exc:
        error = str(exc)

    eval_result = evaluate_single(
        predicted_answer=predicted,
        gold_answer=case.gold_answer,
        question=case.question,
    )
    result = {
        "question_id": case.case_id,
        "question_type": case.question_type.value,
        "question": case.question,
        "gold_answer": case.gold_answer,
        "predicted_answer": predicted,
        "token_f1": eval_result["token_f1"],
        "exact_match": eval_result["exact_match"],
        "judge_correct": eval_result.get("rule_judge_correct"),
        "elapsed_s": round(time.monotonic() - t0, 2),
        "error": error,
        "qa_chat_id": qa_chat_id,
        "qa_session_isolated": qa_chat_id != _EVAL_CHAT_ID,
    }
    if trace:
        result["retrieval_trace"] = [
            {
                "id": str(m.id),
                "type": m.memory_type,
                "summary": m.summary[:160],
                "source_ref": m.source_ref,
            }
            for m in pipeline.before_turn.last_retrieved
        ]
        result["retrieval_trace_raw"] = dict(pipeline.before_turn.last_retrieval_trace)
        result["retrieved_memory_block"] = pipeline.before_turn.last_retrieved_memory_block
        reasoner_result = getattr(pipeline, "last_reasoner_result", None)
        if reasoner_result is not None:
            result["tool_calls"] = reasoner_result.tool_calls
    return result


def _load_cases(args: argparse.Namespace) -> list[EvalCase]:
    dataset = EvalDataset()
    cases: list[EvalCase] = []
    if args.set in ("green", "both"):
        cases.extend(dataset.load_green_set())
    if args.set in ("red", "both"):
        cases.extend(dataset.load_red_set())
    if args.limit > 0:
        cases = cases[: args.limit]
    return cases


def _required_tools_for_case(case: EvalCase) -> list[str]:
    required = ["recall_memory"]
    if case.distance_type.value == "exact_match" or case.question_type in {
        QuestionType.SINGLE_SESSION_FACT,
        QuestionType.USER_IDENTITY,
        QuestionType.KNOWLEDGE_UPDATE,
    }:
        required.append("fetch_messages")
    if case.question_type in {
        QuestionType.MULTI_TURN_CONTEXT,
        QuestionType.KNOWLEDGE_UPDATE,
    }:
        required.append("search_messages")
    return required


def _called_tool_names(tool_calls: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for call in tool_calls or []:
        function = call.get("function") if isinstance(call, dict) else None
        if isinstance(function, dict) and function.get("name"):
            names.append(str(function["name"]))
    return names


def _tool_policy_for_case(case: EvalCase, tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    required = _required_tools_for_case(case)
    called = _called_tool_names(tool_calls)
    missing = [name for name in required if name not in called]
    return {
        "required": required,
        "called": called,
        "missing": missing,
        "satisfied": not missing,
    }


def _compare_results(current: list[dict[str, Any]], baseline_path: Path) -> None:
    if not baseline_path.exists():
        print(f"\nBaseline not found: {baseline_path}")
        return
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline_by_id = {r["question_id"]: r for r in baseline.get("results", [])}
    regressions = 0
    improvements = 0
    for r in current:
        base = baseline_by_id.get(r["question_id"])
        if not base:
            continue
        delta = r["token_f1"] - base["token_f1"]
        if (base.get("judge_correct") and not r.get("judge_correct")) or delta < -0.05:
            regressions += 1
            print(f"  REGRESS {r['question_id']}: F1 {base['token_f1']:.3f}->{r['token_f1']:.3f}")
        elif (not base.get("judge_correct") and r.get("judge_correct")) or delta > 0.05:
            improvements += 1
            print(f"  IMPROVE {r['question_id']}: F1 {base['token_f1']:.3f}->{r['token_f1']:.3f}")
    print(f"\nCompare: regressions={regressions} improvements={improvements}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay-style RAG eval")
    parser.add_argument("--set", default="green", choices=["green", "red", "both"])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--fresh", action="store_true", help="Clear eval DB state before each case")
    parser.add_argument("--no-invalidation", action="store_true")
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--timeout", type=float, default=_DEFAULT_TIMEOUT_S)
    parser.add_argument("--compare", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--same-session-qa",
        action="store_true",
        help=(
            "Ask QA in the same chat as haystack replay. Default is isolated QA "
            "chat_id=9001 to evaluate long-memory retrieval without recent-context leakage."
        ),
    )
    return parser


def _validate_eval_args(args: argparse.Namespace) -> None:
    """Enforce AGENTS.md eval invariants before any stateful work starts."""
    if not args.fresh:
        raise SystemExit(
            "replay eval requires --fresh; AGENTS.md requires clearing DB state for every eval run"
        )
    if args.no_invalidation:
        raise SystemExit(
            "replay eval must include invalidation; baseline flow is haystack replay -> "
            "consolidation -> invalidation -> PassiveTurnPipeline QA"
        )


async def _main(args: argparse.Namespace) -> None:
    _validate_eval_args(args)
    init_db()
    conversations = _load_replay_conversations()
    cases = _load_cases(args)
    if not cases:
        print("No eval cases loaded.")
        sys.exit(1)

    print(
        f"\nReplay Eval — LIVE | {args.set.upper()} | {len(cases)} cases"
    )
    print("Mode: haystack replay -> consolidation -> invalidation -> QA")
    qa_chat_id = _EVAL_CHAT_ID if args.same_session_qa else _EVAL_QA_CHAT_ID
    print(
        "QA session: "
        + (
            f"same chat_id={qa_chat_id}"
            if args.same_session_qa
            else f"isolated chat_id={qa_chat_id} (haystack chat_id={_EVAL_CHAT_ID})"
        )
    )

    results: list[dict[str, Any]] = []
    t0 = time.monotonic()
    for i, case in enumerate(cases, 1):
        _clear_eval_state()
        embedder = Embedder()
        store = MemoryStore(embedder)
        try:
            ingest_info = await _replay_haystack(
                case=case,
                conversations=conversations,
                store=store,
                embedder=embedder,
                use_invalidation=not args.no_invalidation,
            )
            pipeline = await _create_pipeline(store=store, embedder=embedder)
            try:
                result = await _run_qa(
                    pipeline,
                    case,
                    qa_chat_id=qa_chat_id,
                    timeout_s=args.timeout,
                    trace=args.trace,
                )
            finally:
                await _close_pipeline(pipeline)
        finally:
            await _close_async_resource(embedder)
        result["ingest"] = ingest_info
        result["tool_policy"] = _tool_policy_for_case(
            case,
            result.get("tool_calls") or [],
        )
        results.append(result)

        status = "ERROR" if result["error"] else ("OK" if result.get("judge_correct") else "MISS")
        print(
            f"[{i:03d}/{len(cases)}] {case.case_id} {case.question_type.value} "
            f"{status} F1={result['token_f1']:.3f} "
            f"mem={ingest_info['consolidated_memories']} "
            f"sup={ingest_info['invalidated_memories']} "
            f"mode={ingest_info['selection_mode']}"
        )
        if result["error"]:
            print(f"  err: {result['error']}")

    scores = score_results(results)
    print("\n" + format_score_report(scores))
    print(f"Elapsed: {time.monotonic() - t0:.1f}s")
    if args.compare:
        _compare_results(results, args.compare)

    output = args.output
    if output is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output = Path(__file__).parent.parent / "data" / "evaluation" / "results" / f"replay_{ts}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "live",
        "fresh": True,
        "set": args.set,
        "n_instances": len(cases),
        "scores": scores,
        "results": results,
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved -> {output}")


def main() -> None:
    asyncio.run(_main(_build_parser().parse_args()))


if __name__ == "__main__":
    main()
