# telegram-bot 项目学习笔记

## `DefaultMemoryEngine`：记忆系统的统一编排层

文件位置：`memory/engine.py`

`DefaultMemoryEngine` 位于 `Embedder`、`MemoryStore` 和 `SessionStore` 之上。它自己不直接执行 SQL，而是决定一个记忆请求应该调用哪些底层组件、怎样组合结果，以及以什么统一格式返回。

```text
调用方
  ↓
DefaultMemoryEngine
  ├── Embedder
  │     └── 查询文本 → 向量
  ├── MemoryStore
  │     ├── 写入长期记忆
  │     ├── 向量检索
  │     ├── 关键词检索
  │     └── 标记旧记忆失效
  └── SessionStore
        ├── 搜索原始消息
        └── 根据 source_ref 回查原文
```

类注释：

```python
"""Facade over the current vector store and raw session store."""
```

这里的 Facade（门面）表示：上层只需要面对一个统一的记忆接口，不需要分别了解向量库、Embedding 和原始 Session 的实现细节。

---

## 一、为什么需要 `DefaultMemoryEngine`

如果调用方直接使用底层类，一次完整记忆检索需要自己完成：

```text
清理查询文字
  ↓
判断应该搜索哪些记忆类型
  ↓
生成 HyDE 辅助查询
  ↓
把原查询和辅助查询分别转换为向量
  ↓
执行多路向量检索
  ↓
执行关键词检索
  ↓
去重并使用 RRF 融合排序
  ↓
处理 active / superseded
  ↓
格式化结果和调试 trace
```

`DefaultMemoryEngine` 把这些步骤封装起来，上层只需要调用：

```python
await engine.retrieve(...)
await engine.retrieve_explicit(...)
await engine.remember(...)
await engine.forget(...)
await engine.search_messages(...)
await engine.fetch_messages(...)
```

所以可以这样区分：

```text
MemoryStore：数据库访问层
DefaultMemoryEngine：记忆业务编排层
```

---

## 二、它是怎样被创建的

`main.py` 先创建三个底层对象：

```python
embedder = Embedder()
memory_store = MemoryStore(embedder)
session_store = get_session_store()
```

然后调用：

```python
memory_runtime = build_memory_runtime(
    embedder=embedder,
    memory_store=memory_store,
    session_store=session_store,
)
```

`memory/bootstrap.py` 内部创建：

```python
engine = DefaultMemoryEngine(
    store=memory_store,
    embedder=embedder,
    session_store=session_store,
)
```

之后主程序通过下面的方式访问它：

```python
memory_runtime.engine
```

例如：

- `BeforeTurnPhase` 使用它自动召回背景记忆。
- 记忆工具使用它执行 `recall_memory`、`memorize`、`search_messages` 和 `fetch_messages`。

---

## 三、`MemoryEngine` Protocol

在 `DefaultMemoryEngine` 前面定义了：

```python
@runtime_checkable
class MemoryEngine(Protocol):
    ...
```

它规定一个记忆引擎应该提供哪些方法：

| 方法 | 作用 |
|---|---|
| `retrieve()` | 自动检索并注入长期记忆。 |
| `retrieve_explicit()` | 显式执行 `recall_memory` 类型的检索。 |
| `retrieve_interest_block()` | 检索用户偏好、档案和长期规则。 |
| `remember()` | 保存一条长期记忆。 |
| `forget()` | 让指定长期记忆失效。 |
| `fetch_messages()` | 根据 `source_ref` 回查原始消息。 |
| `search_messages()` | 搜索原始 Session 消息。 |

Protocol 不提供实现，只规定接口。这样调用方可以使用 `DefaultMemoryEngine`，也可以换成测试用 Fake Engine 或 `DisabledMemoryEngine`。

---

## 四、请求和结果数据结构

### `MemoryScope`

```python
@dataclass(frozen=True)
class MemoryScope:
    user_id: int | None = None
    chat_id: int | None = None
    session_key: str = ""
    channel: str = "telegram"
```

它描述一次记忆操作的作用范围：

| 字段 | 作用 |
|---|---|
| `user_id` | 记忆所属用户，是大多数长期记忆操作的必要条件。 |
| `chat_id` | 当前聊天 ID，用于生成或解析 Session 来源。 |
| `session_key` | 会话标识字符串。 |
| `channel` | 消息渠道，默认 Telegram。 |

### 主要请求与返回对象

| 请求 | 返回 | 用途 |
|---|---|---|
| `MemoryRetrieveRequest` | `MemoryRetrieveResult` | 被动检索长期记忆。 |
| `ExplicitRetrievalRequest` | `ExplicitRetrievalResult` | 显式召回长期记忆。 |
| `InterestRetrievalRequest` | `InterestRetrievalResult` | 召回用户兴趣和特征。 |
| `RememberRequest` | `RememberResult` | 保存长期记忆。 |
| `ForgetRequest` | `ForgetResult` | 使记忆失效。 |
| `FetchMessagesRequest` | `FetchMessagesResult` | 按来源读取原始消息。 |
| `SearchMessagesRequest` | `SearchMessagesResult` | 搜索原始消息。 |

输入 dataclass 多数使用 `frozen=True`，创建后不能重新给字段赋值。结果 dataclass 可修改，并通过 `default_factory` 为列表和字典创建独立默认值。

---

## 五、`DefaultMemoryEngine.__init__()`

原始代码：

```python
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
```

### 代码的作用

使用依赖注入保存四个组件：

| 属性 | 作用 |
|---|---|
| `store` | 长期记忆的写入、查询和状态修改。 |
| `embedder` | 将查询文本转换成向量。 |
| `session_store` | 搜索、读取原始聊天。 |
| `_aux_query_builder` | 可选的辅助查询生成器，主要用于测试或自定义查询扩展。 |

参数列表中的 `*` 表示后面的参数必须使用关键字传入：

```python
DefaultMemoryEngine(
    store=memory_store,
    embedder=embedder,
    session_store=session_store,
)
```

创建对象时不会立即执行 Embedding、数据库查询或网络请求。

---

## 六、`retrieve()`：被动长期记忆检索

### 使用场景

`BeforeTurnPhase` 在每轮推理前调用这个方法，把相关长期记忆作为背景注入 Prompt。

