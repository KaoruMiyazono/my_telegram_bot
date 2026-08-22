---
title: Consolidation与Invalidation：长期记忆的生成和失效
created: 2026-08-22
updated: 2026-08-22
tags: [telegram-bot, memory, consolidation, invalidation, long-term-memory, pipeline]
description: 专题讲解 ConsolidationWorker 如何从旧 Session 窗口提取并写入长期记忆，以及 InvalidationWorker 如何识别用户纠正、召回冲突候选并把过时记忆标记为 superseded，包含完整数据流、函数输入输出、数据库变化、并发关系和当前局限。
source:
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/agent/pipeline/consolidation_worker.py # ConsolidationWorker 的提取逻辑
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/agent/pipeline/invalidation_worker.py # InvalidationWorker 的失效判断逻辑
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/agent/pipeline/passive_turn.py # 两个 Worker 的触发入口和后台调度
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/memory/store.py # 长期记忆写入、检索和状态更新
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/persistence/session_store.py # Session 原文和 consolidation 游标持久化
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/persistence/database.py # memory_items、vec_items 和 conversation_sessions 表结构
  - Cursor AI 对话，2026-08-22
---

# Consolidation与Invalidation：长期记忆的生成和失效

> Consolidation 负责从 Session 原文中“新增值得长期保留的记忆”，Invalidation 负责在用户明确纠正时“让已经过时的旧记忆退出正常召回”。

本文是 [[learning_post_turn_maintenance]] 中后台记忆维护部分的专题展开。

## 一、先建立最重要的整体认识

这个系统中，一条长期记忆大致经历：

```text
用户在 Session 中说出信息
          ↓
conversation_sessions 保存原文
          ↓
ConsolidationWorker 提取长期摘要
          ↓
memory_items.status = active
vec_items 保存摘要 embedding
          ↓
recall_memory 可以召回
          ↓
用户以后明确纠正旧信息
          ↓
InvalidationWorker 找到冲突旧记忆
          ↓
memory_items.status = superseded
          ↓
默认 recall_memory 不再召回旧记忆
```

两个 Worker 的分工：

| 维度 | ConsolidationWorker | InvalidationWorker |
|---|---|---|
| 核心动作 | 创建长期记忆 | 淘汰过时长期记忆 |
| 输入来源 | 一段较旧的 Session 原文 | 当前用户纠正 + 已有长期记忆 |
| 是否每轮检查 | 是，但满足窗口才启动 | 是，只要配置就启动后台检查 |
| LLM 用途 | 从对话提取结构化摘要 | 识别纠正主题，再判断冲突候选 |
| LLM 调用次数 | 每次 consolidation 1 次 | 1 次提主题 + 每个主题 1 次确认 |
| 主要写表 | `memory_items`、`vec_items` | `memory_items` |
| 主要状态变化 | 新建 `active` | `active → superseded` |
| 是否创建新正确记忆 | 是 | 否 |
| 是否删除原文 | 否 | 否 |

可以把它们理解成：

```text
Consolidation = 长期记忆的生产者
Invalidation  = 长期记忆的清退器
```

## 二、它们在 Pipeline 中的位置

一轮对话的五个主要阶段完成后，Pipeline 先保存 Session，再触发两个 Worker：

```python
get_session_store().save(...)
self._refresh_markdown_recent_turns(...)

self._maybe_consolidate(turn_ctx.session, inbound_message)
self._maybe_invalidate(inbound_message, result, turn_ctx.session)
```

顺序图：

```text
Telegram 回复已经发送
        ↓
追加本轮 user/assistant 消息
        ↓
conversation_sessions 保存原文
        ↓
刷新 RECENT_CONTEXT.md
        ↓
_maybe_consolidate()
        └─ 条件满足 → create_task(consolidate)
        ↓
_maybe_invalidate()
        └─ 已配置 → create_task(invalidation.run)
        ↓
execute() 返回 OutboundMessage
```

两个 Worker 都采用：

```python
asyncio.create_task(...)
```

因此它们属于 fire-and-forget 后台维护，不阻塞回复。

