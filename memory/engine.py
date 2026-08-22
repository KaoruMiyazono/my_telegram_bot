from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from agent.core.types import MemoryItem
from memory.embedder import Embedder
from memory.store import LONG_TERM_MEMORY_TYPES, MemoryStore
from persistence.session_store import SessionStore

_RRF_K = 60


@dataclass(frozen=True)
class MemoryScope:
    user_id: int | None = None
    chat_id: int | None = None
    session_key: str = ""
    channel: str = "telegram"


@dataclass(frozen=True)
class MemoryRetrieveRequest:
    query: str
    scope: MemoryScope
    top_k: int = 8
    memory_types: list[str] | None = None
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryRetrieveResult:
    items: list[MemoryItem] = field(default_factory=list)
    text_block: str = ""
    trace: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExplicitRetrievalRequest:
    query: str
    scope: MemoryScope
    memory_type: str = ""
    include_superseded: bool = False
    search_mode: str = "semantic"
    time_filter: str = ""
    limit: int = 5


@dataclass
class ExplicitRetrievalResult:
    items: list[dict[str, Any]] = field(default_factory=list)
    applied_memory_types: list[str] = field(default_factory=list)
    error: str = ""
    trace: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RememberRequest:
    summary: str
    memory_type: str
    scope: MemoryScope
    source_ref: str = ""


@dataclass(frozen=True)
class RememberResult:
    status: str
    item_id: str = ""
    summary: str = ""
    memory_type: str = ""
    error: str = ""


@dataclass(frozen=True)
class ForgetRequest:
    ids: list[str]
    scope: MemoryScope = field(default_factory=MemoryScope)


@dataclass
class ForgetResult:
    superseded_ids: list[str] = field(default_factory=list)
    missing_ids: list[str] = field(default_factory=list)
    error: str = ""


@dataclass(frozen=True)
class FetchMessagesRequest:
    source_refs: list[str]
    context: int = 0
    limit: int = 20


@dataclass
class FetchMessagesResult:
    matched_count: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    invalid_source_refs: list[str] = field(default_factory=list)
    error: str = ""


@dataclass(frozen=True)
class SearchMessagesRequest:
    query: str
    scope: MemoryScope
    role: str | None = None
    limit: int = 10
    offset: int = 0


@dataclass
class SearchMessagesResult:
    messages: list[dict[str, Any]] = field(default_factory=list)
    matched_count: int = 0
    limit: int = 10
    offset: int = 0
    has_more: bool = False
    next_offset: int | None = None
    error: str = ""


@dataclass(frozen=True)
class InterestRetrievalRequest:
    query: str
    scope: MemoryScope
    top_k: int = 2


@dataclass
class InterestRetrievalResult:
    text_block: str = ""
    hits: list[dict[str, Any]] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)


@dataclass
class _MemorySearchRequest:
    query: str
    user_id: int
    top_k: int
    memory_types: list[str] | None = None
    include_superseded: bool = False
    aux_queries: list[str] = field(default_factory=list)
    keyword_limit: int | None = None


@dataclass
class _MemorySearchResult:
    items: list[MemoryItem] = field(default_factory=list)
    vector_items: list[MemoryItem] = field(default_factory=list)
    keyword_items: list[MemoryItem] = field(default_factory=list)
    rrf_scores: dict[str, float] = field(default_factory=dict)
    lanes_by_id: dict[str, list[str]] = field(default_factory=dict)
    trace: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class MemoryEngine(Protocol):
    async def retrieve(self, request: MemoryRetrieveRequest) -> MemoryRetrieveResult: ...

    async def retrieve_explicit(
        self, request: ExplicitRetrievalRequest
    ) -> ExplicitRetrievalResult: ...

    async def retrieve_interest_block(
        self, request: InterestRetrievalRequest
    ) -> InterestRetrievalResult: ...

    async def remember(self, request: RememberRequest) -> RememberResult: ...

    async def forget(self, request: ForgetRequest) -> ForgetResult: ...

    async def fetch_messages(self, request: FetchMessagesRequest) -> FetchMessagesResult: ...

    async def search_messages(self, request: SearchMessagesRequest) -> SearchMessagesResult: ...