它属于被动检索：即使 LLM 还没有主动调用 `recall_memory`，Pipeline 也会先尝试提供可能有用的记忆。

### 输入检查

```python
query = request.query.strip()
user_id = request.scope.user_id
if not query or user_id is None:
    return MemoryRetrieveResult()
```

- 去掉查询首尾空白。
- 没有查询内容或用户 ID 时直接返回空结果。
- 返回空结果而不是抛异常，调用方可以继续完成回复链。

### 生成辅助查询

```python
aux_queries = await self._build_aux_queries(query)
```

默认通过 HyDE 生成一条更像长期记忆摘要的假设文本。

例如：

```text
原问题：我家打印机是什么型号？
辅助假设：用户家里的打印机型号是 Brother HL-L2460DW
```

辅助假设只是检索用查询，不是真实记忆，也不能直接作为事实答案。

### 调用统一混合检索

```python
search = await self._search_memories(
    _MemorySearchRequest(
        query=query,
        user_id=user_id,
        top_k=request.top_k,
        memory_types=request.memory_types or LONG_TERM_MEMORY_TYPES,
        aux_queries=aux_queries,
    )
)
```

没有指定类型时搜索全部长期记忆类型：

```python
["profile", "preference", "procedure", "event", "fact"]
```

### 格式化 Prompt 文本

```python
lines = [
    _format_memory_line(m)
    for m in search.items[:request.top_k]
]
```

`_format_memory_line()` 生成：

```text
- 用户喜欢喝拿铁 [session:42:7#msg:8-9]
```

有 `source_ref` 时附在摘要后面，方便后续使用 `fetch_messages` 回查证据。

### 判断 HyDE 是否真正有贡献

```python
aux_new_counts = search.trace.get("vector_lane_new_counts") or []
hyde_added = any(int(count) > 0 for count in list(aux_new_counts)[1:])
```

第一条 vector lane 是原查询，后面的 lane 是辅助查询。

只有辅助查询召回了原查询没有召回的新记忆时：

```python
hyde_used = True
```

所以 `hyde_used` 不是“是否生成了辅助查询”，而是“辅助查询是否真的增加了新结果”。

### 返回结果

```python
MemoryRetrieveResult(
    items=search.items,
    text_block="\n".join(lines),
    trace=trace,
)
```

| 字段 | 作用 |
|---|---|
| `items` | 融合后的 `MemoryItem` 列表。 |
| `text_block` | 截取前 `top_k` 条后生成的 Prompt 文本。 |
| `trace` | 检索通道数量、结果数、HyDE 贡献等诊断信息。 |

一个细节是：`text_block` 明确截取前 `top_k`，但 `items` 当前返回完整的融合列表，可能多于 `top_k`。

### 数据库和网络操作

`retrieve()` 间接执行：

| 组件 | 操作 |
|---|---|
| `HyDEEnhancer` | 可能调用 LLM 生成辅助查询。 |
| `Embedder` | 为原查询和辅助查询生成向量。 |
| `MemoryStore.vector_search()` | 查询 `vec_items JOIN memory_items`。 |
| `MemoryStore.keyword_search()` | 查询 `memory_items.summary`。 |

---

## 七、`retrieve_explicit()`：显式长期记忆召回

### 使用场景

这个方法通常由 `recall_memory` 工具调用。相比 `retrieve()`，它支持更多显式参数：

```python
memory_type
include_superseded
search_mode
time_filter
limit
```

### 参数预处理

```python
memory_type = request.memory_type.strip() or None
search_mode = request.search_mode.strip() or "semantic"
time_filter = request.time_filter.strip()
time_window = _parse_time_filter(time_filter)
```

非法时间返回：

```python
ExplicitRetrievalResult(error="invalid_time_filter")
```

非法搜索模式会回退为：

```text
semantic
```

### 推断记忆类型

```python
memory_types = _infer_memory_types(...)
```

优先级：

1. 用户明确传了 `memory_type`，只搜索该类型。
2. `grep` 或带时间范围，默认搜索 `event`。
3. 根据查询关键词推断 `procedure`、`event`、`profile` 或 `preference`。
4. 无法判断时搜索所有长期记忆类型。

例如：

```text
“以后应该怎么做” → procedure
“昨天发生了什么” → event
“我的职业是什么” → profile
“我喜欢喝什么”   → preference + profile
```

### 限制返回数量

```python
max_limit = 50 if search_mode == "grep" else 10
limit = max(1, min(int(request.limit), max_limit))
```

- `semantic` 最多 10 条。
- `grep` 最多 50 条。
- 最少 1 条。

---

## 八、`retrieve_explicit()` 的 `grep` 模式

### 代码的作用

按照时间范围和类型列出长期记忆，适合查询：

```text
今天发生了什么？
最近 7 天有哪些事件？
2026-08-01 到 2026-08-19 的记录是什么？
```

这个模式必须提供有效时间范围，否则返回：

```python
error="time_filter_required"
```

### 调用底层存储

```python
grep_results = self.store.list_memories(
    user_id=user_id,
    memory_types=[memory_type] if memory_type else ["event"],
    include_superseded=request.include_superseded,
    created_start=start,
    created_end=end,
    limit=limit,
)
```

这里需要注意：名称叫 `grep`，但当前实现并没有在摘要上执行关键词 grep，而是按照时间、用户、状态和类型调用 `list_memories()`。

### 结果处理

每条 `MemoryItem` 通过 `_memory_item_payload()` 转换成字典：

```python
{
    "id": ...,
    "memory_type": ...,
    "summary": ...,
    "source_ref": ...,
    "status": ...,
    "score": 1.0,
}
```

如果包含 `superseded`，使用 `_prefer_active_items()` 保证 `active` 排在旧记忆前面。

### 数据库操作

| 表 | 操作 |
|---|---|
| `memory_items` | 按用户、类型、状态和创建时间执行 `SELECT`。 |

---

## 九、`retrieve_explicit()` 的 `semantic` 模式

### 代码的作用

这是显式召回的默认模式，和 `retrieve()` 共用 `_search_memories()`：

```text
HyDE 辅助查询
  +
多路向量搜索
  +
关键词搜索
  ↓
RRF 融合
```

### 构造公开结果

对每条记忆获取：

```python
lanes = search.lanes_by_id.get(mid, [])
```