注意：调用顺序是先创建 consolidation 任务，再创建 invalidation 任务，但两个任务实际可能交错执行，不能理解为 consolidation 一定完整结束后才开始 invalidation。

## 三、ConsolidationWorker 解决什么问题

### 3.1 Session 原文不能直接当长期记忆

Session 可能包含大量内容：

```text
临时问题
寒暄
工具执行任务
一次性计划
稳定偏好
用户身份信息
长期操作规范
重要历史事件
```

如果把每句话都写进长期记忆：

- 长期记忆会迅速膨胀；
- 临时信息会污染检索；
- `recall_memory` 会返回大量无价值结果；
- Assistant 的建议可能被误当成用户事实。

所以 ConsolidationWorker 不是简单复制，而是让 LLM 按较高门槛做提取。

### 3.2 它想保存什么

提取 Prompt 的核心问题是：

```text
把这条信息放进6个月后的一次全新对话，它还有用吗？
```

当前实际解析四类输出：

| 类型 | 含义 | 例子 |
|---|---|---|
| `profile` | 用户客观身份、职业、技能、设备、状态 | 用户目前居住在上海 |
| `preference` | 明确喜好、厌恶、倾向 | 用户喜欢喝拿铁 |
| `procedure` | 希望长期遵守的流程或规则 | 部署前必须运行集成测试 |
| `event` | 重要事件、行为或状态变化 | 用户已经把项目迁移到 pnpm |

全局长期记忆类型里还有 `fact`，但当前 Consolidation Prompt 和解析代码不会生成 `fact`；`fact` 可以来自其他写入路径。

### 3.3 它明确排除什么

Prompt 要求不提取：

- Assistant 自己提出的建议；
- 用户没有明确表达、只能推测的信息；
- “你还记得吗”之类询问句；
- “今天、这次、当前”等临时信息；
- 一次性任务步骤；
- 外部 transcript 或示例里的第一人称陈述；
- 已经通过 `memorize` 显式保存的信息。

这是一套语义过滤，不是代码中的关键词硬判断，最终依赖 LLM 输出。

## 四、Consolidation何时触发

### 4.1 初始化参数

`main.py` 中：

```python
consolidation = ConsolidationWorker(
    keep_count=10,
    min_new_messages=6,
    markdown_store=memory_runtime.markdown.store,
)
```

含义：

```text
keep_count=10
→ 最近10条消息不做 consolidation，保留新鲜上下文

min_new_messages=6
→ total - last_consolidated 至少达到6
```

### 4.2 `should_consolidate()`

输入：

```python
session.messages
session.last_consolidated
```

计算：

```python
total = len(session.messages)
new_count = total - session.last_consolidated
consolidate_up_to = total - keep_count
```

必须满足：

```text
new_count > 0
total > keep_count
new_count >= min_new_messages
last_consolidated < consolidate_up_to
```

返回：

```python
True   # 可以启动
False  # 暂不启动
```

### 4.3 一轮对话为什么不会自动沉淀

正常一轮产生两条 Session 消息：

```text
user      1条
assistant 1条
total     2条
```

因为：

```python
2 <= keep_count  # keep_count=10
```

所以不会触发。

一般到第六轮：

```text
6轮 × 2条 = 12条
```

才第一次满足 `total > 10`。

此时只处理前两条：

```text
messages[0:2]  → 本次提取
messages[2:12] → 最近10条，暂时保留
```

### 4.4 当前 `min_new_messages` 计算的细节

代码使用：

```python
new_count = total - last_consolidated
```

这里包含了最后保留的 10 条消息。因此首次触发以后，只要出现新的可提取窗口，就可能再次触发，并不一定重新积累 6 条“窗口内的新消息”。

如果期望严格每积累 6 条待提取消息再运行，更接近这个含义的计算会是：

```text
(total - keep_count) - last_consolidated
```

当前项目没有这样实现。

## 五、Consolidation窗口怎么切

核心函数：

```python
def get_consolidation_window(session):
    total = len(session.messages)
    consolidate_up_to = total - keep_count
    return session.messages[
        session.last_consolidated : consolidate_up_to
    ]
```

例子：

```python
total = 30
last_consolidated = 10
keep_count = 10
```

窗口：

```python
session.messages[10:20]
```

