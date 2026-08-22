"""
LangSmith experiment runner for the replay-style memory benchmark.

This is intentionally shaped like eval/replay_runner.py:
  1. clear eval state (--fresh is mandatory)
  2. replay each example's haystack_sessions into SessionStore
  3. run consolidation and post-response invalidation at session boundaries
  4. ask the question through the real PassiveTurnPipeline
  5. upload answer + diagnostics to LangSmith

The dataset is expected to use the Akashic-style JSONL shape:
  inputs.question_id, inputs.question, inputs.haystack_sessions, ...
  outputs.answer
  metadata.required_tools / trace_expectations are optional analysis hints.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - python-dotenv is installed via pydantic-settings
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv(_PROJECT_ROOT / ".env")
os.environ.setdefault("TG_BOT_TOKEN", "langsmith_eval_dummy")
if not os.environ.get("LANGSMITH_API_KEY") and os.environ.get("LANGCHAIN_API_KEY"):
    os.environ["LANGSMITH_API_KEY"] = os.environ["LANGCHAIN_API_KEY"]
if not os.environ.get("LANGSMITH_ENDPOINT") and os.environ.get("LANGCHAIN_ENDPOINT"):
    os.environ["LANGSMITH_ENDPOINT"] = os.environ["LANGCHAIN_ENDPOINT"]

try:
    from langsmith import Client, aevaluate, get_current_run_tree
except ModuleNotFoundError as exc:  # pragma: no cover - user-facing setup guard
    raise SystemExit(
        "Missing dependency: langsmith. Install it with `poetry add langsmith` "
        "or `pip install -U langsmith`."
    ) from exc

from agent.core.types import InboundMessage, Session
from agent.pipeline.consolidation_worker import ConsolidationWorker
from agent.pipeline.invalidation_worker import InvalidationWorker
from eval.replay_runner import (
    _EVAL_CHAT_ID,
    _EVAL_QA_CHAT_ID,
    _EVAL_USER_ID,
    _close_async_resource,
    _close_pipeline,
    _clear_eval_state,
    _create_pipeline,
    _finalize_tail,
    _last_dialogue_pair,
    _save_session,
    _tool_policy_for_case,
)
from evaluation.dataset_builder import DistanceType, EvalCase, QuestionType
from evaluation.metrics import evaluate_single
from memory.bootstrap import default_markdown_memory_root
from memory.embedder import Embedder
from memory.markdown_store import MarkdownMemoryStore
from memory.store import LONG_TERM_MEMORY_TYPES, MemoryStore
from persistence.database import init_db

_DEFAULT_TIMEOUT_S = 90.0
_DEFAULT_DATASET = "base_memory_eval"


def _question_type(raw: str | None) -> QuestionType:
    try:
        return QuestionType(str(raw or "single_session_fact"))
    except ValueError:
        return QuestionType.SINGLE_SESSION_FACT


def _distance_type(raw: str | None) -> DistanceType:
    try:
        return DistanceType(str(raw or "semantic_similarity"))
    except ValueError:
        return DistanceType.SEMANTIC_SIMILARITY


def _case_for_policy(inputs: dict[str, Any]) -> EvalCase:
    metadata = inputs.get("_metadata") or {}
    return EvalCase(
        case_id=str(inputs.get("question_id") or "langsmith-example"),
        question=str(inputs.get("question") or ""),
        gold_answer=str(inputs.get("_reference_answer") or ""),
        question_type=_question_type(metadata.get("original_question_type")),
        context_sessions=list(inputs.get("haystack_session_ids") or []),
        distance_type=_distance_type(metadata.get("distance_type")),
        source=str(metadata.get("source") or "langsmith"),
        notes=str(metadata.get("notes") or ""),
    )


class _EvalTrace:
    """Small LangSmith child-run helper for eval-only observability."""

    def __init__(self, *, prompt_visibility: str = "preview") -> None:
        self.prompt_visibility = prompt_visibility
        try:
            self._parent = get_current_run_tree()
        except Exception:
            self._parent = None

    def start(
        self,
        name: str,
        *,
        inputs: dict[str, Any] | None = None,
        parent: Any | None = None,
        run_type: str = "chain",
    ) -> Any | None:
        base = parent or self._parent
        if base is None:
            return None
        try:
            child = base.create_child(
                name,
                run_type=run_type,
                inputs=_json_safe(inputs or {}),
            )
            child.post()
            return child
        except Exception:
            return None

    def end(
        self,
        run: Any | None,
        *,
        outputs: dict[str, Any] | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if run is None:
            return
        try:
            run.end(
                outputs=_json_safe(outputs or {}),
                error=error,
                metadata=_json_safe(metadata) if metadata else None,
            )
            run.patch()
        except Exception:
            return

    def record(
        self,
        name: str,
        *,
        inputs: dict[str, Any] | None = None,
        outputs: dict[str, Any] | None = None,
        parent: Any | None = None,
        run_type: str = "chain",
        error: str | None = None,
    ) -> None:
        run = self.start(name, inputs=inputs, parent=parent, run_type=run_type)
        self.end(run, outputs=outputs, error=error)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except Exception:
            pass
    return str(value)


def _text_payload(text: str, *, mode: str, max_chars: int = 800) -> dict[str, Any]:
    content = str(text or "")
    payload: dict[str, Any] = {
        "chars": len(content),
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }
    if mode == "full":
        payload["content"] = content
    elif mode == "hash":
        pass
    else:
        payload["preview"] = content[:max_chars]
    return payload


def _message_previews(
    messages: list[dict[str, Any]],
    *,
    start_seq: int = 0,
    max_items: int = 8,
    max_chars: int = 240,
) -> list[dict[str, Any]]:
    previews: list[dict[str, Any]] = []
    for offset, msg in enumerate(messages[:max_items]):
        previews.append(
            {
                "seq": start_seq + offset,
                "role": str(msg.get("role") or ""),
                "content": str(msg.get("content") or "")[:max_chars],
            }
        )
    if len(messages) > max_items:
        previews.append({"omitted": len(messages) - max_items})
    return previews


def _memory_payload(memory: Any, *, max_summary_chars: int = 240) -> dict[str, Any]:
    return {
        "id": str(getattr(memory, "id", "")),
        "type": str(getattr(memory, "memory_type", "")),
        "summary": str(getattr(memory, "summary", ""))[:max_summary_chars],
        "status": str(getattr(memory, "status", "")),
        "source_ref": getattr(memory, "source_ref", None),
        "created_at": _json_safe(getattr(memory, "created_at", "")),
        "updated_at": _json_safe(getattr(memory, "updated_at", "")),
    }


def _memory_snapshot(store: MemoryStore, *, user_id: int) -> dict[str, Any]:
    memories = store.list_memories(
        user_id=user_id,
        memory_types=LONG_TERM_MEMORY_TYPES,
        include_superseded=True,
        limit=200,
    )
    items = [_memory_payload(memory) for memory in memories]
    return {
        "count": len(items),
        "active_count": sum(1 for item in items if item.get("status") == "active"),
        "superseded_count": sum(
            1 for item in items if item.get("status") == "superseded"
        ),
        "items": items,
    }


def _memory_delta(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    before_ids = {str(item.get("id")) for item in before.get("items", [])}
    return [
        item
        for item in after.get("items", [])
        if str(item.get("id")) not in before_ids
    ]


def _compact_memory_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "count": snapshot.get("count", 0),
        "active_count": snapshot.get("active_count", 0),
        "superseded_count": snapshot.get("superseded_count", 0),
        "items": snapshot.get("items", [])[:30],
    }


def _markdown_snapshot(
    markdown_store: MarkdownMemoryStore,
    *,
    user_id: int,
    visibility: str,
) -> dict[str, Any]:
    markdown_store.ensure_user(user_id)
    files = {
        "PENDING.md": markdown_store.read_pending(user_id),
        "HISTORY.md": _read_markdown_file(markdown_store, user_id, "HISTORY.md"),
        "RECENT_CONTEXT.md": markdown_store.read_recent_context(user_id),
        "MEMORY.md": markdown_store.read_long_term(user_id),
    }
    return {
        name: _text_payload(text, mode=visibility, max_chars=1200)
        for name, text in files.items()
    }


def _read_markdown_file(
    markdown_store: MarkdownMemoryStore,
    user_id: int,
    name: str,
) -> str:
    path = markdown_store._memory_file(user_id, name)  # eval-only snapshot helper
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _prompt_sections_payload(
    sections: list[Any],
    *,
    visibility: str,
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for section in sections:
        content = str(getattr(section, "content", "") or "")
        item = {
            "name": str(getattr(section, "name", "") or ""),
            "is_static": bool(getattr(section, "is_static", False)),
            "cache_hit": bool(getattr(section, "cache_hit", False)),
            "estimated_tokens": max(1, len(content) // 3) if content else 0,
        }
        item.update(_text_payload(content, mode=visibility, max_chars=1000))
        payload.append(item)
    return payload


def _load_json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw or ""))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _called_tool_names(tool_calls: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for call in tool_calls or []:
        function = call.get("function") if isinstance(call, dict) else None
        if isinstance(function, dict) and function.get("name"):
            names.append(str(function["name"]))
    return names


def _required_tools_from_inputs(inputs: dict[str, Any]) -> list[str]:
    metadata = inputs.get("_metadata") or {}
    trace_expectations = metadata.get("trace_expectations") or {}
    raw_required = (
        metadata.get("required_tools")
        or trace_expectations.get("required_tools")
        or []
    )
    required: list[str] = []
    for value in raw_required:
        name = str(value or "").strip()
        if name and name not in required:
            required.append(name)
    return required


def _tool_policy_for_inputs(
    inputs: dict[str, Any],
    tool_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    required = _required_tools_from_inputs(inputs)
    if not required:
        return _tool_policy_for_case(_case_for_policy(inputs), tool_calls)
    called = _called_tool_names(tool_calls)
    missing = [name for name in required if name not in called]
    return {
        "required": required,
        "called": called,
        "missing": missing,
        "satisfied": not missing,
    }


def _tool_payloads(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for index, call in enumerate(tool_calls or [], 1):
        raw_function = call.get("function") if isinstance(call, dict) else {}
        function = raw_function if isinstance(raw_function, dict) else {}
        args = _load_json_object(function.get("arguments"))
        result_raw = str(call.get("result") or "")
        result = _load_json_object(result_raw)
        payloads.append(
            {
                "index": index,
                "name": str(function.get("name") or ""),
                "guard": call.get("guard"),
                "arguments": args,
                "result": result,
                "result_text": _text_payload(result_raw, mode="preview", max_chars=1200),
            }
        )
    return payloads


async def _replay_haystack_from_inputs(
    *,
    inputs: dict[str, Any],
    store: MemoryStore,
    embedder: Embedder,
    markdown_store: MarkdownMemoryStore,
    trace: _EvalTrace,
    parent_run: Any | None = None,
) -> dict[str, Any]:
    session = Session(user_id=_EVAL_USER_ID, chat_id=_EVAL_CHAT_ID)
    consolidation = ConsolidationWorker(
        keep_count=0,
        min_new_messages=1,
        markdown_store=markdown_store,
    )
    invalidation = InvalidationWorker(store, embedder)

    try:
        sessions = list(inputs.get("haystack_sessions") or [])
        n_consolidated = 0
        n_invalidated = 0

        for session_index, raw_session in enumerate(sessions):
            window_start = len(session.messages)
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

            session.messages.extend(messages)
            current_source_ref = _source_ref_for_window(
                user_id=_EVAL_USER_ID,
                chat_id=_EVAL_CHAT_ID,
                start=window_start,
                end=len(session.messages) - 1,
            )
            _save_session(session)

            before_consolidation = _memory_snapshot(store, user_id=_EVAL_USER_ID)
            consolidation_run = trace.start(
                "memory.consolidation.window",
                parent=parent_run,
                inputs={
                    "question_id": str(inputs.get("question_id") or ""),
                    "session_index": session_index,
                    "window_start": window_start,
                    "window_end": len(session.messages) - 1,
                    "source_ref": current_source_ref,
                    "message_count": len(messages),
                    "messages": _message_previews(messages, start_seq=window_start),
                },
            )
            try:
                written = await consolidation.consolidate(
                    session,
                    store,
                    _EVAL_USER_ID,
                    _EVAL_CHAT_ID,
                )
                n_consolidated += written
                after_consolidation = _memory_snapshot(store, user_id=_EVAL_USER_ID)
                trace.end(
                    consolidation_run,
                    outputs={
                        "written_count": written,
                        "last_consolidated": session.last_consolidated,
                        "new_memories": _memory_delta(
                            before_consolidation,
                            after_consolidation,
                        ),
                        "memory_snapshot": _compact_memory_snapshot(
                            after_consolidation
                        ),
                    },
                )
            except Exception as exc:
                trace.end(consolidation_run, error=str(exc))
                raise
            _save_session(session)

            user_msg, assistant_msg = _last_dialogue_pair(messages)
            if user_msg:
                before_invalidation = _memory_snapshot(store, user_id=_EVAL_USER_ID)
                invalidation_run = trace.start(
                    "memory.invalidation",
                    parent=parent_run,
                    inputs={
                        "question_id": str(inputs.get("question_id") or ""),
                        "session_index": session_index,
                        "source_ref": current_source_ref,
                        "user_msg": user_msg[:500],
                        "agent_response": assistant_msg[:500],
                    },
                )
                try:
                    superseded = await invalidation.run(
                        user_msg=user_msg,
                        agent_response=assistant_msg,
                        tool_calls=[],
                        user_id=_EVAL_USER_ID,
                        chat_id=_EVAL_CHAT_ID,
                        source_ref=current_source_ref,
                    )
                    n_invalidated += len(superseded)
                    after_invalidation = _memory_snapshot(store, user_id=_EVAL_USER_ID)
                    trace.end(
                        invalidation_run,
                        outputs={
                            "superseded_ids": superseded,
                            "newly_superseded": [
                                item
                                for item in after_invalidation.get("items", [])
                                if item.get("status") == "superseded"
                                and item.get("id")
                                not in {
                                    before_item.get("id")
                                    for before_item in before_invalidation.get("items", [])
                                    if before_item.get("status") == "superseded"
                                }
                            ],
                            "memory_snapshot": _compact_memory_snapshot(
                                after_invalidation
                            ),
                        },
                    )
                except Exception as exc:
                    trace.end(invalidation_run, error=str(exc))
                    raise

        before_tail = _memory_snapshot(store, user_id=_EVAL_USER_ID)
        tail_run = trace.start(
            "memory.consolidation.tail",
            parent=parent_run,
            inputs={
                "question_id": str(inputs.get("question_id") or ""),
                "remaining_messages": max(
                    0,
                    len(session.messages) - session.last_consolidated,
                ),
                "last_consolidated": session.last_consolidated,
            },
        )
        try:
            tail_written = await _finalize_tail(session, store)
            n_consolidated += tail_written
            after_tail = _memory_snapshot(store, user_id=_EVAL_USER_ID)
            trace.end(
                tail_run,
                outputs={
                    "written_count": tail_written,
                    "last_consolidated": session.last_consolidated,
                    "new_memories": _memory_delta(before_tail, after_tail),
                    "memory_snapshot": _compact_memory_snapshot(after_tail),
                },
            )
        except Exception as exc:
            trace.end(tail_run, error=str(exc))
            raise
        return {
            "selection_mode": "langsmith_haystack_sessions",
            "replayed_sessions": len(sessions),
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


async def _run_one_async(
    inputs: dict[str, Any],
    *,
    timeout_s: float,
    prompt_visibility: str,
    qa_chat_id: int,
) -> dict[str, Any]:
    trace = _EvalTrace(prompt_visibility=prompt_visibility)
    clear_run = trace.start(
        "eval.clear_state",
        inputs={"question_id": str(inputs.get("question_id") or "")},
    )
    try:
        _clear_eval_state()
        trace.end(
            clear_run,
            outputs={
                "cleared_tables": [
                    "vec_items",
                    "memory_replacements",
                    "memory_items",
                    "conversation_sessions",
                ],
                "cleared_markdown_user_root": str(
                    default_markdown_memory_root() / "users" / str(_EVAL_USER_ID)
                ),
            },
        )
    except Exception as exc:
        trace.end(clear_run, error=str(exc))
        raise

    embedder = Embedder()
    store = MemoryStore(embedder)
    markdown_store = MarkdownMemoryStore(default_markdown_memory_root())
    pipeline = None

    try:
        replay_run = trace.start(
            "haystack.replay",
            inputs={
                "question_id": str(inputs.get("question_id") or ""),
                "haystack_sessions": len(inputs.get("haystack_sessions") or []),
                "haystack_message_count": sum(
                    len(session or [])
                    for session in list(inputs.get("haystack_sessions") or [])
                ),
            },
        )
        try:
            ingest = await _replay_haystack_from_inputs(
                inputs=inputs,
                store=store,
                embedder=embedder,
                markdown_store=markdown_store,
                trace=trace,
                parent_run=replay_run,
            )
            memory_after_replay = _memory_snapshot(store, user_id=_EVAL_USER_ID)
            markdown_after_replay = _markdown_snapshot(
                markdown_store,
                user_id=_EVAL_USER_ID,
                visibility=prompt_visibility,
            )
            trace.end(
                replay_run,
                outputs={
                    **ingest,
                    "memory_snapshot": _compact_memory_snapshot(memory_after_replay),
                },
            )
            trace.record(
                "markdown.snapshot.after_consolidation",
                outputs=markdown_after_replay,
            )
        except Exception as exc:
            trace.end(replay_run, error=str(exc))
            raise

        pipeline = await _create_pipeline(store=store, embedder=embedder)

        t0 = time.monotonic()
        error: str | None = None
        answer = ""
        qa_run = trace.start(
            "passive.qa",
            inputs={
                "question_id": str(inputs.get("question_id") or ""),
                "question": str(inputs.get("question") or ""),
                "haystack_chat_id": _EVAL_CHAT_ID,
                "qa_chat_id": qa_chat_id,
                "qa_session_isolated": qa_chat_id != _EVAL_CHAT_ID,
                "timeout_s": timeout_s,
            },
        )
        try:
            outbound = await asyncio.wait_for(
                pipeline.execute(
                    InboundMessage(
                        user_id=_EVAL_USER_ID,
                        chat_id=qa_chat_id,
                        content=str(inputs.get("question") or ""),
                    )
                ),
                timeout=timeout_s,
            )
            answer = outbound.content if outbound else ""
        except asyncio.TimeoutError:
            error = f"timeout after {timeout_s}s"
        except Exception as exc:  # pragma: no cover - uploaded as experiment output
            error = str(exc)
        finally:
            trace.end(
                qa_run,
                outputs={
                    "answer": answer,
                    "elapsed_s": round(time.monotonic() - t0, 2),
                    "error": error,
                    "qa_chat_id": qa_chat_id,
                    "qa_session_isolated": qa_chat_id != _EVAL_CHAT_ID,
                },
                error=error,
            )

        reasoner_result = getattr(pipeline, "last_reasoner_result", None)
        tool_calls = reasoner_result.tool_calls if reasoner_result is not None else []
        retrieval_trace = [
            {
                "id": str(memory.id),
                "type": memory.memory_type,
                "summary": memory.summary[:160],
                "status": memory.status,
                "source_ref": memory.source_ref,
            }
            for memory in pipeline.before_turn.last_retrieved
        ]
        before_turn_trace = {
            "query_text": getattr(pipeline.before_turn, "last_query_text", ""),
            "engine_trace": getattr(pipeline.before_turn, "last_retrieval_trace", {}),
            "retrieved": retrieval_trace,
        }
        trace.record(
            "passive.before_turn.retrieve",
            inputs={
                "question_id": str(inputs.get("question_id") or ""),
                "question": str(inputs.get("question") or ""),
                "query_text": before_turn_trace["query_text"],
            },
            outputs=before_turn_trace,
        )

        prompt_sections = list(
            getattr(pipeline.before_reasoning, "last_prompt_sections", []) or []
        )
        prompt_messages = list(
            getattr(pipeline.before_reasoning, "last_messages", []) or []
        )
        trace.record(
            "passive.before_reasoning.prompt",
            inputs={
                "question_id": str(inputs.get("question_id") or ""),
                "visibility": prompt_visibility,
            },
            outputs={
                "message_count": len(prompt_messages),
                "system_section_count": len(prompt_sections),
                "system_sections": _prompt_sections_payload(
                    prompt_sections,
                    visibility=prompt_visibility,
                ),
            },
        )

        finish_reason = (
            getattr(reasoner_result, "finish_reason", "") if reasoner_result else ""
        )
        tool_payloads = _tool_payloads(tool_calls)
        tool_registry = getattr(pipeline.before_reasoning, "tool_registry", None)
        if tool_registry is not None:
            visible_tool_schemas = tool_registry.get_schemas()
        else:
            visible_tool_schemas = []
        trace.record(
            "passive.reasoner.step",
            inputs={
                "question_id": str(inputs.get("question_id") or ""),
                "visible_tools": [
                    str(tool.get("function", {}).get("name", ""))
                    for tool in visible_tool_schemas
                    if tool.get("function", {}).get("name")
                ],
            },
            outputs={
                "finish_reason": finish_reason,
                "tool_call_count": len(tool_calls),
                "tool_names": _called_tool_names(tool_calls),
                "tool_calls": tool_payloads,
            },
        )
        for tool_payload in tool_payloads:
            tool_name = tool_payload.get("name") or "unknown"
            trace.record(
                f"tool.{tool_name}",
                inputs={
                    "question_id": str(inputs.get("question_id") or ""),
                    "arguments": tool_payload.get("arguments") or {},
                    "guard": tool_payload.get("guard"),
                },
                outputs={
                    "result": tool_payload.get("result") or {},
                    "result_text": tool_payload.get("result_text") or {},
                },
                run_type="tool",
            )

        tool_policy = _tool_policy_for_inputs(inputs, tool_calls)
        final_memory_snapshot = _memory_snapshot(store, user_id=_EVAL_USER_ID)
        metadata = inputs.get("_metadata") or {}
        trace.record(
            "eval.final",
            inputs={
                "question_id": str(inputs.get("question_id") or ""),
                "question": str(inputs.get("question") or ""),
            },
            outputs={
                "answer": answer,
                "error": error,
                "haystack_chat_id": _EVAL_CHAT_ID,
                "qa_chat_id": qa_chat_id,
                "qa_session_isolated": qa_chat_id != _EVAL_CHAT_ID,
                "tool_policy": tool_policy,
                "acceptance_criteria": metadata.get("acceptance_criteria") or [],
                "trace_expectations": metadata.get("trace_expectations") or {},
                "retrieval_trace": retrieval_trace,
                "memory_snapshot": _compact_memory_snapshot(final_memory_snapshot),
            },
            error=error,
        )
        return {
            "answer": answer,
            "question_id": str(inputs.get("question_id") or ""),
            "elapsed_s": round(time.monotonic() - t0, 2),
            "error": error,
            "haystack_chat_id": _EVAL_CHAT_ID,
            "qa_chat_id": qa_chat_id,
            "qa_session_isolated": qa_chat_id != _EVAL_CHAT_ID,
            "ingest": ingest,
            "retrieval_trace": retrieval_trace,
            "tool_calls": tool_calls,
            "tool_policy": tool_policy,
            "trace_summary": {
                "before_turn": before_turn_trace,
                "prompt_sections": _prompt_sections_payload(
                    prompt_sections,
                    visibility="hash",
                ),
                "memory_snapshot": _compact_memory_snapshot(final_memory_snapshot),
            },
        }
    finally:
        if pipeline is not None:
            await _close_pipeline(pipeline)
        await _close_async_resource(embedder)


def build_target(timeout_s: float, *, prompt_visibility: str, qa_chat_id: int):
    async def target(inputs: dict[str, Any]) -> dict[str, Any]:
        return await _run_one_async(
            inputs,
            timeout_s=timeout_s,
            prompt_visibility=prompt_visibility,
            qa_chat_id=qa_chat_id,
        )

    return target


def rule_answer_correctness(
    outputs: dict[str, Any],
    reference_outputs: dict[str, Any],
    inputs: dict[str, Any],
) -> bool:
    result = evaluate_single(
        predicted_answer=str(outputs.get("answer") or ""),
        gold_answer=str(reference_outputs.get("answer") or ""),
        question=str(inputs.get("question") or ""),
    )
    return bool(result.get("rule_judge_correct"))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run LangSmith replay memory eval")
    parser.add_argument("--dataset", default=_DEFAULT_DATASET)
    parser.add_argument("--experiment-prefix", default="telegram-bot-mvp-memory")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--question-id",
        action="append",
        default=[],
        help="Run only examples whose inputs.question_id matches. Repeat for multiple ids.",
    )
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--timeout", type=float, default=_DEFAULT_TIMEOUT_S)
    parser.add_argument(
        "--prompt-visibility",
        choices=["preview", "hash", "full"],
        default="preview",
        help="How much prompt/markdown text to upload into LangSmith child runs.",
    )
    parser.add_argument(
        "--no-local-evaluator",
        action="store_true",
        help="Upload runs only; use LangSmith UI evaluators instead of local rule judge.",
    )
    parser.add_argument(
        "--same-session-qa",
        action="store_true",
        help=(
            "Ask QA in the same chat as haystack replay. Default is isolated QA "
            "chat_id=9001 to evaluate long-memory retrieval without recent-context leakage."
        ),
    )
    return parser


def _validate_env(args: argparse.Namespace) -> None:
    if not args.fresh:
        raise SystemExit("LangSmith replay eval requires --fresh.")
    missing = [
        name
        for name in (
            "LANGSMITH_API_KEY",
            "DEEPSEEK_API_KEY",
            "ALIYUN_DASHSCOPE_API_KEY",
        )
        if not os.environ.get(name)
    ]
    if missing:
        raise SystemExit(f"Missing required env vars: {', '.join(missing)}")


async def _amain() -> None:
    args = _build_parser().parse_args()
    _validate_env(args)
    init_db()

    client = Client()
    client.read_dataset(dataset_name=args.dataset)
    data: Any = args.dataset
    if args.question_id:
        wanted = {str(value) for value in args.question_id}
        examples = [
            example
            for example in client.list_examples(dataset_name=args.dataset)
            if str((getattr(example, "inputs", None) or {}).get("question_id") or "")
            in wanted
        ]
        found = {
            str((getattr(example, "inputs", None) or {}).get("question_id") or "")
            for example in examples
        }
        missing = sorted(wanted - found)
        if missing:
            raise SystemExit(f"Dataset examples not found for question_id: {missing}")
        data = examples[: args.limit] if args.limit > 0 else examples
    elif args.limit > 0:
        data = list(client.list_examples(dataset_name=args.dataset, limit=args.limit))

    evaluators = [] if args.no_local_evaluator else [rule_answer_correctness]
    qa_chat_id = _EVAL_CHAT_ID if args.same_session_qa else _EVAL_QA_CHAT_ID
    results = await aevaluate(
        build_target(
            args.timeout,
            prompt_visibility=args.prompt_visibility,
            qa_chat_id=qa_chat_id,
        ),
        data=data,
        evaluators=evaluators,
        experiment_prefix=args.experiment_prefix,
        description=(
            "Replay eval: haystack_sessions -> consolidation -> invalidation -> "
            "PassiveTurnPipeline QA"
        ),
        max_concurrency=1,
        client=client,
    )
    print(results)


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