`lanes` 可能是：

```python
["vector"]
["keyword"]
["vector", "keyword"]
```

然后返回：

```python
payload["rrf_score"] = ...
payload["lanes"] = lanes
```

基础 `score` 当前是一个粗粒度标记：

```text
来自 vector → 1.0
只来自 keyword → 0.5
```

真正用于融合排序的是 `rrf_score`。

### 可选时间过滤

即使使用 `semantic`，也可以传入时间范围。代码先做混合检索，再用 `_memory_created_in_window()` 根据记忆的 `created_at` 过滤。

时间范围采用左闭右开：

```text
start <= created_at < end
```

### active 优先

如果：

```python
include_superseded=True
```

会把 `active` 结果排在 `superseded` 前面，同时保持同一状态组内原来的相对顺序。

---

## 十、`retrieve_interest_block()`：检索用户兴趣信息

这个方法复用 `retrieve()`，但只搜索：

```python
["preference", "profile", "procedure"]
```

它排除普通 `event` 和 `fact`，重点返回：

- 用户偏好。
- 用户档案。
- 长期操作规则。

结果简化为：

```python
{
    "id": str(item.id),
    "text": item.summary,
    "memory_type": item.memory_type,
    "source_ref": item.source_ref,
}
```

并生成只包含摘要的 `text_block`。

---

## 十一、`remember()`：保存长期记忆

### 输入校验

必须满足：

- `summary` 非空。
- `memory_type` 非空。
- `user_id` 存在。
- 类型属于 `procedure`、`preference`、`event`、`profile`、`fact`。

否则返回 `RememberResult(status="failed", error=...)`，不会抛给调用方。

### 生成来源

```python
source_ref=request.source_ref or _session_source_ref(request.scope)
```

如果请求已经提供来源，就使用它；否则：

```text
user_id 和 chat_id 都存在
→ session:{user_id}:{chat_id}

缺少其中一个
→ memorize_tool
```

这里生成的是 Session 级来源，不包含具体消息序号。

### 写入长期记忆

```python
item = await self.store.upsert_item(...)
```

底层会：

```text
Embedder.embed(summary)
  ↓
INSERT memory_items
  ↓
INSERT vec_items
  ↓
commit
```

成功返回 `status="saved"` 和新记忆 ID；异常被捕获并转换为 `status="failed"`。

### 数据库操作

| 表 | 操作 |
|---|---|
| `memory_items` | 插入记忆摘要、类型、状态和来源。 |
| `vec_items` | 插入该记忆的向量。 |

---

## 十二、`forget()`：让长期记忆失效

```python
updated = self.store.mark_superseded_batch(
    request.ids,
    user_id=request.scope.user_id,
)
```

它不物理删除记忆，而是把仍为 `active` 的目标记忆更新为：

```text
status = superseded
```

返回结果：

| 字段 | 作用 |
|---|---|
| `superseded_ids` | 实际从 active 更新为 superseded 的 ID。 |
| `missing_ids` | 没有被更新的输入 ID。 |
| `error` | 底层操作异常信息。 |

`missing_ids` 不一定表示数据库里完全不存在，也可能表示：

- 已经是 `superseded`。
- 不属于指定用户。
- 输入重复或无效。

当前调用的是 `mark_superseded_batch()`，所以只更新状态，不写 `memory_replacements` 替换关系。

### 数据库操作

| 表 | 操作 |
|---|---|
| `memory_items` | 查询仍为 active 的目标 ID，然后批量更新为 superseded。 |

---

## 十三、`fetch_messages()`：根据 `source_ref` 回查原文

### 代码的作用

长期 Memory 是摘要。回答精确事实前，需要根据摘要携带的 `source_ref` 回到 Session 获取原始消息。

### 去重和参数限制

```python
refs = _dedupe_refs(request.source_refs)
limit = max(1, min(int(request.limit), 50))
context = max(0, min(int(request.context), 10))
```

- 去除空引用和重复引用。
- 最多返回 50 条消息。
- 前后文范围最多 10 条。

没有有效引用时返回：

```python
error="source_ref_required"
```

### 解析引用

```python
user_id, chat_id, seq, seq_end = _parse_session_ref(source_ref)
```

支持：

```text
session:42:7
session:42:7#msg:8
session:42:7#msg:8-11
```

分别表示：

- 整个 Session。
- 一条消息。
- 一段消息窗口。

非法引用会放入：

```python
invalid_source_refs
```

### 调用 SessionStore

```python
fetched, matched = self.session_store.fetch_messages(...)
```

对于多个可能重叠的引用，代码使用：

```python
(source_ref, seq)
```

作为去重键，避免相同消息重复返回。

### 整理公开结果

- 消息正文最多保留 500 个字符。
- 保留 role、seq、source_ref。
- `in_source_ref` 区分目标消息与 context 附带的上下文消息。
- 最终结果总数再截断到 `limit`。

如果存在非法引用但同时也取到了有效消息，`invalid_source_refs` 会记录问题，但 `error` 保持为空；只有全部没有取到且存在非法引用时才设置 error。

### 数据库操作

| 表 | 操作 |
|---|---|
| `conversation_sessions` | 按 `user_id + chat_id` 读取完整消息 JSON，再由 SessionStore 切片。 |

---

## 十四、`search_messages()`：搜索原始 Session 消息

### 代码的作用

调用：

```python
self.session_store.search_messages(...)
```

搜索指定用户的原始聊天，而不是搜索长期记忆摘要。

### 输入处理

- 查询为空或没有用户 ID 时返回空结果。
- `limit` 限制为 1～50。
- `offset` 最小为 0。
- 可以使用 `role` 只搜索 user 或 assistant 消息。

### 结果处理

每条消息只公开：

```python
{
    "role": ...,
    "content": 最多300字符,
    "seq": ...,
    "source_ref": ...,
}
```

分页字段：

```python
next_offset = offset + len(public_messages)
has_more = next_offset < total
```

还有更多结果时返回下次使用的 `next_offset`，否则为 `None`。

### 与长期记忆检索的区别

```text
retrieve / retrieve_explicit
→ 搜索 memory_items 中的长期摘要

search_messages
→ 搜索 conversation_sessions 中的原始对话
```

### 数据库操作