class DefaultMemoryEngine:
    """Facade over the current vector store and raw session store."""

    def __init__(
        self,
        *,
        store: MemoryStore,
        embedder: Embedder,
        session_store: SessionStore,
        aux_query_builder: Callable[[str], Awaitable[list[str]]] | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.session_store = session_store
        self._aux_query_builder = aux_query_builder

    async def retrieve(self, request: MemoryRetrieveRequest) -> MemoryRetrieveResult:
        query = request.query.strip()
        user_id = request.scope.user_id
        if not query or user_id is None:
            return MemoryRetrieveResult()

        #  得到辅助检索的结果，辅助检索就是用大模型生成了可能的回答，增加检索效率 返回的是一个列表，里面是大模型生成的假想记忆条目
        aux_queries = await self._build_aux_queries(query)
        search = await self._search_memories(
            _MemorySearchRequest(
                query=query,
                user_id=user_id,
                top_k=request.top_k,
                memory_types=request.memory_types or LONG_TERM_MEMORY_TYPES,
                aux_queries=aux_queries,
            )
        )
        #  每一行都是一个记忆条目，格式化成字符串 包括记忆本身和记忆的来源
        lines = [_format_memory_line(m) for m in search.items[: request.top_k]]
        aux_new_counts = search.trace.get("vector_lane_new_counts") or []
        hyde_added = any(int(count) > 0 for count in list(aux_new_counts)[1:])
        trace = {
            **search.trace,
            "hyde_used": hyde_added,
            "hypothesis": aux_queries[0] if aux_queries else "",
            "aux_queries": aux_queries,
        }
        return MemoryRetrieveResult(
            #  返回的items是MemoryItem对象列表，text_block是格式化后的字符串，trace是检索过程中的一些信息，也就是aux是不是有用
            items=search.items,
            text_block="\n".join(lines),
            trace=trace,
        )

    #  grep事找对应时间内的记忆，semantic是找内容相关的
    async def retrieve_explicit(
        self, request: ExplicitRetrievalRequest
    ) -> ExplicitRetrievalResult:
        query = request.query.strip()
        user_id = request.scope.user_id
        if not query or user_id is None:
            return ExplicitRetrievalResult()

        memory_type = request.memory_type.strip() or None
        search_mode = request.search_mode.strip() or "semantic"
        time_filter = request.time_filter.strip()
        time_window = _parse_time_filter(time_filter)
        if time_filter and time_window is None:
            return ExplicitRetrievalResult(error="invalid_time_filter")
        if search_mode not in {"semantic", "grep"}:
            search_mode = "semantic"

        memory_types = _infer_memory_types(
            query=query,
            explicit_memory_type=memory_type,
            search_mode=search_mode,
            time_filter=time_filter,
        )
        max_limit = 50 if search_mode == "grep" else 10
        limit = max(1, min(int(request.limit), max_limit))

        if search_mode == "grep":
            if time_window is None:
                return ExplicitRetrievalResult(error="time_filter_required")
            start, end = time_window
            grep_results = self.store.list_memories(
                user_id=user_id,
                memory_types=[memory_type] if memory_type else ["event"],
                include_superseded=request.include_superseded,
                created_start=start,
                created_end=end,
                limit=limit,
            )
            items = [
                _memory_item_payload(mem, score=1.0)
                for mem in grep_results
            ]
            if request.include_superseded:
                items = _prefer_active_items(items)
            return ExplicitRetrievalResult(
                items=items,
                applied_memory_types=[memory_type] if memory_type else ["event"],
            )

        aux_queries = await self._build_aux_queries(query)
        search = await self._search_memories(
            _MemorySearchRequest(
                query=query,
                user_id=user_id,
                top_k=limit,
                memory_types=memory_types,
                include_superseded=request.include_superseded,
                aux_queries=aux_queries,
                keyword_limit=limit,
            )
        )

        items: list[dict[str, Any]] = []
        for mem in search.items:
            mid = str(mem.id)
            lanes = search.lanes_by_id.get(mid, [])
            payload = _memory_item_payload(
                mem,
                score=1.0 if "vector" in lanes else 0.5,
            )
            payload["rrf_score"] = round(search.rrf_scores.get(mid, 0.0), 6)
            payload["lanes"] = lanes
            items.append(payload)

        if time_window is not None:
            start, end = time_window
            items = [
                item
                for item in items
                if _memory_created_in_window(
                    item["id"], [*search.vector_items, *search.keyword_items], start, end
                )
            ]
        if request.include_superseded:
            items = _prefer_active_items(items)
        aux_new_counts = search.trace.get("vector_lane_new_counts") or []
        hyde_added = any(int(count) > 0 for count in list(aux_new_counts)[1:])
        return ExplicitRetrievalResult(
            items=items[:limit],
            applied_memory_types=memory_types,
            trace={
                **search.trace,
                "hyde_used": hyde_added,
                "hypothesis": aux_queries[0] if aux_queries else "",
            },
        )

    #  也是召回，但是是兴趣召回，主要是召回用户的偏好，兴趣，习惯等（这些都是记忆类型）
    async def retrieve_interest_block(
        self, request: InterestRetrievalRequest
    ) -> InterestRetrievalResult:
        result = await self.retrieve(
            MemoryRetrieveRequest(
                query=request.query,
                scope=request.scope,
                top_k=request.top_k,
                memory_types=["preference", "profile", "procedure"],
            )
        )
        hits = [
            {
                "id": str(item.id),
                "text": item.summary,
                "memory_type": item.memory_type,
                "source_ref": item.source_ref,
            }
            for item in result.items[: request.top_k]
        ]
        return InterestRetrievalResult(
            text_block="\n".join(hit["text"] for hit in hits),
            hits=hits,
            trace=result.trace,
        )

    async def remember(self, request: RememberRequest) -> RememberResult:
        summary = request.summary.strip()
        memory_type = request.memory_type.strip()
        user_id = request.scope.user_id
        if not summary or not memory_type:
            return RememberResult(
                status="failed",
                error="summary and memory_type required",
            )
        if user_id is None:
            return RememberResult(status="failed", error="user_id_required")
        if memory_type not in ("procedure", "preference", "event", "profile", "fact"):
            return RememberResult(
                status="failed",
                error=f"invalid memory_type: {memory_type}",
            )
        try:
            item = await self.store.upsert_item(
                memory_type=memory_type,
                summary=summary,
                user_id=user_id,
                source_ref=request.source_ref or _session_source_ref(request.scope),
            )
        except Exception as exc:
            return RememberResult(status="failed", error=str(exc))
        return RememberResult(
            status="saved",
            item_id=str(item.id),
            summary=summary,
            memory_type=memory_type,
        )

    async def forget(self, request: ForgetRequest) -> ForgetResult:
        try:
            updated = self.store.mark_superseded_batch(
                request.ids,
                user_id=request.scope.user_id,
            )
        except Exception as exc:
            return ForgetResult(error=str(exc))
        missing = [item_id for item_id in request.ids if item_id not in set(updated)]
        return ForgetResult(superseded_ids=updated, missing_ids=missing)

    async def fetch_messages(self, request: FetchMessagesRequest) -> FetchMessagesResult:
        refs = _dedupe_refs(request.source_refs)
        if not refs:
            return FetchMessagesResult(error="source_ref_required")

        limit = max(1, min(int(request.limit), 50))
        context = max(0, min(int(request.context), 10))
        all_fetched: list[dict[str, Any]] = []
        total_matched = 0
        invalid_refs: list[str] = []
        seen: set[tuple[str, int | None]] = set()

        for source_ref in refs:
            user_id, chat_id, seq, seq_end = _parse_session_ref(source_ref)
            if user_id is None or chat_id is None:
                invalid_refs.append(source_ref)
                continue
            fetched, matched = self.session_store.fetch_messages(
                user_id,
                chat_id,
                seq=seq,
                seq_end=seq_end,
                context=context,
                limit=limit,
            )
            total_matched += matched
            for message in fetched:
                key = (str(message.get("source_ref", "")), message.get("seq"))
                if key in seen:
                    continue
                seen.add(key)
                all_fetched.append(message)

        if len(all_fetched) > limit:
            all_fetched = all_fetched[:limit]
        return FetchMessagesResult(
            matched_count=total_matched if all_fetched else 0,
            source_refs=refs,
            messages=[
                {
                    "role": m.get("role", "?"),
                    "content": str(m.get("content", ""))[:500],
                    "seq": m.get("seq"),
                    "source_ref": m.get("source_ref", ""),
                    "in_source_ref": bool(m.get("in_source_ref")),
                }
                for m in all_fetched
            ],
            invalid_source_refs=invalid_refs,
            error=(
                f"invalid_source_ref: {', '.join(invalid_refs)}"
                if invalid_refs and not all_fetched
                else ""
            ),
        )

    async def search_messages(self, request: SearchMessagesRequest) -> SearchMessagesResult:
        query = request.query.strip()
        user_id = request.scope.user_id
        if not query or user_id is None:
            return SearchMessagesResult(limit=request.limit, offset=request.offset)

        limit = max(1, min(int(request.limit), 50))
        offset = max(0, int(request.offset))
        try:
            messages, total = self.session_store.search_messages(
                query,
                user_id=user_id,
                role=request.role,
                limit=limit,
                offset=offset,
            )
        except Exception as exc:
            return SearchMessagesResult(error=str(exc), limit=limit, offset=offset)
        public_messages = [
            {
                "role": m.get("role", ""),
                "content": str(m.get("content", ""))[:300],
                "seq": m.get("seq"),
                "source_ref": m.get("source_ref", ""),
            }
            for m in messages
        ]
        next_offset = offset + len(public_messages)
        return SearchMessagesResult(
            messages=public_messages,
            matched_count=total,
            limit=limit,
            offset=offset,
            has_more=next_offset < total,
            next_offset=next_offset if next_offset < total else None,
        )

    async def _search_memories(
        self,
        request: _MemorySearchRequest,
    ) -> _MemorySearchResult:
        """Shared vector + keyword + RRF retrieval core.

        Passive context injection and explicit recall_memory use this same
        fusion path; callers may differ only in query expansion and output
        rendering.
        """
        #  把用户的查询和辅助查询去重，得到一个列表 query_texts 辅助query是可能的答案
        query_texts = _dedupe_texts([request.query, *request.aux_queries])
        vector_results: list[MemoryItem] = []
        vector_seen: set[str] = set()
        vector_lane_counts: list[int] = []
        vector_lane_new_counts: list[int] = []
        # 语义和关键词检索的融合，先进行语义检索，再进行关键词检索，然后使用RRF算法进行融合
        for query_text in query_texts:
            query_vec = await self.embedder.embed(query_text)
            lane_results = await self.store.vector_search(
                query_vec=query_vec,
                user_id=request.user_id,
                top_k=request.top_k,
                memory_types=request.memory_types,
                include_superseded=request.include_superseded,
            )
            vector_lane_counts.append(len(lane_results))
            new_count = 0
            for mem in lane_results:
                mid = str(mem.id)
                if mid in vector_seen:
                    continue
                vector_seen.add(mid)
                vector_results.append(mem)
                new_count += 1
            vector_lane_new_counts.append(new_count)

        kw_results = await self.store.keyword_search(
            terms=request.query,
            user_id=request.user_id,
            limit=request.keyword_limit or request.top_k,
            memory_types=request.memory_types,
            include_superseded=request.include_superseded,
        )
        combined, rrf_scores, lanes_by_id = _rrf_fuse_with_trace(
            vector_results,
            kw_results,
        )
        return _MemorySearchResult(
            items=combined,
            vector_items=vector_results,
            keyword_items=kw_results,
            rrf_scores=rrf_scores,
            lanes_by_id=lanes_by_id,
            trace={
                "retrieval_mode": "hybrid_rrf",
                "fusion": "rrf",
                "query_count": len(query_texts),
                "vector_count": len(vector_results),
                "keyword_count": len(kw_results),
                "fused_count": len(combined),
                "vector_lane_counts": vector_lane_counts,
                "vector_lane_new_counts": vector_lane_new_counts,
                "keyword_limit": request.keyword_limit or request.top_k,
                "aux_queries": list(request.aux_queries),
            },
        )

    async def _build_aux_queries(self, query: str) -> list[str]:
        #  这个一般是空的
        if self._aux_query_builder is not None:
            queries = await self._aux_query_builder(query)
            return _dedupe_texts([str(item) for item in queries])

        from memory.hyde_enhancer import HyDEEnhancer

        hypothesis = await HyDEEnhancer().generate_hypothesis(query)
        return [hypothesis] if hypothesis else []


class DisabledMemoryEngine:
    async def retrieve(self, request: MemoryRetrieveRequest) -> MemoryRetrieveResult:
        return MemoryRetrieveResult()

    async def retrieve_explicit(
        self, request: ExplicitRetrievalRequest
    ) -> ExplicitRetrievalResult:
        return ExplicitRetrievalResult(error="memory_not_available")

    async def retrieve_interest_block(
        self, request: InterestRetrievalRequest
    ) -> InterestRetrievalResult:
        return InterestRetrievalResult()

    async def remember(self, request: RememberRequest) -> RememberResult:
        return RememberResult(status="failed", error="memory_not_available")

    async def forget(self, request: ForgetRequest) -> ForgetResult:
        return ForgetResult(error="memory_not_available")

    async def fetch_messages(self, request: FetchMessagesRequest) -> FetchMessagesResult:
        return FetchMessagesResult(error="session_store_not_available")

    async def search_messages(self, request: SearchMessagesRequest) -> SearchMessagesResult:
        return SearchMessagesResult(error="session_store_not_available")


def _rrf_fuse(vec_results: list[MemoryItem], kw_results: list[MemoryItem]) -> list[MemoryItem]:
    items, _scores, _lanes = _rrf_fuse_with_trace(vec_results, kw_results)
    return items


# 可能两路检索结果不一样，这个算法就是如何把两个结果融合成一个，本质上就是加权，找到最合适的
def _rrf_fuse_with_trace(
    vec_results: list[MemoryItem],
    kw_results: list[MemoryItem],
) -> tuple[list[MemoryItem], dict[str, float], dict[str, list[str]]]:
    fused: dict[str, dict[str, Any]] = {}
    lanes_by_id: dict[str, list[str]] = {}
    for rank, mem in enumerate(vec_results, 1):
        mid = str(mem.id)
        fused.setdefault(mid, {"mem": mem, "rrf_score": 0.0})
        fused[mid]["rrf_score"] += 1.0 / (_RRF_K + rank)
        _append_lane(lanes_by_id, mid, "vector")
    for rank, mem in enumerate(kw_results, 1):
        mid = str(mem.id)
        fused.setdefault(mid, {"mem": mem, "rrf_score": 0.0})
        fused[mid]["rrf_score"] += 1.0 / (_RRF_K + rank)
        _append_lane(lanes_by_id, mid, "keyword")
    sorted_items = sorted(fused.values(), key=lambda x: x["rrf_score"], reverse=True)
    items = [item["mem"] for item in sorted_items]
    scores = {str(item["mem"].id): float(item["rrf_score"]) for item in sorted_items}
    return items, scores, lanes_by_id


def _append_lane(lanes_by_id: dict[str, list[str]], item_id: str, lane: str) -> None:
    lanes = lanes_by_id.setdefault(item_id, [])
    if lane not in lanes:
        lanes.append(lane)


#  保持唯一性，去掉重复的文本，返回一个新的列表
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


def _memory_item_payload(mem: MemoryItem, *, score: float) -> dict[str, Any]:
    return {
        "id": str(mem.id),
        "memory_type": mem.memory_type,
        "summary": mem.summary,
        "source_ref": mem.source_ref,
        "status": mem.status,
        "score": score,
    }


def _prefer_active_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for _idx, item in sorted(
            enumerate(items),
            key=lambda pair: (_status_rank(pair[1].get("status")), pair[0]),
        )
    ]