图示：

```text
消息序号 0          10         20         30
         |-----------|----------|----------|
            已处理      本次提取     最近保留
                       10～19       20～29
```

`last_consolidated=10` 表示：

```text
messages[:10] 已处理
下一条待处理消息是 messages[10]
```

它是 Python 切片游标，不是“最后处理的消息序号为10”。

## 六、Consolidation执行链路

### 6.1 Pipeline防止进程内重复任务

```python
session_key = (user_id, chat_id)

if session_key in self._consolidation_inflight:
    return

self._consolidation_inflight.add(session_key)
```

同一个 Pipeline 实例中，同一个 `(user_id, chat_id)` 同时只运行一个 consolidation。

任务结束时：

```python
finally:
    self._consolidation_inflight.discard(session_key)
```

这只是 Python 进程内集合，不是数据库锁。多进程或多副本部署时不能阻止不同进程重复处理同一 Session。

### 6.2 固定任务启动时的窗口终点

`consolidate()` 一开始记录：

```python
total_at_start = len(session.messages)
consolidate_up_to = total_at_start - keep_count
window_start = session.last_consolidated
window = session.messages[window_start:consolidate_up_to]
```

即使 LLM 调用期间 `session.messages` 又追加了新消息，任务结束后也只把游标推进到本次启动时计算的 `consolidate_up_to`，不会跳过后来追加的消息。

### 6.3 将窗口拼成 LLM 输入

```python
conversation = "\n".join(
    f"[{m['role']}]: {m['content']}"
    for m in window
)
```

输入示例：

```text
[user]: 我现在住在上海。
[assistant]: 上海生活怎么样？
[user]: 我平时主要写 Rust。
[assistant]: Rust 很适合系统开发。
```

发送给 LLM 时最多使用：

```python
conversation[:4000]
```

这是字符截断，不是 token 精确截断。

### 6.4 LLM输出示例

LLM 被要求只返回 JSON：

```json
{
  "profile": [
    {"summary": "用户目前居住在上海。"},
    {"summary": "用户主要使用 Rust 编程。"}
  ],
  "preference": [],
  "procedure": [],
  "event": []
}
```

`_llm_extract()` 将其统一转换成：

```python
[
    {
        "summary": "用户目前居住在上海。",
        "memory_type": "profile",
    },
    {
        "summary": "用户主要使用 Rust 编程。",
        "memory_type": "profile",
    },
]
```

函数输入输出：

```python
summaries = await worker._llm_extract(conversation)
```

```text
输入：对话文本字符串
输出：list[dict[str, str]]
失败、非法JSON或无可提取内容：[]
```

### 6.5 建立可回源地址

如果窗口为：

```python
messages[10:20]
```

实际消息序号是 10～19，因此生成：

```text
session:42:9001#msg:10-19
```

以后 `recall_memory` 返回这条长期记忆时，Agent 可以继续调用 `fetch_messages` 读取对应原文。

### 6.6 写入 Markdown镜像

`_shadow_write_markdown()` 会尝试写入：

```text
HISTORY.md
PENDING.md
memory/journal/YYYY-MM-DD.md
```

它们带有基于 `source_ref` 的去重标记，防止同一窗口重复写入相同类型内容。

Markdown 写入失败只记录异常，不阻止后续 SQLite 长期记忆写入。

### 6.7 写入长期记忆数据库

每条摘要调用：

```python
await store.upsert_item(
    memory_type=s["memory_type"],
    summary=s["summary"],
    user_id=user_id,
    source_ref=source_ref,
)
```

内部步骤：

```text
summary
  ↓ Embedder.embed()
embedding
  ↓
生成 UUID
  ├─ INSERT memory_items
  └─ INSERT vec_items
```

假设：

```text
id = mem-123
summary = 用户目前居住在上海。
```

数据库变化：

```text
memory_items
id=mem-123
user_id=42
memory_type=profile
summary=用户目前居住在上海。
status=active
source_ref=session:42:9001#msg:10-19

vec_items
embedding_id=mem-123
embedding=[0.012, -0.073, ...]
```

### 6.8 推进并再次保存游标

提取完成后：

```python
session.last_consolidated = consolidate_up_to
```