| 表 | 操作 |
|---|---|
| `conversation_sessions` | 读取指定用户的所有 Session；文字匹配在 Python 中完成。 |

---

## 十五、`_search_memories()`：混合检索核心

这个内部方法同时被以下方法使用：

```text
retrieve()
retrieve_explicit() 的 semantic 模式
```

因此被动 Prompt 注入和显式 `recall_memory` 使用同一套核心检索逻辑。

### 1. 合并并去重查询文本

```python
query_texts = _dedupe_texts([
    request.query,
    *request.aux_queries,
])
```

顺序通常是：

```text
原始查询
HyDE 辅助查询 1
HyDE 辅助查询 2
```

空字符串和完全重复的文本会被移除，原有顺序得到保留。

### 2. 为每条查询执行向量检索

```python
for query_text in query_texts:
    query_vec = await self.embedder.embed(query_text)
    lane_results = await self.store.vector_search(...)
```

如果有一个原查询和一个辅助查询，就会发生：

- 两次 Embedding API 调用。
- 两次向量数据库查询。

每个查询形成一条 vector lane。

### 3. 合并向量结果并按 ID 去重

```python
vector_seen: set[str] = set()
```

同一条记忆可能被多个查询召回，只保留第一次出现的位置。

同时记录：

```python
vector_lane_counts
vector_lane_new_counts
```

例如：

```text
原查询召回 5 条，其中 5 条是新结果
辅助查询召回 5 条，其中 2 条是新结果
```

trace 为：

```python
vector_lane_counts = [5, 5]
vector_lane_new_counts = [5, 2]
```

### 4. 执行关键词检索

```python
kw_results = await self.store.keyword_search(
    terms=request.query,
    ...
)
```

关键词通道只使用原始查询，不使用 HyDE 辅助文本。

### 5. RRF 融合

```python
combined, rrf_scores, lanes_by_id = _rrf_fuse_with_trace(
    vector_results,
    kw_results,
)
```

它把向量通道和关键词通道合成一个排名列表。

### 6. 返回检索结果和 trace

`_MemorySearchResult` 同时保存：

- 最终融合结果。
- 原始向量结果。
- 原始关键词结果。
- 每条记忆的 RRF 分数。
- 每条记忆命中的通道。
- 检索过程统计信息。

trace 示例：

```python
{
    "retrieval_mode": "hybrid_rrf",
    "fusion": "rrf",
    "query_count": 2,
    "vector_count": 7,
    "keyword_count": 3,
    "fused_count": 8,
    "vector_lane_counts": [5, 5],
    "vector_lane_new_counts": [5, 2],
    "keyword_limit": 5,
    "aux_queries": [...],
}
```

---

## 十六、RRF 排名融合

RRF 全称 Reciprocal Rank Fusion，即倒数排名融合。

常量：

```python
_RRF_K = 60
```

每条结果在一个通道中的得分公式：

```text
1 / (60 + rank)
```

其中 rank 从 1 开始。

### 计算示例

记忆 A：

```text
向量排名第 1
关键词排名第 2
```

得分：

```text
1 / 61 + 1 / 62 ≈ 0.03252
```

记忆 B：

```text
只在向量排名第 2
```

得分：

```text
1 / 62 ≈ 0.01613
```

因此同时被语义和关键词检索命中的 A 通常排在 B 前面。

### 为什么使用 RRF

向量检索使用距离，关键词检索没有同样的距离尺度，两个原始分数不能简单相加。RRF 只依赖各通道排名，不要求它们使用相同评分标准。

### `lanes_by_id`

`_append_lane()` 记录一条记忆来自哪些通道，并避免重复名称：

```python
{
    "记忆ID": ["vector", "keyword"]
}
```

---

## 十七、`_build_aux_queries()` 和 HyDE

### 自定义构建器

如果初始化时传入了：

```python
aux_query_builder
```

就调用它并去重结果。这主要便于测试或替换默认策略。

### 默认 HyDE

没有自定义构建器时：

```python
from memory.hyde_enhancer import HyDEEnhancer
hypothesis = await HyDEEnhancer().generate_hypothesis(query)
```

HyDE 的思路：

```text
用户问题
  ↓
LLM 生成一条“可能的记忆陈述”
  ↓
对该陈述生成向量
  ↓
用这个向量辅助查找真正记忆
```

例如：

```text
问题：我家打印机是什么型号？
假设：用户家打印机型号是 Brother HL-L2460DW。
```

假设可能是错的，所以它只能用于检索，最终回答仍然必须依据真正召回的 Memory 和 Session 原文。

---

## 十八、记忆类型推断 `_infer_memory_types()`

### 代码的作用

这个函数根据用户的查询内容，推断本次应该搜索哪几类长期记忆，从而缩小 `MemoryStore` 的检索范围。

例如用户问：

```text
我喜欢喝什么？
```

函数会认为这是偏好问题，返回：

```python
["preference", "profile"]
```

后续向量检索和关键词检索就只查询这两类记忆，而不是搜索全部长期记忆。

### 完整代码

```python
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
```

### 输入和输出

输入参数：

| 参数 | 作用 |
|---|---|
| `query` | 用户的问题或长期记忆查询文本。 |
| `explicit_memory_type` | 调用者是否已经明确指定记忆类型。 |
| `search_mode` | 检索模式，例如 `semantic` 或 `grep`。 |
| `time_filter` | 时间条件，例如 `today`、`recent_7d`。 |

返回值：

```python
list[str]
```

也就是应该搜索的记忆类型列表。

输入示例：

```python
types = _infer_memory_types(
    query="我喜欢喝什么？",
    explicit_memory_type=None,
    search_mode="semantic",
    time_filter="",
)
```

输出：

```python
["preference", "profile"]
```

参数列表开头的 `*` 表示所有参数必须用名称传递，不能按位置传递。

正确：

```python
_infer_memory_types(
    query="我的职业是什么？",
    explicit_memory_type=None,
    search_mode="semantic",
    time_filter="",
)
```

不允许：

```python
_infer_memory_types(
    "我的职业是什么？",
    None,
    "semantic",
    "",
)
```

### 判断优先级图

代码从上向下执行，一旦命中就立即 `return`，后面的规则不会再检查：