def _status_rank(status: Any) -> int:
    value = str(status or "").strip()
    if value == "active":
        return 0
    if value == "superseded":
        return 1
    return 2


def _format_memory_line(mem: MemoryItem) -> str:
    source = f" [{mem.source_ref}]" if mem.source_ref else ""
    return f"- {mem.summary}{source}"


def _session_source_ref(scope: MemoryScope) -> str:
    if scope.user_id is None or scope.chat_id is None:
        return "memorize_tool"
    return f"session:{scope.user_id}:{scope.chat_id}"


def _dedupe_refs(source_refs: list[str]) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for value in source_refs:
        ref = str(value or "").strip()
        if ref and ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return refs


def _parse_session_ref(source_ref: str) -> tuple[int | None, int | None, int | None, int | None]:
    base, _, suffix = source_ref.partition("#")
    parts = base.split(":")
    if len(parts) >= 3 and parts[0] == "session":
        try:
            seq = None
            seq_end = None
            if suffix.startswith("msg:"):
                raw_seq = suffix.split(":", 1)[1]
                if "-" in raw_seq:
                    left, right = raw_seq.split("-", 1)
                    seq = int(left)
                    seq_end = int(right)
                else:
                    seq = int(raw_seq)
            return int(parts[1]), int(parts[2]), seq, seq_end
        except (ValueError, IndexError):
            pass
    return None, None, None, None