后台 `_run()` 随后再次调用：

```python
get_session_store().save(
    user_id,
    chat_id,
    session.messages,
    last_consolidated=session.last_consolidated,
)
```

这样程序重启后，`BeforeTurnPhase` 可以恢复处理进度。

### 6.9 返回值

```python
written = await worker.consolidate(...)
```

返回成功写入的长期记忆数量：

```python
written == 2
```

如果没有提取到内容：

```python
written == 0
```

## 七、Consolidation的重要失败语义

### 7.1 没提取到内容仍推进游标

```python
if not summaries:
    session.last_consolidated = consolidate_up_to
    return 0
```

好处是无价值窗口不会被反复交给 LLM。

但 `_llm_extract()` 在以下情况也返回空列表：

```text
API调用失败
返回非法JSON
返回结构不正确
```

因此临时 API 故障也可能让游标推进，该窗口不会自动重试。

### 7.2 部分写入失败仍推进游标

每条 `upsert_item()` 单独捕获异常。假设 LLM 提取三条，只有两条写入成功：

```python
written == 2
session.last_consolidated = consolidate_up_to
```

失败的第三条不会因为游标推进而自动重试。

### 7.3 Markdown与SQLite不是一个事务

当前顺序是：

```text
先写 Markdown 镜像
再逐条写 SQLite 长期记忆
```

两者不在同一数据库事务中，可能出现 Markdown 已记录、SQLite 写入失败的短暂或永久不一致。

## 八、InvalidationWorker解决什么问题

### 8.1 它不是通用事实核查器

Invalidation 不会上网验证用户说的事实，也不判断某条记忆是否“客观真实”。

它识别的是：

> 用户本轮是否明确纠正、否定、废弃或更新了与自己或 Agent 操作规范有关的旧记忆。

例如：

```text
旧记忆：用户住在北京。
用户：你记错了，我已经搬到上海了。
```

旧记忆已经过时，应退出默认召回。

### 8.2 它不会创建新正确记忆

Invalidation 只负责：

```text
用户住在北京：active → superseded
```

它不会负责新增：

```text
用户住在上海：active
```

新值要由：

```text
本轮 memorize 工具
或以后 ConsolidationWorker
```

写入。

这意味着一次更新可能呈现：

```text
先淘汰旧值
→ 稍后才通过 consolidation 沉淀新值
```

如果用户明确说“记住”，触发 `memorize`，新值可以在本轮立即写入。

## 九、Invalidation的完整识别链路

`run()` 的总体流程：

```text
本轮 tool_calls
    ↓
保护本轮 memorize 新建的记忆 ID
    ↓
第一次 LLM：判断是否在纠正旧记忆，并提取主题
    ↓
没有主题 → 返回 []，数据库不变
    ↓
每个主题进行向量检索 + 关键词检索
    ↓
过滤候选，最多保留5条
    ↓
第二次 LLM：判断哪些候选确实冲突或过时
    ↓
再次校验 ID 和保护集合
    ↓
mark_superseded_batch()
    ↓
返回实际更新的旧记忆 ID
```

## 十、第一层保护：本轮memorize不能被立即淘汰

### 10.1 `_collect_protected_ids()`

本轮可能刚执行：

```json
{
  "function": {"name": "memorize"},
  "result": "{\"status\":\"saved\",\"item_id\":\"new-tea-id\"}"
}
```

Worker 解析出：

```python
protected_ids = {"new-tea-id"}
```

如果结果不是合法 JSON，还会尝试从：

```text
item_id=new-tea-id
```

这种文本格式中用正则提取。

### 10.2 为什么需要保护

用户说：

```text
不对，我现在喜欢茶，不喜欢咖啡了。
```

本轮 `memorize` 可能创建：

```text
new-tea-id：用户喜欢茶
```

后面搜索“用户饮品偏好”时，新旧记忆都可能被找到。保护集合防止 LLM 把刚写的新值一起判为失效。

## 十一、第一次LLM：识别纠正主题

### 11.1 `_extract_invalidation_topics()`

输入：

```python
user_msg = "不对，我已经不住北京了，现在住上海。"
token_budget = 1000
```