```text
是否明确指定 memory_type？
  ├── 是 → 返回指定类型
  └── 否
       ↓
是否为 grep，或者设置了 time_filter？
  ├── 是 → [event]
  └── 否
       ↓
是否命中流程/规则关键词？
  ├── 是 → [procedure]
  └── 否
       ↓
是否命中历史事件关键词？
  ├── 是 → [event]
  └── 否
       ↓
是否命中用户档案关键词？
  ├── 是 → [profile]
  └── 否
       ↓
是否命中偏好关键词？
  ├── 是 → [preference, profile]
  └── 否 → 搜索全部长期记忆类型
```

### 1. 明确指定类型时直接返回

```python
if explicit_memory_type:
    return [explicit_memory_type]
```

输入：

```python
_infer_memory_types(
    query="我的信息是什么？",
    explicit_memory_type="profile",
    search_mode="semantic",
    time_filter="",
)
```

输出：

```python
["profile"]
```

明确参数的优先级最高。即使 `query` 中包含其他类型关键词，也不会继续推断。

### 2. 时间查询默认返回 `event`

```python
if search_mode == "grep" or time_filter:
    return ["event"]
```

只要搜索模式是 `grep`，或者 `time_filter` 是非空字符串，就默认查询事件记忆。

输入：

```python
_infer_memory_types(
    query="最近发生过什么？",
    explicit_memory_type=None,
    search_mode="grep",
    time_filter="recent_7d",
)
```

输出：

```python
["event"]
```

设计思想是：按日期或时间范围查询的内容通常是在询问“发生过什么”。

### 3. 流程和规则返回 `procedure`

相关关键词包括：

```text
以后、下次、你要怎么做、怎么做、流程、规则、操作规范、必须、应该、工具
```

输入：

```python
_infer_memory_types(
    query="以后部署之前必须做什么？",
    explicit_memory_type=None,
    search_mode="semantic",
    time_filter="",
)
```

输出：

```python
["procedure"]
```

因为查询命中了“以后”和“必须”。

`procedure` 通常保存：

```text
以后部署前必须运行测试
下次执行评测前需要清理数据库
提交代码前要运行格式检查
```

### 4. 历史行为返回 `event`

相关关键词包括：

```text
今天聊、昨天做、最近聊、聊过什么、做过什么、发生过、历史事件
```

输入：

```python
_infer_memory_types(
    query="我们昨天聊过什么？",
    explicit_memory_type=None,
    search_mode="semantic",
    time_filter="",
)
```

输出：

```python
["event"]
```

### 5. 身份和设备信息返回 `profile`

相关关键词包括：

```text
职业、工作、公司、城市、居住、生日、年龄、编程语言、技术栈、手机、设备、iPhone、Android
```

代码先执行：

```python
text = query.lower()
```

所以：

```text
iPhone → iphone
Android → android
```

可以命中小写关键词。

输入：

```python
_infer_memory_types(
    query="我现在使用的是 iPhone 还是 Android？",
    explicit_memory_type=None,
    search_mode="semantic",
    time_filter="",
)
```

输出：

```python
["profile"]
```

`profile` 通常保存：

```text
用户是一名后端工程师
用户居住在上海
用户主要使用 Python
用户现在使用 Android 手机
```

### 6. 喜好问题返回 `preference` 和 `profile`

相关关键词包括：

```text
喜欢、偏好、推荐、喝、咖啡、茶、饮品、音乐、食物、不喜欢、讨厌
```

输入：

```python
_infer_memory_types(
    query="根据我的偏好推荐一种饮料",
    explicit_memory_type=None,
    search_mode="semantic",
    time_filter="",
)
```

输出：

```python
["preference", "profile"]
```

这里同时搜索 `profile`，是因为一些用户偏好可能被保存为用户档案信息。同时搜索两个类型可以减少漏检。

### 7. 无法判断时搜索所有类型

```python
return LONG_TERM_MEMORY_TYPES
```

长期记忆共有五种类型：

```python
[
    "profile",
    "preference",
    "procedure",
    "event",
    "fact",
]
```

输入：

```python
_infer_memory_types(
    query="你还记得那个东西吗？",
    explicit_memory_type=None,
    search_mode="semantic",
    time_filter="",
)
```

因为没有命中任何明确关键词，输出：

```python
[
    "profile",
    "preference",
    "procedure",
    "event",
    "fact",
]
```

搜索范围虽然更大，但可以避免因为分类不出来而完全漏掉结果。

### `_contains_any()` 是如何检查关键词的

```python
def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)
```

输入：

```python
_contains_any(
    "我喜欢喝拿铁",
    ("喜欢", "咖啡", "茶"),
)
```

执行逻辑：

```text
“喜欢”是否在文本中？ → True
至少一个关键词命中    → 返回 True
```

输出：

```python
True
```

它只是执行子字符串检查，不理解真正语义。

### 规则顺序可能造成误判

例如：

```text
这个工具是我喜欢的吗？
```

它同时包含：

```text
工具 → procedure 关键词
喜欢 → preference 关键词
```

但 `procedure` 判断写在 `preference` 前面，所以会先返回：

```python
["procedure"]
```

不会继续得到：

```python
["preference", "profile"]
```

因此代码顺序本身就是分类优先级。

### 设计优点和局限

```text
优点
├── 执行速度快
├── 不需要调用 LLM
├── 相同输入结果稳定
└── 规则容易查看和调试

局限
├── 依赖预先写好的关键词
├── 无法真正理解复杂语义
├── 关键词覆盖不全时会搜索全部类型
└── 同时命中多类时通常只返回排在前面的类别
```

最核心的理解：

> `_infer_memory_types()` 是一个基于关键词和判断优先级的检索路由器，用来决定本次长期记忆查询应该搜索哪些 `memory_type`。

---

## 十九、时间过滤 `_parse_time_filter()`

支持：

| 输入 | 时间窗口 |
|---|---|
| `today` | 今天 00:00 到明天 00:00 |
| `yesterday` | 昨天 00:00 到今天 00:00 |
| `recent_3d` | 当前时间往前 3 天 |
| `recent_7d` | 当前时间往前 7 天 |
| `recent_30d` | 当前时间往前 30 天 |
| `YYYY-MM-DD` | 指定日期的一整天 |
| `开始日期~结束日期` | 包含开始日和结束日的日期范围 |

代码使用：

```python
datetime.utcnow()
```

