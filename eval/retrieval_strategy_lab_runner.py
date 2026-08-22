"""
Seeded retrieval strategy lab.

This runner is intentionally not an eval of the real passive reply chain. It
does not replay haystacks, consolidate, invalidate, or call the reasoner. It
seeds small teaching memories into MemoryStore, runs retrieval strategies, and
prints tables that make rank changes easy to inspect.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES = ROOT / "data" / "evaluation" / "retrieval_strategy_lab.jsonl"
DEFAULT_LAB_DB = ROOT / "data" / "evaluation" / "retrieval_strategy_lab.db"
DEFAULT_TOP_K = 5
DEFAULT_RRF_K = 60
LAB_USER_ID = 900_2024
LAB_CHAT_ID_BASE = 910_000


def _preparse_database_path(argv: list[str]) -> str | None:
    for index, arg in enumerate(argv):
        if arg == "--database-path" and index + 1 < len(argv):
            return argv[index + 1]
        if arg.startswith("--database-path="):
            return arg.split("=", 1)[1]
    return None


def _dotenv_has_key(key: str) -> bool:
    dotenv = ROOT / ".env"
    if not dotenv.exists():
        return False
    for line in dotenv.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.split("=", 1)[0].strip() == key:
            return True
    return False


def _ensure_settings_importable() -> None:
    for key in ("TG_BOT_TOKEN", "DEEPSEEK_API_KEY", "ALIYUN_DASHSCOPE_API_KEY"):
        if key not in os.environ and not _dotenv_has_key(key):
            os.environ[key] = "retrieval-lab-unused"


os.environ["DATABASE_PATH"] = _preparse_database_path(sys.argv) or str(DEFAULT_LAB_DB)
_ensure_settings_importable()
sys.path.insert(0, str(ROOT))

from agent.core.types import MemoryItem  # noqa: E402
from memory.embedder import Embedder  # noqa: E402
from memory.store import LONG_TERM_MEMORY_TYPES, MemoryStore  # noqa: E402
from persistence.database import get_connection, init_db  # noqa: E402


@dataclass(frozen=True)
class QuerySpec:
    query: str
    vector_tags: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class SeedMemorySpec:
    label: str
    summary: str
    memory_type: str = "fact"
    vector_tags: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class LabCase:
    case_id: str
    lesson: str
    query: str
    target_labels: list[str]
    query_vector_tags: dict[str, float] = field(default_factory=dict)
    aux_queries: list[QuerySpec] = field(default_factory=list)
    memories: list[SeedMemorySpec] = field(default_factory=list)
    strategy_notes: dict[str, str] = field(default_factory=dict)


@dataclass
class RankedHit:
    memory: MemoryItem
    label: str
    lanes: list[str]
    score: float = 0.0


@dataclass
class StrategyResult:
    name: str
    hits: list[RankedHit]
    note: str


class LabEmbedder:
    """Small deterministic embedder for teaching retrieval mechanics."""

    dimensions = 1024

    def __init__(self) -> None:
        self._vectors_by_text: dict[str, list[float]] = {}
        self._feature_indexes: dict[str, int] = {}

    def register(self, text: str, tags: dict[str, float] | None) -> None:
        clean = _text_key(text)
        self._vectors_by_text[clean] = self._vector_from_tags(tags or {})

    async def embed(self, text: str) -> list[float]:
        clean = _text_key(text)
        if clean in self._vectors_by_text:
            return list(self._vectors_by_text[clean])
        return self._vector_from_tags(_fallback_tags(text))

    async def close(self) -> None:
        return None

    def _vector_from_tags(self, tags: dict[str, float]) -> list[float]:
        vector = [0.0] * self.dimensions
        for tag, weight in sorted((tags or {}).items()):
            index = self._index_for_feature(str(tag))
            vector[index] = float(weight)
        return vector

    def _index_for_feature(self, feature: str) -> int:
        if feature not in self._feature_indexes:
            self._feature_indexes[feature] = len(self._feature_indexes)
        index = self._feature_indexes[feature]
        if index >= self.dimensions:
            raise ValueError("too many lab embedding features")
        return index


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run seeded retrieval strategy lab")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--display-top", type=int, default=5)
    parser.add_argument("--rrf-k", type=int, default=DEFAULT_RRF_K)
    parser.add_argument(
        "--strategies",
        default="vector_single,vector_dual_manual,keyword_raw_like,keyword_tokenized,hybrid_rrf_k60",
        help="Comma-separated strategy names.",
    )
    parser.add_argument(
        "--embedder",
        choices=["lab", "live"],
        default="lab",
        help="lab is deterministic and offline; live uses the configured DashScope embedder.",
    )
    parser.add_argument(
        "--database-path",
        type=Path,
        default=Path(os.environ["DATABASE_PATH"]),
        help="SQLite path. Defaults to an isolated lab DB, not data/memory.db.",
    )
    parser.add_argument("--list", action="store_true", help="List case ids and exit.")
    return parser


def _load_cases(path: Path) -> list[LabCase]:
    cases: list[LabCase] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            raw = line.strip()
            if not raw:
                continue
            payload = json.loads(raw)
            cases.append(_case_from_payload(payload, source=f"{path}:{line_number}"))
    return cases


def _case_from_payload(payload: dict[str, Any], *, source: str) -> LabCase:
    memories = [
        SeedMemorySpec(
            label=str(item["label"]),
            summary=str(item["summary"]),
            memory_type=str(item.get("memory_type") or "fact"),
            vector_tags=_float_dict(item.get("vector_tags") or {}),
        )
        for item in payload.get("memories") or []
    ]
    aux_queries = []
    for item in payload.get("aux_queries") or []:
        if isinstance(item, str):
            aux_queries.append(QuerySpec(query=item))
        else:
            aux_queries.append(
                QuerySpec(
                    query=str(item.get("query") or ""),
                    vector_tags=_float_dict(item.get("vector_tags") or {}),
                )
            )
    case = LabCase(
        case_id=str(payload["case_id"]),
        lesson=str(payload.get("lesson") or ""),
        query=str(payload["query"]),
        target_labels=[str(label) for label in payload.get("target_labels") or []],
        query_vector_tags=_float_dict(payload.get("query_vector_tags") or {}),
        aux_queries=aux_queries,
        memories=memories,
        strategy_notes={
            str(key): str(value)
            for key, value in (payload.get("strategy_notes") or {}).items()
        },
    )
    if not case.memories:
        raise ValueError(f"{source} has no memories")
    if not case.target_labels:
        raise ValueError(f"{source} has no target_labels")
    return case


def _float_dict(value: dict[str, Any]) -> dict[str, float]:
    return {str(key): float(raw) for key, raw in value.items()}


async def _run_case(
    case: LabCase,
    *,
    store: MemoryStore,
    embedder: Any,
    strategies: list[str],
    top_k: int,
    rrf_k: int,
) -> list[StrategyResult]:
    _register_case_vectors(case, embedder)
    label_by_id = await _seed_case(store, case)

    results: list[StrategyResult] = []
    for strategy in strategies:
        hits = await _run_strategy(
            strategy=strategy,
            case=case,
            store=store,
            embedder=embedder,
            label_by_id=label_by_id,
            top_k=top_k,
            rrf_k=rrf_k,
        )
        results.append(
            StrategyResult(
                name=strategy,
                hits=hits,
                note=_strategy_note(case, strategy, hits),
            )
        )
    return results


def _register_case_vectors(case: LabCase, embedder: Any) -> None:
    register = getattr(embedder, "register", None)
    if register is None:
        return
    register(case.query, case.query_vector_tags)
    for aux in case.aux_queries:
        register(aux.query, aux.vector_tags)
    for memory in case.memories:
        register(memory.summary, memory.vector_tags)


async def _seed_case(store: MemoryStore, case: LabCase) -> dict[str, str]:
    _clear_lab_state()
    label_by_id: dict[str, str] = {}
    for index, memory in enumerate(case.memories):
        item = await store.upsert_item(
            memory_type=memory.memory_type,
            summary=memory.summary,
            user_id=LAB_USER_ID,
            source_ref=f"session:{LAB_USER_ID}:{LAB_CHAT_ID_BASE + index}",
        )
        label_by_id[str(item.id)] = memory.label
    return label_by_id


def _clear_lab_state() -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM memory_replacements")
    cursor.execute("DELETE FROM vec_items")
    cursor.execute("DELETE FROM memory_items")
    cursor.execute("DELETE FROM conversation_sessions")
    conn.commit()


async def _run_strategy(
    *,
    strategy: str,
    case: LabCase,
    store: MemoryStore,
    embedder: Any,
    label_by_id: dict[str, str],
    top_k: int,
    rrf_k: int,
) -> list[RankedHit]:
    if strategy == "vector_single":
        memories = await _vector_lane(store, embedder, case.query, top_k=top_k)
        return _ranked_hits(memories, label_by_id, lane="vector:q0")

    if strategy == "vector_dual_manual":
        lanes = [("vector:q0", await _vector_lane(store, embedder, case.query, top_k=top_k))]
        for index, aux in enumerate(case.aux_queries, 1):
            lanes.append(
                (
                    f"vector:aux{index}",
                    await _vector_lane(store, embedder, aux.query, top_k=top_k),
                )
            )
        return _rrf_fuse_lanes(lanes, label_by_id, rrf_k=rrf_k)[:top_k]

    if strategy == "keyword_raw_like":
        memories = await store.keyword_search(
            terms=case.query,
            user_id=LAB_USER_ID,
            limit=top_k,
            memory_types=LONG_TERM_MEMORY_TYPES,
        )
        return _ranked_hits(memories, label_by_id, lane="keyword:raw_like")

    if strategy == "keyword_tokenized":
        return _tokenized_keyword_search(store, case.query, label_by_id, top_k=top_k)

    if strategy == "hybrid_rrf_k60":
        vector_memories = await _vector_lane(store, embedder, case.query, top_k=top_k)
        keyword_hits = _tokenized_keyword_search(store, case.query, label_by_id, top_k=top_k)
        keyword_memories = [hit.memory for hit in keyword_hits]
        return _rrf_fuse_lanes(
            [
                ("vector:q0", vector_memories),
                ("keyword:tokenized", keyword_memories),
            ],
            label_by_id,
            rrf_k=rrf_k,
        )[:top_k]

    raise SystemExit(f"unknown strategy: {strategy}")


async def _vector_lane(
    store: MemoryStore,
    embedder: Any,
    query: str,
    *,
    top_k: int,
) -> list[MemoryItem]:
    query_vec = await embedder.embed(query)
    return await store.vector_search(
        query_vec=query_vec,
        user_id=LAB_USER_ID,
        top_k=top_k,
        memory_types=LONG_TERM_MEMORY_TYPES,
    )


def _ranked_hits(
    memories: list[MemoryItem],
    label_by_id: dict[str, str],
    *,
    lane: str,
) -> list[RankedHit]:
    return [
        RankedHit(memory=memory, label=label_by_id.get(str(memory.id), "?"), lanes=[lane])
        for memory in memories
    ]


def _rrf_fuse_lanes(
    lanes: list[tuple[str, list[MemoryItem]]],
    label_by_id: dict[str, str],
    *,
    rrf_k: int,
) -> list[RankedHit]:
    fused: dict[str, RankedHit] = {}
    first_seen: dict[str, int] = {}
    sequence = 0
    for lane_name, memories in lanes:
        for rank, memory in enumerate(memories, 1):
            memory_id = str(memory.id)
            if memory_id not in fused:
                fused[memory_id] = RankedHit(
                    memory=memory,
                    label=label_by_id.get(memory_id, "?"),
                    lanes=[],
                )
                first_seen[memory_id] = sequence
                sequence += 1
            fused[memory_id].score += 1.0 / (max(1, rrf_k) + rank)
            if lane_name not in fused[memory_id].lanes:
                fused[memory_id].lanes.append(lane_name)
    return sorted(
        fused.values(),
        key=lambda hit: (-hit.score, first_seen.get(str(hit.memory.id), 0)),
    )


def _tokenized_keyword_search(
    store: MemoryStore,
    query: str,
    label_by_id: dict[str, str],
    *,
    top_k: int,
) -> list[RankedHit]:
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    memories = store.list_memories(
        user_id=LAB_USER_ID,
        memory_types=LONG_TERM_MEMORY_TYPES,
        limit=200,
    )
    scored: list[tuple[float, MemoryItem]] = []
    for memory in memories:
        score = _keyword_score(query, query_tokens, memory.summary)
        if score > 0:
            scored.append((score, memory))
    scored.sort(key=lambda item: (-item[0], str(item[1].created_at), str(item[1].id)))
    return [
        RankedHit(
            memory=memory,
            label=label_by_id.get(str(memory.id), "?"),
            lanes=["keyword:tokenized"],
            score=score,
        )
        for score, memory in scored[:top_k]
    ]


def _keyword_score(query: str, query_tokens: list[str], summary: str) -> float:
    summary_key = _text_key(summary)
    summary_tokens = set(_tokenize(summary))
    score = 0.0
    for token in query_tokens:
        if token in summary_tokens:
            score += 1.0 + min(len(token), 20) / 20.0
    query_key = _text_key(query)
    if query_key and query_key in summary_key:
        score += 8.0
    for token in query_tokens:
        if len(token) >= 6 and token in summary_key:
            score += 2.0
    return score


_ASCII_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.-]*", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")


def _tokenize(text: str) -> list[str]:
    clean = _text_key(text)
    tokens: list[str] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        token = token.strip("_-. ")
        if len(token) < 2 or token in seen:
            return
        seen.add(token)
        tokens.append(token)

    for match in _ASCII_TOKEN_RE.finditer(clean):
        raw = match.group(0)
        add(raw)
        for part in re.split(r"[_\-.]+", raw):
            add(part)
    for chunk in _CJK_RE.findall(clean):
        if len(chunk) <= 4:
            add(chunk)
            continue
        for size in (2, 3, 4):
            for index in range(0, len(chunk) - size + 1):
                add(chunk[index : index + size])
    return tokens


def _fallback_tags(text: str) -> dict[str, float]:
    clean = _text_key(text)
    tags: dict[str, float] = {}
    rules = {
        "project_config": ("config", "配置", "project", "项目"),
        "migration_window": ("migration", "迁移", "window", "窗口"),
        "unfinished_work": ("未完成", "没做完", "unfinished", "todo"),
        "backlog_place": ("backlog", "待办", "搁", "收进"),
        "log_observe": ("日志", "trace", "log", "观察"),
        "short_command": ("命令", "command", "短命令", "smoke"),
        "rag_layer": ("rag", "检索层", "retrieval"),
        "retrieval_smoke": ("smoke", "sanity"),
    }
    for tag, needles in rules.items():
        if any(needle in clean for needle in needles):
            tags[tag] = 1.0
    return tags


def _strategy_note(case: LabCase, strategy: str, hits: list[RankedHit]) -> str:
    base = case.strategy_notes.get(strategy, "")
    if not hits:
        return _join_note(base, "no hits")
    top = hits[0].label
    if top not in set(case.target_labels):
        return _join_note(base, f"top decoy={top}")
    return _join_note(base, "target is top1")


def _join_note(left: str, right: str) -> str:
    left = left.strip()
    right = right.strip()
    if left and right:
        return f"{left}; {right}"
    return left or right


def _target_rank(hits: list[RankedHit], target_labels: list[str]) -> str:
    targets = set(target_labels)
    for rank, hit in enumerate(hits, 1):
        if hit.label in targets:
            return str(rank)
    return "miss"


def _hit_at_1(hits: list[RankedHit], target_labels: list[str]) -> str:
    return "yes" if hits and hits[0].label in set(target_labels) else "no"


def _print_case(case: LabCase, results: list[StrategyResult], *, display_top: int) -> None:
    print("\n" + "=" * 96)
    print(f"case: {case.case_id}")
    print(f"lesson: {case.lesson}")
    print(f"query: {case.query}")
    if case.aux_queries:
        for index, aux in enumerate(case.aux_queries, 1):
            print(f"aux_query_{index}: {aux.query}")
    print(f"target_labels: {', '.join(case.target_labels)}")
    print()

    rows = []
    for result in results:
        top1 = result.hits[0].label if result.hits else "-"
        rows.append(
            [
                result.name,
                _target_rank(result.hits, case.target_labels),
                _hit_at_1(result.hits, case.target_labels),
                top1,
                result.note,
            ]
        )
    print(_format_table(["strategy", "target_rank", "hit@1", "top1", "note"], rows))

    for result in results:
        print(f"\n{result.name} top{display_top}:")
        top_rows = [
            [
                str(rank),
                hit.label,
                "+".join(hit.lanes),
                hit.memory.summary,
            ]
            for rank, hit in enumerate(result.hits[:display_top], 1)
        ]
        if top_rows:
            print(_format_table(["rank", "label", "lane", "summary"], top_rows))
        else:
            print("(no hits)")


def _format_table(headers: list[str], rows: list[list[str]]) -> str:
    all_rows = [headers, *rows]
    widths = [
        min(
            max(len(_clip(str(row[index]), 80)) for row in all_rows),
            80,
        )
        for index in range(len(headers))
    ]

    def fmt(row: list[str]) -> str:
        cells = [
            _clip(str(value), widths[index]).ljust(widths[index])
            for index, value in enumerate(row)
        ]
        return "| " + " | ".join(cells) + " |"

    separator = "| " + " | ".join("-" * width for width in widths) + " |"
    return "\n".join([fmt(headers), separator, *[fmt(row) for row in rows]])


def _clip(value: str, width: int) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= width:
        return value
    return value[: max(0, width - 1)] + "..."


def _text_key(text: str) -> str:
    return str(text or "").lower().strip()


async def _main() -> None:
    args = _build_parser().parse_args()
    if args.top_k <= 0:
        raise SystemExit("--top-k must be positive")
    if args.display_top <= 0:
        raise SystemExit("--display-top must be positive")
    if args.rrf_k <= 0:
        raise SystemExit("--rrf-k must be positive")

    cases = _load_cases(args.cases)
    if args.list:
        for case in cases:
            print(case.case_id)
        return

    wanted = set(args.case_id)
    if wanted:
        cases = [case for case in cases if case.case_id in wanted]
    if not cases:
        raise SystemExit("no matching cases")

    strategies = [item.strip() for item in args.strategies.split(",") if item.strip()]
    args.database_path.parent.mkdir(parents=True, exist_ok=True)
    init_db()

    embedder: Any = LabEmbedder() if args.embedder == "lab" else Embedder()
    store = MemoryStore(embedder)

    print("Mode: seeded MemoryStore retrieval lab")
    print("Skipped: replay, consolidation, invalidation, reasoner")
    print(f"DB: {os.environ['DATABASE_PATH']}")
    print(f"Embedder: {args.embedder}")
    print(f"Strategies: {', '.join(strategies)}")

    try:
        for case in cases:
            results = await _run_case(
                case,
                store=store,
                embedder=embedder,
                strategies=strategies,
                top_k=args.top_k,
                rrf_k=args.rrf_k,
            )
            _print_case(case, results, display_top=min(args.display_top, args.top_k))
    finally:
        close = getattr(embedder, "close", None)
        if close is not None:
            await close()


if __name__ == "__main__":
    asyncio.run(_main())