# 、 _infer_memory_types() 是一个基于关键词和优先级的路由器，用来决定这次长期记忆检索应该查询哪些 memory_type。
def _infer_memory_types(
    *,
    query: str,
    explicit_memory_type: str | None,
    search_mode: str,
    time_filter: str,
) -> list[str]:
    if explicit_memory_type:
        return [explicit_memory_type]
    if search_mode == "grep" or time_filter:
        return ["event"]
    text = query.lower()
    if _contains_any(text, ("以后", "下次", "你要怎么做", "怎么做", "流程", "规则", "操作规范", "必须", "应该", "工具")):
        return ["procedure"]
    if _contains_any(text, ("今天聊", "今天做", "昨天聊", "昨天做", "最近聊", "最近做", "聊过什么", "做过什么", "发生过", "历史事件")):
        return ["event"]
    if _contains_any(text, ("职业", "工作", "公司", "城市", "居住", "住在", "生日", "年龄", "编程语言", "技术栈", "手机", "设备", "iphone", "android")):
        return ["profile"]
    if _contains_any(text, ("喜欢", "偏好", "推荐", "喝", "咖啡", "茶", "饮品", "饮料", "音乐", "音乐人", "食物", "川菜", "摇滚", "爵士", "不喜欢", "讨厌")):
        return ["preference", "profile"]
    return LONG_TERM_MEMORY_TYPES


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _parse_time_filter(value: str) -> tuple[datetime, datetime] | None:
    text = (value or "").strip()
    if not text:
        return None
    now = datetime.utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if text == "today":
        return today, today + timedelta(days=1)
    if text == "yesterday":
        return today - timedelta(days=1), today
    presets = {"recent_3d": 3, "recent_7d": 7, "recent_30d": 30}
    if text in presets:
        return now - timedelta(days=presets[text]), now
    if "~" in text:
        left, right = [part.strip() for part in text.split("~", 1)]
        try:
            start = datetime.strptime(left, "%Y-%m-%d")
            end = datetime.strptime(right, "%Y-%m-%d") + timedelta(days=1)
            return start, end
        except ValueError:
            return None
    try:
        start = datetime.strptime(text, "%Y-%m-%d")
        return start, start + timedelta(days=1)
    except ValueError:
        return None


def _memory_created_in_window(
    item_id: str,
    memories: list[MemoryItem],
    start: datetime,
    end: datetime,
) -> bool:
    for mem in memories:
        if str(mem.id) != item_id:
            continue
        created_at = mem.created_at
        if created_at is None:
            return False
        if created_at.tzinfo is not None:
            created_at = created_at.replace(tzinfo=None)
        return start <= created_at < end
    return False