因此这里按照 UTC 的无时区 datetime 计算，不是 `Asia/Shanghai` 本地时间。对于 `today` 和 `yesterday`，需要意识到 UTC 日期边界与中国本地日期边界可能相差 8 小时。

非法格式返回 `None`。

---

## 二十、其他辅助函数

### `_dedupe_texts()`

去掉：

- 空文本。
- 首尾空白。
- 完全重复文本。

保留第一次出现的顺序。

### `_dedupe_refs()`

对 `source_ref` 做相同的清理与去重。

### `_memory_item_payload()`

把 `MemoryItem` 转换成可以返回给工具调用方的普通字典。

### `_prefer_active_items()`

按照状态排序：

```text
active → superseded → 其他状态
```

同一状态内部保持原顺序。

### `_format_memory_line()`

把记忆格式化成 Prompt 行，并在存在时附加 `source_ref`。

### `_parse_session_ref()`

解析 Session、单条消息或消息范围引用。非法格式统一返回四个 `None`。

### `_memory_created_in_window()`

在已经召回的 `MemoryItem` 中查找目标 ID，并判断 `created_at` 是否位于左闭右开的时间窗口。

如果时间带时区，会通过：

```python
created_at.replace(tzinfo=None)
```

直接移除时区信息，再与无时区的 start/end 比较。

---

## 二十一、数据库操作总结

`DefaultMemoryEngine` 自己没有调用 `get_connection()`，但通过底层组件间接操作数据库。

| Engine 方法 | 底层方法 | 涉及表 |
|---|---|---|
| `retrieve()` | `vector_search()`、`keyword_search()` | `vec_items`、`memory_items` |
| `retrieve_explicit()` semantic | `vector_search()`、`keyword_search()` | `vec_items`、`memory_items` |
| `retrieve_explicit()` grep | `list_memories()` | `memory_items` |
| `retrieve_interest_block()` | 复用 `retrieve()` | `vec_items`、`memory_items` |
| `remember()` | `upsert_item()` | `memory_items`、`vec_items` |
| `forget()` | `mark_superseded_batch()` | `memory_items` |
| `fetch_messages()` | `SessionStore.fetch_messages()` | `conversation_sessions` |
| `search_messages()` | `SessionStore.search_messages()` | `conversation_sessions` |

它不直接操作 Markdown Memory；Markdown 层由 `MarkdownMemoryStore` 和相关 Worker 管理。

---

## 二十二、一次完整被动检索示例

用户发送：

```text
我家打印机是什么型号？
```

执行链路：

```text
BeforeTurnPhase
  ↓
DefaultMemoryEngine.retrieve()
  ↓
_build_aux_queries()
  ├── 原问题：我家打印机是什么型号？
  └── HyDE：用户家打印机型号是 Brother HL-L2460DW
  ↓
_search_memories()
  ├── Embedder.embed(原问题)
  │     ↓
  │   MemoryStore.vector_search()
  ├── Embedder.embed(HyDE 假设)
  │     ↓
  │   MemoryStore.vector_search()
  └── MemoryStore.keyword_search(原问题)
  ↓
按记忆 ID 去重
  ↓
RRF 融合 vector + keyword
  ↓
生成 text_block 和 trace
  ↓
BeforeTurnCtx.retrieved_memories
  ↓
BeforeReasoningPhase 加入 Prompt
```

---

## 二十三、一次带证据的显式召回示例

用户问：

```text
你还记得我以前喜欢喝什么吗？
```

可能执行：

```text
Reasoner 调用 recall_memory
  ↓
DefaultMemoryEngine.retrieve_explicit(
    query="用户过去和现在的饮品偏好",
    include_superseded=True,
)
  ↓
返回 active 和 superseded 记忆
  ↓
结果包含 source_ref
  ↓
Reasoner 调用 fetch_messages(source_ref)
  ↓
DefaultMemoryEngine.fetch_messages()
  ↓
SessionStore.fetch_messages()
  ↓
返回原始对话
  ↓
LLM 基于原文回答
```

这体现了两层职责：

```text
长期 Memory：快速找到相关线索
原始 Session：提供可以核对的事实证据
```

---

## 二十四、函数输入与输出示例总览

这一节按照“一个函数对应一个输入和输出例子”集中整理 `DefaultMemoryEngine` 的全部方法。

### 方法路由图

```text
DefaultMemoryEngine
│
├── retrieve(request)
│     ├── _build_aux_queries(query)
│     └── _search_memories(request)
│            ├── Embedder.embed()
│            ├── MemoryStore.vector_search()
│            ├── MemoryStore.keyword_search()
│            └── RRF 融合
│
├── retrieve_explicit(request)
│     ├── semantic → _build_aux_queries + _search_memories
│     └── grep     → MemoryStore.list_memories
│
├── retrieve_interest_block(request)
│     └── 复用 retrieve()
│
├── remember(request)
│     └── MemoryStore.upsert_item()
│
├── forget(request)
│     └── MemoryStore.mark_superseded_batch()
│
├── fetch_messages(request)
│     └── SessionStore.fetch_messages()
│
└── search_messages(request)
      └── SessionStore.search_messages()
```

### 1. `__init__()` 输入和结果

输入：

```python
engine = DefaultMemoryEngine(
    store=memory_store,
    embedder=embedder,
    session_store=session_store,
    aux_query_builder=None,
)
```

结果：

```text
创建一个 DefaultMemoryEngine 对象
  ├── engine.store         指向 memory_store
  ├── engine.embedder      指向 embedder
  ├── engine.session_store 指向 session_store
  └── engine._aux_query_builder = None
```

这个函数没有显式返回值。Python 构造流程最终返回新创建的 `engine` 对象。初始化期间不调用网络，也不访问数据库。

### 2. `retrieve()` 输入和输出

输入示例：

```python
request = MemoryRetrieveRequest(
    query="我家打印机是什么型号？",
    scope=MemoryScope(user_id=42, chat_id=7),
    top_k=3,
    memory_types=["profile"],
)

result = await engine.retrieve(request)
```

假设数据库中召回了一条记忆，输出逻辑上类似：