输出：

```python
topics = ["用户居住地"]
remaining_budget = 872
```

它只负责提取检索主题，不直接选择数据库 ID。

### 11.2 必须同时满足的条件

Prompt 要求：

1. 用户有明确否定、纠错、废弃或更新意图；
2. 新说法会替代一条关于用户或 Agent 流程的旧记忆。

常见触发表达：

```text
不对、不是、错了、记错了、其实
改成、改为、换成、迁到、更新一下
以后、忘掉、不要再、过时、删除
```

但这不是简单的字符串 `if`，最终由 LLM 结合语义判断。

### 11.3 应触发与不应触发的例子

| 用户消息 | 预期主题 |
|---|---|
| 不对，我喜欢茶 | `用户的饮品偏好` |
| 我不住北京了，现在在上海 | `用户居住地` |
| 流程更新，以后提交前跑 pnpm test | `仓库提交前测试流程` |
| 我今天喝了茶 | `[]` |
| 我以前是不是喜欢咖啡 | `[]` |
| 我可能以后搬去上海 | `[]` |
| 小王不是住在北京 | 通常为 `[]`，除非与当前记忆有关 |

没有主题时：

```python
if not topics:
    return []
```

绝大多数普通对话到这里就结束。

## 十二、召回候选旧记忆

### 12.1 为什么不能让LLM凭空决定ID

第一次 LLM 只看到用户本轮消息，不知道数据库中有哪些记忆。因此先得到主题，再由程序查询候选。

### 12.2 向量通道

```python
query_vec = await embedder.embed(topic)

vec_results = await store.vector_search(
    query_vec=query_vec,
    user_id=user_id,
    top_k=5,
    memory_types=STRUCTURED_MEMORY_TYPES,
)
```

优势是可以找到语义相近但措辞不同的旧记忆：

```text
主题：用户的饮品偏好
旧记忆：用户每天早晨喜欢喝拿铁
```

### 12.3 关键词通道

```python
keyword_results = await store.keyword_search(
    terms=topic,
    user_id=user_id,
    limit=5,
)
```

它在 `memory_items.summary` 上执行 SQL `LIKE`。

### 12.4 合并方式

```python
for mem in [*vec_results, *keyword_results]:
```

这里不是 `recall_memory` 使用的 RRF 融合，而是：

```text
先放向量结果
再用关键词结果补充
去重
最多5条
```

## 十三、候选过滤

召回结果需要经过：

```text
重复 ID                        → 排除
本轮 memorize 产生的 protected ID → 排除
source_ref 与本轮完全相同       → 排除
不属于结构化长期记忆类型         → 排除
超过5条                        → 截断
```

支持的类型：

```python
[
    "procedure",
    "preference",
    "profile",
    "event",
    "fact",
]
```

### 13.1 为什么排除当前消息窗口

本轮消息地址可能是：

```text
session:42:9001#msg:18-19
```

如果候选记忆也恰好来自这个地址，说明它很可能是本轮刚产生的内容，不应立刻作为“旧记忆”淘汰。

当前 `_same_exact_message_window()` 只判断两个字符串完全相等，不判断消息范围是否部分重叠。

## 十四、第二次LLM：确认哪些候选真的冲突

### 14.1 `_check_invalidate()`

假设候选是：

```text
id=memory-1 | profile | 用户居住在北京。
id=memory-2 | event   | 用户曾经在北京工作。
```

用户原话：

```text
你记错了，我已经从北京搬到上海了。
```

第二次 LLM 应理解：

```text
“居住在北京”已过时
“曾经在北京工作”不一定错误
```

返回：

```json
["memory-1"]
```

### 14.2 为什么不能只看语义相似度

```text
语义相关 ≠ 内容冲突 ≠ 应该失效
```

向量检索只能找到相关候选，第二次 LLM 才负责判断逻辑上的替代、矛盾或过时。

### 14.3 防止LLM伪造ID

```python
valid_ids = {c["id"] for c in candidates}

return [
    item_id
    for item_id in result
    if item_id in valid_ids
]
```

模型只能选择程序提供过的候选 ID。凭空生成的 ID 会被过滤。

## 十五、更新数据库状态