```python
MemoryRetrieveResult(
    items=[
        MemoryItem(
            id=UUID("..."),
            user_id=42,
            memory_type="profile",
            summary="用户家打印机型号是 Brother HL-L2460DW",
            status="active",
            source_ref="session:42:7#msg:8-9",
            embedding=[...],
        )
    ],
    text_block=(
        "- 用户家打印机型号是 Brother HL-L2460DW "
        "[session:42:7#msg:8-9]"
    ),
    trace={
        "retrieval_mode": "hybrid_rrf",
        "fusion": "rrf",
        "query_count": 2,
        "vector_count": 1,
        "keyword_count": 1,
        "fused_count": 1,
        "hyde_used": True,
        "hypothesis": "用户家打印机型号是 Brother HL-L2460DW",
        "aux_queries": ["用户家打印机型号是 Brother HL-L2460DW"],
    },
)
```

空输入示例：

```python
await engine.retrieve(
    MemoryRetrieveRequest(
        query="   ",
        scope=MemoryScope(user_id=42),
    )
)
```

输出：

```python
MemoryRetrieveResult(items=[], text_block="", trace={})
```

### 3. `retrieve_explicit()` semantic 模式输入和输出

输入示例：

```python
request = ExplicitRetrievalRequest(
    query="用户过去和现在的饮品偏好",
    scope=MemoryScope(user_id=42, chat_id=7),
    memory_type="preference",
    include_superseded=True,
    search_mode="semantic",
    limit=5,
)

result = await engine.retrieve_explicit(request)
```

输出逻辑上类似：

```python
ExplicitRetrievalResult(
    items=[
        {
            "id": "new-memory-id",
            "memory_type": "preference",
            "summary": "用户现在主要喝绿茶",
            "source_ref": "session:42:7#msg:20-21",
            "status": "active",
            "score": 1.0,
            "rrf_score": 0.032522,
            "lanes": ["vector", "keyword"],
        },
        {
            "id": "old-memory-id",
            "memory_type": "preference",
            "summary": "用户以前每天喝咖啡",
            "source_ref": "session:42:7#msg:2-3",
            "status": "superseded",
            "score": 1.0,
            "rrf_score": 0.016129,
            "lanes": ["vector"],
        },
    ],
    applied_memory_types=["preference"],
    error="",
    trace={
        "retrieval_mode": "hybrid_rrf",
        "fusion": "rrf",
        "hyde_used": True,
        "hypothesis": "用户以前喝咖啡，现在主要喝绿茶",
        # 还包含各通道数量等字段
    },
)
```

图示：

```text
ExplicitRetrievalRequest
  ├── include_superseded=True
  └── memory_type=preference
            ↓
      active + superseded
            ↓
active 结果优先，旧记忆随后
```

### 4. `retrieve_explicit()` grep 模式输入和输出

输入示例：

```python
request = ExplicitRetrievalRequest(
    query="最近发生了什么？",
    scope=MemoryScope(user_id=42),
    search_mode="grep",
    time_filter="recent_7d",
    limit=10,
)

result = await engine.retrieve_explicit(request)
```

输出逻辑上类似：

```python
ExplicitRetrievalResult(
    items=[
        {
            "id": "event-memory-id",
            "memory_type": "event",
            "summary": "用户本周更换了 Android 手机",
            "source_ref": "session:42:7#msg:30-31",
            "status": "active",
            "score": 1.0,
        }
    ],
    applied_memory_types=["event"],
    error="",
    trace={},
)
```

没有时间条件时：

```python
ExplicitRetrievalResult(
    items=[],
    applied_memory_types=[],
    error="time_filter_required",
    trace={},
)
```

### 5. `retrieve_interest_block()` 输入和输出

输入示例：

```python
request = InterestRetrievalRequest(
    query="用户有什么兴趣和长期习惯？",
    scope=MemoryScope(user_id=42, chat_id=7),
    top_k=2,
)

result = await engine.retrieve_interest_block(request)
```

输出逻辑上类似：

```python
InterestRetrievalResult(
    text_block="用户喜欢爵士乐\n用户习惯提交前运行测试",
    hits=[
        {
            "id": "preference-id",
            "text": "用户喜欢爵士乐",
            "memory_type": "preference",
            "source_ref": "session:42:7#msg:4",
        },
        {
            "id": "procedure-id",
            "text": "用户习惯提交前运行测试",
            "memory_type": "procedure",
            "source_ref": "session:42:7#msg:10",
        },
    ],
    trace={"retrieval_mode": "hybrid_rrf", "fusion": "rrf"},
)
```

这个函数只会通过 `retrieve()` 搜索 `preference`、`profile` 和 `procedure`。

### 6. `remember()` 输入和输出

输入示例：

```python
request = RememberRequest(
    summary="用户喜欢喝拿铁",
    memory_type="preference",
    scope=MemoryScope(user_id=42, chat_id=7),
    source_ref="session:42:7#msg:8",
)

result = await engine.remember(request)
```

成功输出：

```python
RememberResult(
    status="saved",
    item_id="新生成的UUID",
    summary="用户喜欢喝拿铁",
    memory_type="preference",
    error="",
)
```

非法类型输入：

```python
RememberRequest(
    summary="用户喜欢喝拿铁",
    memory_type="unknown",
    scope=MemoryScope(user_id=42),
)
```

输出：

```python
RememberResult(
    status="failed",
    error="invalid memory_type: unknown",
)
```

写入图示：

```text
RememberRequest.summary
  ↓ Embedder.embed()
1024 维向量
  ├── INSERT memory_items
  └── INSERT vec_items
             ↓
      RememberResult(saved)
```

### 7. `forget()` 输入和输出

输入示例：

```python
request = ForgetRequest(
    ids=["active-id", "missing-id"],
    scope=MemoryScope(user_id=42),
)

result = await engine.forget(request)
```

输出逻辑上类似：

```python
ForgetResult(
    superseded_ids=["active-id"],
    missing_ids=["missing-id"],
    error="",
)
```

状态变化：

```text
memory_items

active-id:
active ─────────→ superseded

missing-id:
没有更新 ───────→ 放入 missing_ids
```

### 8. `fetch_messages()` 输入和输出

输入示例：

```python
request = FetchMessagesRequest(
    source_refs=["session:42:7#msg:8-9"],
    context=1,
    limit=10,
)

result = await engine.fetch_messages(request)
```

假设目标是第 8～9 条，并带出前后一条上下文，输出逻辑上类似：

```python
FetchMessagesResult(
    matched_count=2,
    messages=[
        {
            "role": "assistant",
            "content": "上一条上下文",
            "seq": 7,
            "source_ref": "session:42:7#msg:7",
            "in_source_ref": False,
        },
        {
            "role": "user",
            "content": "我喜欢喝拿铁",
            "seq": 8,
            "source_ref": "session:42:7#msg:8",
            "in_source_ref": True,
        },
        {
            "role": "assistant",
            "content": "好的，我记住了。",
            "seq": 9,
            "source_ref": "session:42:7#msg:9",
            "in_source_ref": True,
        },
        {
            "role": "user",
            "content": "下一条上下文",
            "seq": 10,
            "source_ref": "session:42:7#msg:10",
            "in_source_ref": False,
        },
    ],
    source_refs=["session:42:7#msg:8-9"],
    invalid_source_refs=[],
    error="",
)
```

无效引用输入：

```python
FetchMessagesRequest(source_refs=["not-a-session-ref"])
```

输出：

```python
FetchMessagesResult(
    matched_count=0,
    messages=[],
    source_refs=["not-a-session-ref"],
    invalid_source_refs=["not-a-session-ref"],
    error="invalid_source_ref: not-a-session-ref",
)
```

回源图示：

```text
MemoryItem.source_ref
  ↓ _parse_session_ref()
(user_id, chat_id, seq, seq_end)
  ↓ SessionStore.fetch_messages()
conversation_sessions.messages_json
  ↓
目标原文 + 前后文
```

### 9. `search_messages()` 输入和输出

输入示例：

```python
request = SearchMessagesRequest(
    query="打印机",
    scope=MemoryScope(user_id=42),
    role="user",
    limit=2,
    offset=0,
)

result = await engine.search_messages(request)
```

输出逻辑上类似：

```python
SearchMessagesResult(
    messages=[
        {
            "role": "user",
            "content": "我家打印机是 Brother HL-L2460DW",
            "seq": 12,
            "source_ref": "session:42:7#msg:12",
        },
        {
            "role": "user",
            "content": "打印机今天没有连接成功",
            "seq": 18,
            "source_ref": "session:42:7#msg:18",
        },
    ],
    matched_count=3,
    limit=2,
    offset=0,
    has_more=True,
    next_offset=2,
    error="",
)
```

下一页输入：

```python
SearchMessagesRequest(
    query="打印机",
    scope=MemoryScope(user_id=42),
    role="user",
    limit=2,
    offset=2,
)
```

### 10. `_search_memories()` 输入和输出

这是内部方法，上层通常不会直接调用。

输入示例：

```python
request = _MemorySearchRequest(
    query="我家打印机是什么型号？",
    user_id=42,
    top_k=5,
    memory_types=["profile"],
    include_superseded=False,
    aux_queries=["用户家打印机型号是 Brother HL-L2460DW"],
    keyword_limit=5,
)

result = await engine._search_memories(request)
```

输出逻辑上类似：

```python
_MemorySearchResult(
    items=[打印机型号记忆, 其他相关记忆],
    vector_items=[打印机型号记忆, 其他相关记忆],
    keyword_items=[打印机型号记忆],
    rrf_scores={
        "打印机型号记忆ID": 0.032522,
        "其他相关记忆ID": 0.016129,
    },
    lanes_by_id={
        "打印机型号记忆ID": ["vector", "keyword"],
        "其他相关记忆ID": ["vector"],
    },
    trace={
        "retrieval_mode": "hybrid_rrf",
        "fusion": "rrf",
        "query_count": 2,
        "vector_count": 2,
        "keyword_count": 1,
        "fused_count": 2,
        "vector_lane_counts": [1, 1],
        "vector_lane_new_counts": [1, 1],
        "keyword_limit": 5,
        "aux_queries": ["用户家打印机型号是 Brother HL-L2460DW"],
    },
)
```

内部数据流：

```text
_MemorySearchRequest
  ├── query
  └── aux_queries
        ↓ _dedupe_texts
     query_texts
        ↓ 每个文本分别 embed + vector_search
     vector_results

原始 query ── keyword_search ──→ keyword_results

vector_results + keyword_results
        ↓ _rrf_fuse_with_trace
_MemorySearchResult
```

### 11. `_build_aux_queries()` 输入和输出

输入示例：

```python
queries = await engine._build_aux_queries(
    "我家打印机是什么型号？"
)
```

如果 HyDE 成功生成假设，输出可能是：

```python
["用户家打印机型号是 Brother HL-L2460DW"]
```

如果 HyDE 没有生成有效内容，输出：

```python
[]
```

传入自定义 builder 时：

```python
async def custom_builder(query: str) -> list[str]:
    return [
        " 用户家打印机型号 ",
        "用户家打印机型号",
        "",
    ]
```

经过 `_dedupe_texts()` 后输出：

```python
["用户家打印机型号"]
```

---

## 二十五、阅读时需要记住的关键点

- `DefaultMemoryEngine` 是编排层，不是底层数据库类。
- 它把 `Embedder`、`MemoryStore` 和 `SessionStore` 组合成统一接口。
- `retrieve()` 用于每轮对话的被动记忆注入。
- `retrieve_explicit()` 通常用于 LLM 主动调用 `recall_memory`。
- 两种语义检索共用 `_search_memories()`，核心策略保持一致。
- 核心检索是“原查询 + HyDE + 多路向量检索 + 关键词检索 + RRF”。
- HyDE 生成的是检索假设，不是可信事实。
- `hyde_used=True` 表示辅助查询真正增加了新召回结果。
- RRF 使用排名而不是直接混合不兼容的原始分数。
- `grep` 模式当前实际是按时间和类型列出记忆，不是摘要文字 grep。
- `remember()` 写入 `memory_items` 和 `vec_items`。
- `forget()` 是软失效，不是物理删除。
- `fetch_messages()` 根据 Memory 的 `source_ref` 回到 Session 读取原文。
- `search_messages()` 搜索原始对话，`retrieve()` 搜索长期记忆。
- 方法通常把异常转成结果对象中的 `error`，减少记忆故障对主回复链的影响。
- 时间过滤使用 UTC 日期边界，需要注意与中国本地时间的差异。