最终调用：

```python
updated = store.mark_superseded_batch(
    supersede_ids,
    user_id=user_id,
)
```

它先确认：

```text
ID真实存在
属于当前user_id
当前status仍是active
```

然后执行：

```sql
UPDATE memory_items
SET status = 'superseded',
    updated_at = CURRENT_TIMESTAMP
WHERE id IN (...)
  AND user_id = ?
```

数据库变化：

```text
更新前
id=memory-1
summary=用户居住在北京。
status=active

更新后
id=memory-1
summary=用户居住在北京。
status=superseded
```

它不会：

- 删除 `memory_items` 记录；
- 删除对应 `vec_items` 向量；
- 创建新的正确记忆；
- 写入 `memory_replacements`。

虽然旧向量还存在，但普通向量检索会 JOIN `memory_items` 并默认过滤 `status='active'`，因此旧记忆不会正常召回。

## 十六、Invalidation函数输入输出例子

输入：

```python
superseded_ids = await worker.run(
    user_msg="不对，我不喜欢咖啡了，现在喜欢茶。",
    agent_response="好的，我更新一下。",
    tool_calls=[],
    user_id=42,
    chat_id=9001,
    source_ref="session:42:9001#msg:18-19",
)
```

数据库原有：

```text
id=coffee-old
type=preference
summary=用户喜欢喝咖啡。
status=active
```

可能输出：

```python
superseded_ids == ["coffee-old"]
```

副作用：

```text
coffee-old.status = superseded
```

如果用户只是说：

```text
我今天喝了一杯茶。
```

则可能输出：

```python
superseded_ids == []
```

数据库不变。

## 十七、Invalidation的Token预算

配置：

```python
TOKEN_BUDGET_PER_RUN = 1000
TOKENS_EXTRACT_INVALIDATION = 128
TOKENS_CHECK_INVALIDATE = 160
```

预算变化：

```text
初始                         1000
第一次LLM提取主题             -128
检查第1个主题                 -160
检查第2个主题                 -160
检查第3个主题                 -160
最多处理3个主题后剩余          392
```

这只是代码预先扣减的预算，不是读取 API 返回的实际 token usage。

## 十八、同一个更新案例如何经过两个Worker

假设数据库已有：

```text
memory-old
summary=用户居住在北京。
status=active
```

用户本轮说：

```text
你记错了，我已经搬到上海了。
```

### 18.1 本轮Session先保存原文

```text
conversation_sessions.messages_json
→ 新增用户纠正和 Agent 回复
```

### 18.2 Invalidation立即检查当前纠正

```text
提取主题：用户居住地
→ 找到 memory-old
→ 二次LLM确认冲突
→ memory-old.status = superseded
```

### 18.3 Consolidation不会立刻提取当前轮

因为它保留最近 10 条消息，当前纠正通常仍位于保留窗口：

```text
最近10条 → 暂不 consolidation
```

### 18.4 新值如何进入长期记忆

有两种路径：

```text
路径A：模型本轮调用 memorize
→ “用户目前居住在上海”立即写入 active

路径B：没有调用 memorize
→ 等当前轮以后变成较旧窗口
→ Consolidation 提取“用户目前居住在上海”
→ 写入 active
```

因此没有 `memorize` 时，可能短暂出现：

```text
旧北京记忆已经 superseded
新上海记忆还未 consolidation
```

不过当前原话已经保存在 Session 中，可以通过 Session 历史或 `search_messages/fetch_messages` 找到。

## 十九、两者会不会打架

通常职责互补：

```text
Consolidation处理较旧窗口
Invalidation处理当前明确纠正
```

`keep_count=10` 将最近消息排除在 consolidation 窗口之外，也减少了两个 Worker 同时处理当前新信息的机会。

但它们是独立后台任务，没有统一事务：

- 可能交错运行；
- 一个成功不代表另一个成功；
- Invalidation 不知道 Consolidation 是否会创建新值；
- Consolidation 不直接负责替代旧值；
- 数据库写入和 Markdown 镜像不是一个事务。

所以这是“最终逐步一致”的维护方式，不是一次原子更新。

## 二十、涉及的数据库表

### 20.1 `conversation_sessions`

| 字段 | 作用 |
|---|---|
| `user_id` | 用户 ID |
| `chat_id` | Session ID |
| `messages_json` | user/assistant 原始消息列表 |
| `last_consolidated` | 已处理窗口的下一个消息下标 |
| `created_at` | Session 创建时间 |
| `updated_at` | 最近保存时间 |

Consolidation 从这里对应的内存 Session 读取原文，并在结束后更新游标。

### 20.2 `memory_items`

| 字段 | Consolidation | Invalidation |
|---|---|---|
| `id` | 新建 UUID | 按候选 ID 定位 |
| `user_id` | 写入所属用户 | 限定只能更新当前用户 |
| `memory_type` | 写入类型 | 过滤结构化类型 |
| `summary` | 写入 LLM 摘要 | 作为冲突判断候选 |
| `status` | 新建为 `active` | 改为 `superseded` |
| `source_ref` | 写入原文窗口地址 | 排除当前完全相同窗口 |
| `updated_at` | 新建时间 | 状态更新时刷新 |

### 20.3 `vec_items`

| 字段 | 作用 |
|---|---|
| `embedding_id` | 对应 `memory_items.id` |
| `embedding` | 长期摘要的向量 |

Consolidation 新建 embedding；Invalidation 使用它召回语义相关候选，但不会删除它。

### 20.4 `memory_replacements`

项目有这张替换关系表，但 Invalidation 当前使用 `mark_superseded_batch()`，不会写它。

因此只能知道旧记忆已失效，不能从这张表得知它具体被哪条新记忆替代。

## 二十一、当前实现的局限

### 21.1 Consolidation局限

- 一轮对话不会立即自动沉淀，存在长期记忆冷启动；
- LLM 提取失败也可能推进游标；
- 单条写入失败不会自动重试；
- 最近窗口按消息数而不是轮数计算；
- 对话输入按前 4000 个字符截断；
- 只自动解析四类记忆，不生成 `fact`；
- 进程内 inflight 集合不能解决多进程重复任务；
- Markdown 与 SQLite 可能不一致。

### 21.2 Invalidation局限

- 依赖 LLM，可能误判或漏判；
- 只对明确纠正比较敏感，隐式变化可能识别不到；
- 不创建新正确记忆；
- 不记录新旧替换关系；
- 向量和关键词候选没有做 RRF；
- 关键词检索使用整个 topic 做 `LIKE`，措辞不同可能查不到；
- 只排除完全相同的 `source_ref`，不判断范围部分重叠；
- 后台失败只记录日志，不自动重试；
- `agent_response` 参数当前没有参与判断。

## 二十二、阅读代码时抓住这五个核心函数

### Consolidation

```text
should_consolidate(session)
→ 判断是否满足窗口条件

get_consolidation_window(session)
→ 查看本次处理哪些原始消息

consolidate(session, store, user_id, chat_id)
→ 提取、写入、推进游标
```

### Invalidation

```text
_extract_invalidation_topics(user_msg, budget)
→ 第一次LLM：判断用户纠正了什么

_retrieve_candidates(topic, ...)
→ 从长期记忆中找相关旧记忆

_check_invalidate(topic, user_msg, candidates, budget)
→ 第二次LLM：判断哪些候选真的过时

run(...)
→ 串联保护、提主题、检索、确认和更新
```

## 二十三、最终总结

```text
ConsolidationWorker
输入：较旧的 Session 原文窗口
判断：哪些内容半年后仍值得记住
输出：新的 active 长期记忆 + embedding

InvalidationWorker
输入：当前用户纠正 + 已有长期记忆候选
判断：哪些旧记忆确实被推翻或已经过时
输出：旧记忆 ID，并把 status 改为 superseded
```

两者共同形成长期记忆生命周期：

```text
Session原文
    ↓ Consolidation
active长期记忆
    ↓ recall_memory
被Agent召回使用
    ↓ 用户明确纠正
Invalidation
    ↓
superseded旧记忆
```

最需要记住的边界：

> Consolidation 负责“写新”，Invalidation 负责“退旧”；Invalidation 不会替 Consolidation 创建新的正确记忆。
