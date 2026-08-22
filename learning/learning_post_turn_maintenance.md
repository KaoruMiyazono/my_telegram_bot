---
title: 回合完成后的持久化与记忆维护
created: 2026-08-22
updated: 2026-08-22
tags: [telegram-bot, pipeline, session, consolidation, invalidation, markdown-memory]
description: 逐步讲解 PassiveTurnPipeline 在五个核心阶段完成后，如何追加并保存 Session 原始消息、刷新 RECENT_CONTEXT.md、异步提取长期记忆，以及检测并淘汰被用户纠正的旧记忆。
source:
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/agent/pipeline/passive_turn.py # 本文主要讲解的回合收尾代码
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/persistence/session_store.py # Session 原文与 consolidation 游标的 SQLite 持久化
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/memory/markdown_store.py # RECENT_CONTEXT.md 与 consolidation 的 Markdown 镜像
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/agent/pipeline/consolidation_worker.py # 长期记忆窗口提取
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/agent/pipeline/invalidation_worker.py # 旧长期记忆失效检测
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/memory/store.py # 长期记忆写入和 superseded 更新
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/persistence/database.py # 涉及的数据库表结构
  - Cursor AI 对话，2026-08-22
---

# 回合完成后的持久化与记忆维护

> 这部分发生在 BeforeTurn、BeforeReasoning、Reasoner、AfterReasoning、AfterTurn 五个核心阶段之后，负责把本轮对话落盘，并启动不阻塞回复的长期记忆维护任务。

## 一、先看整体位置

本节分析的是 `PassiveTurnPipeline.execute()` 的最后一部分：

```python
# Update session with new messages
turn_ctx.session.messages.append({
    "role": "user",
    "content": inbound_message.content,
})
turn_ctx.session.messages.append({
    "role": "assistant",
    "content": result.content,
})

# Persist session
from persistence.session_store import get_session_store
get_session_store().save(
    inbound_message.user_id,
    inbound_message.chat_id,
    turn_ctx.session.messages,
    last_consolidated=turn_ctx.session.last_consolidated,
)
self._refresh_markdown_recent_turns(
    turn_ctx.session,
    inbound_message.user_id,
)

self._maybe_consolidate(turn_ctx.session, inbound_message)
self._maybe_invalidate(inbound_message, result, turn_ctx.session)
```

它可以拆成四条链：

```text
本轮已经生成并发送回复
          │
          ├─ ① 把 user/assistant 原文追加到内存 Session
          │
          ├─ ② 把完整 Session 保存到 conversation_sessions
          │
          ├─ ③ 把最近 10 条消息刷新到 RECENT_CONTEXT.md
          │
          ├─ ④ 满足窗口条件时，后台提取长期记忆
          │
          └─ ⑤ 每轮后台检查：用户是否纠正了旧记忆
```

其中 ①～③ 是本轮同步执行；④、⑤ 内部使用 `asyncio.create_task()`，属于后台任务。

## 二、重要的执行顺序

完整顺序不是“先保存再发送”，而是：

```text
Reasoner 生成答案
    ↓
AfterReasoning 加工答案
    ↓
AfterTurn 记录 turn_committed 并发送 Telegram 消息
    ↓
追加 user/assistant 到 Session
    ↓
SessionStore.save() 保存 SQLite
    ↓
刷新 RECENT_CONTEXT.md
    ↓
尝试启动 consolidation
    ↓
启动 invalidation
```

这意味着：

- 用户收到 Telegram 回复后，程序才保存本轮 Session。
- 如果 Telegram 发送抛出异常，`execute()` 会在 `AfterTurn` 停止，后面的 Session 保存和记忆维护不会执行。
- 如果 Telegram 已发送成功，但进程恰好在 Session 保存前崩溃，用户可能已经看到回复，但数据库没有这一轮记录。

这是当前代码的真实失败边界。

## 三、第一步：追加本轮原始消息

### 3.1 代码作用

```python
turn_ctx.session.messages.append({
    "role": "user",
    "content": inbound_message.content,
})

turn_ctx.session.messages.append({
    "role": "assistant",
    "content": result.content,
})
```

`turn_ctx.session.messages` 是当前会话在内存中的原始消息列表。

这两次 `append()` 把本轮的：

1. 用户输入；
2. Agent 最终回答；

按照对话顺序追加到列表末尾。

### 3.2 输入输出例子

追加之前：

```python
turn_ctx.session.messages == [
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": "你好，有什么可以帮你？"},
]

inbound_message.content == "我喜欢喝拿铁"
result.content == "好的，我记住了。"
```

追加之后：

```python
turn_ctx.session.messages == [
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": "你好，有什么可以帮你？"},
    {"role": "user", "content": "我喜欢喝拿铁"},
    {"role": "assistant", "content": "好的，我记住了。"},
]
```

### 3.3 这是原文，不是摘要

这里保存的是：

```text
inbound_message.content → 用户原始输入
result.content          → Agent 最终回复
```

没有在此处调用 LLM 压缩，也没有转换为 `MemoryItem`。

因此它属于 Session 原文，不属于长期记忆摘要。

### 3.4 为什么工具消息没有追加到 Session

Reasoner 内部的 `messages` 还可能包含：

```text
assistant tool_calls
tool 执行结果
Guard 自动工具结果
```

但这里最终只保存 user 和 assistant 两条可见消息。

所以 `conversation_sessions` 保存的是用户可见对话原文，不是 Reasoner 的完整内部推理轨迹和工具调用轨迹。

## 四、第二步：保存到 SessionStore

### 4.1 调用代码

```python
get_session_store().save(
    inbound_message.user_id,
    inbound_message.chat_id,
    turn_ctx.session.messages,
    last_consolidated=turn_ctx.session.last_consolidated,
)
```

输入可以理解为：

```python
user_id = 42
chat_id = 9001
messages = [
    {"role": "user", "content": "我喜欢喝拿铁"},
    {"role": "assistant", "content": "好的，我记住了。"},
]
last_consolidated = 0
```

`save()` 没有返回值：

```python
None
```

它的输出效果是更新数据库。

### 4.2 `get_session_store()` 的作用

`get_session_store()` 返回同一进程共享的 `SessionStore` 实例：

```python
_session_store: SessionStore | None = None

def get_session_store() -> SessionStore:
    global _session_store
    if _session_store is None:
        _session_store = SessionStore()
    return _session_store
```

不过 `SessionStore` 本身没有保存内存缓存；真正的数据仍写入 SQLite。

### 4.3 数据库表结构

这里操作的是 `conversation_sessions`：

```sql
CREATE TABLE IF NOT EXISTS conversation_sessions (
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    messages_json TEXT NOT NULL DEFAULT '[]',
    last_consolidated INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, chat_id)
);
```

字段含义：

| 字段 | 作用 | 示例 |
|---|---|---|
| `user_id` | Telegram 用户 ID | `42` |
| `chat_id` | Telegram 会话 ID | `9001` |
| `messages_json` | 当前 Session 的全部原始消息 JSON | `[{"role":"user",...}]` |
| `last_consolidated` | 已经完成长期记忆提取的消息游标 | `12` |
| `created_at` | 这条 Session 记录首次创建时间 | `2026-08-22 10:00:00` |
| `updated_at` | 最近一次保存时间 | `2026-08-22 10:10:00` |

联合主键是：

```text
(user_id, chat_id)
```

所以同一个用户可以在不同 Telegram Chat 中拥有不同 Session。

### 4.4 `save()` 如何实现 UPSERT

核心 SQL：

```sql
INSERT INTO conversation_sessions
    (user_id, chat_id, messages_json, last_consolidated, updated_at)
VALUES (?, ?, ?, COALESCE(?, 0), CURRENT_TIMESTAMP)
ON CONFLICT(user_id, chat_id) DO UPDATE SET
    messages_json = excluded.messages_json,
    last_consolidated = COALESCE(
        ?,
        conversation_sessions.last_consolidated
    ),
    updated_at = CURRENT_TIMESTAMP
```

逻辑是：

```text
数据库里没有 (user_id, chat_id)
→ INSERT 新 Session

数据库里已经存在
→ UPDATE messages_json、last_consolidated、updated_at
```

这里不是只插入本轮两条消息，而是将整个 `session.messages` 序列化后覆盖 `messages_json`。

### 4.5 `last_consolidated` 到底是什么

`last_consolidated` 不是时间，也不是消息 ID，而是消息列表中的游标。

例如：

```python
len(session.messages) == 30
session.last_consolidated == 10
```

含义是：

```text
messages[0:10] 已经处理过
messages[10:] 还没有全部完成 consolidation
```

它必须和 `messages_json` 一起保存，否则程序重启后会重复提取旧窗口。

## 五、第三步：刷新 Markdown 最近上下文

### 5.1 调用入口

```python
self._refresh_markdown_recent_turns(
    turn_ctx.session,
    inbound_message.user_id,
)
```

实现：

```python
def _refresh_markdown_recent_turns(self, session, user_id: int) -> None:
    runtime = self._memory_runtime
    markdown = getattr(runtime, "markdown", None)
    store = getattr(markdown, "store", None)
    if store is None:
        return

    try:
        store.write_recent_turns(
            user_id=user_id,
            messages=session.messages,
        )
    except Exception:
        logger.exception(...)
```

### 5.2 为什么连续使用 `getattr`

这段代码允许 Markdown 记忆层不存在：

```text
memory_runtime 为空
→ markdown 为空
→ store 为空
→ 直接 return
```

因此 Markdown 层是可选能力。即使没有它，SQLite Session 仍能正常保存。

### 5.3 写入哪个文件

最终调用：

```python
MarkdownMemoryStore.write_recent_turns()
```

它更新当前用户目录下的：

```text
RECENT_CONTEXT.md
```

这里只替换 `## Recent Turns` 小节，不会覆盖文件中的 `Compression` 和 `Ongoing Threads`。

### 5.4 保存最近 10 条消息，不是最近 10 轮

默认参数：

```python
keep_count = 10
messages[-10:]
```

因为正常一轮包含：

```text
1 条 user 消息 + 1 条 assistant 消息
```

所以最近 10 条消息通常约等于最近 5 轮对话。

### 5.5 用户原文和 Assistant 预览的差别

格式化逻辑：

```python
if role == "assistant":
    lines.append(f"[a-preview] {content[:80]}")
else:
    lines.append(f"[user] {content}")
```

因此：

- 用户消息保存完整内容；
- Assistant 消息只保留前 80 个字符；
- 工具消息和其他 role 会被忽略。

输入：

```python
messages = [
    {"role": "user", "content": "帮我解释 EventBus"},
    {"role": "assistant", "content": "EventBus 是一个事件分发器……"},
]
```

写入结果：

```markdown
## Recent Turns
<!-- a-preview = assistant reply preview only -->
[user] 帮我解释 EventBus
[a-preview] EventBus 是一个事件分发器……
```

### 5.6 错误处理

如果 Markdown 文件写入失败，代码只记录异常，不继续向外抛出。

所以：

```text
SQLite Session 已保存
Markdown 刷新失败
→ 本轮 execute() 仍然继续
→ consolidation / invalidation 仍会被尝试启动
```

注意：`write_recent_turns()` 是同步文件操作；它直接运行在 `execute()` 中，不是后台任务。

## 六、第四步：`_maybe_consolidate()` 提取长期记忆

### 6.1 Consolidation 是什么

Consolidation 可以理解为：

> 从积累的 Session 原始对话中，提取少量半年后仍可能有用的长期事实。

例如原始对话：

```text
[user] 我搬到上海了。
[assistant] 好的，上海生活怎么样？
[user] 我现在主要写 Rust。
```

可能提取成：

```json
[
  {
    "memory_type": "profile",
    "summary": "用户目前居住在上海。"
  },
  {
    "memory_type": "profile",
    "summary": "用户目前主要使用 Rust 编程。"
  }
]
```

原始消息保留在 `conversation_sessions`，摘要写入长期记忆表。

### 6.2 入口检查

```python
if self._consolidation is None or self._store is None:
    return

if not self._consolidation.should_consolidate(session):
    return
```

只有同时注入了：

```text
ConsolidationWorker
MemoryStore
```

并满足窗口条件，才会继续。

### 6.3 当前配置

在 `main.py` 中：

```python
consolidation = ConsolidationWorker(
    keep_count=10,
    min_new_messages=6,
    markdown_store=memory_runtime.markdown.store,
)
```

参数含义：

| 参数 | 作用 |
|---|---|
| `keep_count=10` | 最近 10 条消息暂时留在窗口外，不进行提取 |
| `min_new_messages=6` | `total - last_consolidated` 至少达到 6 才允许触发 |
| `markdown_store` | 把提取结果同时镜像写入 Markdown 文件 |

### 6.4 `should_consolidate()` 的条件

```python
total = len(session.messages)
new_count = total - session.last_consolidated
consolidate_up_to = total - keep_count
```

必须全部满足：

```text
new_count > 0
total > keep_count
new_count >= min_new_messages
last_consolidated < consolidate_up_to
```

### 6.5 窗口怎么切

真正提取的窗口：

```python
session.messages[
    session.last_consolidated : len(session.messages) - keep_count
]
```

例如：

```python
total = 30
last_consolidated = 10
keep_count = 10
```

那么：

```text
已处理区域        本次提取区域       暂时保留区域
messages[0:10]   messages[10:20]   messages[20:30]
      10条             10条              10条
```

图示：

```text
0         10         20         30
|----------|----------|----------|
  已处理      本次提取      最近上下文
              window       keep_count
```

保留最近 10 条的原因是：最近对话可能还在继续，过早提取容易失去语境。

### 6.6 防止同一 Session 重复启动

```python
session_key = (user_id, chat_id)

if session_key in self._consolidation_inflight:
    return

self._consolidation_inflight.add(session_key)
```

同一个 Pipeline 实例中，如果当前 Session 已经有 consolidation 在运行，新一轮不会再启动一个相同任务。

任务结束后：

```python
finally:
    self._consolidation_inflight.discard(session_key)
```

注意，这只是当前 Python 进程内的保护，不是数据库分布式锁。部署多个 Bot 进程时，它不能阻止不同进程同时处理同一个 Session。

### 6.7 为什么它不阻塞用户

```python
asyncio.create_task(_run())
```

`_maybe_consolidate()` 不等待 LLM 提取完成，而是把 `_run()` 注册到事件循环，随后立即返回。

```text
主流程                         后台任务
  │                              │
  ├─ create_task(_run) ─────────→│ 调用 consolidation LLM
  │                              │ 写长期记忆
  └─ return outbound_message     │ 更新游标并再次保存 Session
```

### 6.8 `consolidate()` 的完整步骤

```text
记录任务启动时的消息总数 total_at_start
          ↓
截取待提取窗口
          ↓
拼成 [role]: content 文本
          ↓
调用 LLM，要求返回结构化 JSON
          ↓
解析 profile / preference / procedure / event
          ↓
生成可回源的 source_ref
          ↓
镜像写入 Markdown
          ↓
逐条调用 MemoryStore.upsert_item()
          ↓
推进 session.last_consolidated
          ↓
再次 SessionStore.save() 保存新游标
```

### 6.9 `consolidate()` 输入输出例子

输入：

```python
written = await worker.consolidate(
    session=session,
    store=memory_store,
    user_id=42,
    chat_id=9001,
)
```

假设 LLM 提取出两条长期记忆，输出：

```python
written == 2
```

同时产生副作用：

```text
memory_items 新增 2 行
vec_items 新增 2 个向量
Markdown 历史/待处理/日志文件追加内容
session.last_consolidated 向前推进
conversation_sessions.last_consolidated 再次保存
```

### 6.10 `source_ref` 如何生成

如果本次处理 `messages[10:20]`，包含序号 10～19：

```python
source_ref = "session:42:9001#msg:10-19"
```

长期记忆以后通过 `fetch_messages` 可以回到这一段原始对话。

### 6.11 长期记忆写入哪些表

`MemoryStore.upsert_item()` 同时操作两张表：

```text
长期记忆摘要
   ├─ memory_items：结构化内容、状态、来源
     └─ vec_items：用于语义检索的 embedding
```

关键字段：

| 表 | 字段 | 作用 |
|---|---|---|
| `memory_items` | `id` | 长期记忆 UUID |
| `memory_items` | `user_id` | 记忆所属用户 |
| `memory_items` | `memory_type` | `profile/preference/procedure/event` 等类型 |
| `memory_items` | `summary` | LLM 提取的长期记忆摘要 |
| `memory_items` | `status` | 新写入时为 `active` |
| `memory_items` | `source_ref` | 对应 Session 原文窗口 |
| `vec_items` | `embedding_id` | 对应 `memory_items.id` |
| `vec_items` | `embedding` | 摘要的向量表示 |

### 6.12 即使没有提取结果，也会推进游标

```python
if not summaries:
    session.last_consolidated = consolidate_up_to
    return 0
```

作用是避免下一轮重复把同一个无价值窗口交给 LLM。

需要注意：LLM 调用失败时 `_llm_extract()` 也返回空列表。因此暂时性的 API 异常同样可能导致游标推进，这个窗口不会自动重试。

### 6.13 当前触发条件的一个细节

代码中的：

```python
new_count = total - last_consolidated
```

把最后保留的 10 条消息也计算在 `new_count` 中。因此第一次 consolidation 以后，只要又产生了可提取窗口，就可能很快再次触发，而不一定真的等待 6 条新的“可提取消息”。

如果设计目标是每积累 6 条待提取消息再运行，更严格的计算通常应围绕：

```text
(total - keep_count) - last_consolidated
```

本文只描述差异，不修改当前实现。

## 七、第五步：`_maybe_invalidate()` 淘汰过时记忆

### 7.1 Invalidation 是什么

Consolidation 负责新增长期记忆，Invalidation 负责退休已经过时或被用户纠正的长期记忆。

```text
用户以前：我喜欢咖啡
长期记忆：用户喜欢咖啡（active）

用户现在：不对，我现在喜欢茶
                     ↓
Invalidation 找到旧记忆
                     ↓
将“用户喜欢咖啡”改为 superseded
```

它不会直接删除数据库记录，因此仍可审计旧记忆。

### 7.2 本轮 `source_ref`

调用前，本轮 user 和 assistant 已经追加到 Session。

```python
current_source_ref = _source_ref_for_last_turn(
    user_id,
    chat_id,
    len(session.messages),
)
```

如果追加后总共有 14 条消息：

```python
message_count = 14
source_ref = "session:42:9001#msg:12-13"
```

其中：

```text
seq=12 → 本轮 user 消息
seq=13 → 本轮 assistant 消息
```

### 7.3 每轮都会异步检查

只要 `_invalidation` 已配置，`_maybe_invalidate()` 每轮都会：

```python
asyncio.create_task(_run())
```

不过并不是每轮都会更新数据库。Worker 首先判断用户是否表达了明确的纠正、否定、废弃或更新意图。

普通消息：

```text
我今天喝了茶
```

通常提取不到 invalidation topic，直接返回空列表。

纠正消息：

```text
不对，我已经不住北京了，现在住上海
```

可能提取：

```json
["用户居住地"]
```

### 7.4 `InvalidationWorker.run()` 的完整链路

```text
读取本轮 memorize 创建的记忆 ID，加入保护集合
                  ↓
LLM 从 user_msg 提取 1～3 个纠正主题
                  ↓
每个主题做向量检索 + 关键词检索
                  ↓
过滤重复项、受保护项和本轮同一 source_ref
                  ↓
LLM 判断哪些候选旧记忆确实冲突或过时
                  ↓
MemoryStore.mark_superseded_batch()
                  ↓
把旧记忆 status 改为 superseded
```

### 7.5 为什么保护本轮 `memorize` 结果

假设本轮工具调用刚写入：

```json
{
  "function": {"name": "memorize"},
  "result": "{\"status\":\"saved\",\"item_id\":\"new-id\"}"
}
```

`_collect_protected_ids()` 会取出 `new-id`。

后面即使 LLM 把它误判成候选，也不会在同一轮把刚写入的新记忆标记为过时。

### 7.6 候选记忆怎么查

每个纠正主题都会走两条检索通道：

```text
topic
  ├─ Embedder.embed(topic)
  │      ↓
  │  MemoryStore.vector_search(top_k=5)
  │
  └─ MemoryStore.keyword_search(limit=5)
         ↓
      合并、去重、过滤
```

只检查结构化长期记忆类型：

```python
["procedure", "preference", "profile", "event", "fact"]
```

### 7.7 数据库更新

最终执行：

```sql
UPDATE memory_items
SET status = 'superseded',
    updated_at = CURRENT_TIMESTAMP
WHERE id IN (...)
  AND user_id = ?
```

它只修改 `memory_items.status`：

```text
active → superseded
```

当前 `mark_superseded_batch()`：

- 不删除旧记忆；
- 不删除旧向量；
- 不创建新记忆；
- 不写 `memory_replacements` 替换关系。

正常检索默认只查询 `active`，所以被标记为 `superseded` 的记忆不会再进入普通召回结果。

### 7.8 输入输出例子

输入：

```python
superseded_ids = await worker.run(
    user_msg="不对，我喜欢茶，不喜欢咖啡了。",
    agent_response="好的，我更新一下。",
    tool_calls=[],
    user_id=42,
    chat_id=9001,
    source_ref="session:42:9001#msg:12-13",
)
```

数据库原数据：

```text
id=old-1
type=preference
summary=用户喜欢喝咖啡。
status=active
```

可能输出：

```python
superseded_ids == ["old-1"]
```

数据库变成：

```text
id=old-1
summary=用户喜欢喝咖啡。
status=superseded
```

新记忆“用户喜欢茶”需要由本轮 `memorize` 或后续 consolidation 负责写入。Invalidation 自己只负责淘汰旧记忆。

### 7.9 `agent_response` 当前没有参与判断

`run()` 虽然接收：

```python
agent_response=result.content
```

但当前实现的主题提取和冲突判断主要使用 `user_msg`，没有读取 `agent_response`。

这是为未来扩展预留的参数，不能理解为 Worker 当前会根据 Agent 回复判断记忆是否失效。

## 八、四种存储结果不要混淆

| 数据 | 保存位置 | 内容形态 | 主要用途 |
|---|---|---|---|
| Session 原文 | `conversation_sessions.messages_json` | 全部 user/assistant 原文 | 下一轮历史、`search_messages`、`fetch_messages` |
| Consolidation 游标 | `conversation_sessions.last_consolidated` | 整数下标 | 防止重复提取旧窗口 |
| 最近 Markdown 上下文 | `RECENT_CONTEXT.md` | 最近 10 条消息，Assistant 截到 80 字 | Prompt 的轻量最近上下文 |
| 长期记忆 | `memory_items` + `vec_items` | LLM 摘要 + embedding | `recall_memory` 语义/关键词召回 |
| 过时状态 | `memory_items.status` | `active/superseded` | 隐藏已失效的长期记忆 |

数据关系图：

```text
                       ┌────────────────────────────┐
                       │ conversation_sessions      │
本轮 user/assistant ──→│ messages_json：原始对话    │
                       │ last_consolidated：提取游标 │
                       └──────────────┬─────────────┘
                                      │
             ┌────────────────────────┼──────────────────────┐
             │                        │                      │
             ▼                        ▼                      ▼
   RECENT_CONTEXT.md       ConsolidationWorker     InvalidationWorker
   最近消息轻量副本         从旧窗口提取新记忆       查找冲突的旧记忆
                                      │                      │
                                      ▼                      ▼
                           memory_items + vec_items   status=superseded
```

## 九、同步任务和后台任务

### 9.1 同步完成后才返回

以下操作位于 `execute()` 主链路：

```text
append Session
SessionStore.save()
write_recent_turns()
调用 _maybe_consolidate()
调用 _maybe_invalidate()
```

其中 `_maybe_*()` 本身很快返回，只负责判断和创建后台任务。

### 9.2 后台执行

真正异步运行的是：

```text
ConsolidationWorker.consolidate()
InvalidationWorker.run()
```

因此 `execute()` 返回 `OutboundMessage` 时：

- Session SQLite 保存已经完成；
- `RECENT_CONTEXT.md` 刷新已经完成或失败并记录日志；
- 长期记忆提取可能仍在运行；
- 旧记忆失效检测可能仍在运行。

### 9.3 后台错误不会撤销回复

两个后台任务都捕获异常并写日志：

```text
consolidation 失败
→ 不影响已经发送的 Telegram 回复

invalidation 失败
→ 不影响已经发送的 Telegram 回复
```

这是“优先保证用户及时收到回复”的设计。

## 十、用一个完整例子串起来

假设本轮开始前：

```python
len(session.messages) == 18
session.last_consolidated == 0
```

用户说：

```text
不对，我已经从北京搬到上海了。
```

Agent 回复：

```text
明白，你现在居住在上海。
```

### 10.1 追加消息

```text
messages[18] = user 原话
messages[19] = assistant 回复
total = 20
```

### 10.2 保存 Session

```text
conversation_sessions
user_id = 42
chat_id = 9001
messages_json = 20 条完整消息
last_consolidated = 0
```

### 10.3 刷新最近上下文

```text
RECENT_CONTEXT.md
→ 写入 messages[10:20]
→ 共 10 条消息
```

### 10.4 Consolidation

```text
keep_count = 10
window = messages[0:10]
source_ref = session:42:9001#msg:0-9
```

后台从较旧的 10 条消息提取长期记忆，并把 `last_consolidated` 推进到 `10`。

### 10.5 Invalidation

```text
current_source_ref = session:42:9001#msg:18-19
topic = 用户居住地
候选旧记忆 = 用户住在北京
```

确认冲突后：

```text
“用户住在北京” status: active → superseded
```

本轮形成两种不同方向的维护：

```text
Consolidation：从旧窗口新增值得保留的记忆
Invalidation：根据当前纠正淘汰过时的旧记忆
```

## 十一、常见问题

### 11.1 Session 保存的是摘要吗

不是。`messages_json` 保存 user/assistant 对话原文。

### 11.2 `RECENT_CONTEXT.md` 是数据库备份吗

不是。它只保存最近 10 条消息的轻量视图，Assistant 还会截断到 80 字。

### 11.3 每轮都会生成长期记忆吗

不会。只有 consolidation 窗口满足条件，并且 LLM 判断内容值得长期保留时才写入。

### 11.4 每轮都会执行 invalidation 吗

每轮都会启动检查，但用户没有明确纠正旧信息时通常立即返回空列表，不更新数据库。

### 11.5 `last_consolidated=10` 是已经处理第 10 条吗

它更适合作为 Python 切片游标理解：`messages[:10]` 已处理，下一段从 `messages[10]` 开始。

### 11.6 Consolidation 和 Invalidation 会阻塞回复吗

不会。它们通过 `asyncio.create_task()` 在后台运行，而且是在 Telegram 回复已经发送后启动。

### 11.7 为什么 consolidation 完成后还要再保存一次 Session

因为后台任务修改了：

```python
session.last_consolidated
```

必须再次调用 `SessionStore.save()` 把新游标写回数据库，否则程序重启会丢失处理进度。

## 十二、最终总结

这段代码完成的不是第五个 Phase，而是五层 Pipeline 后面的“回合收尾与记忆维护”：

```text
追加原文
→ 让内存 Session 拥有本轮 user/assistant 消息

SessionStore.save
→ 将完整原文和 consolidation 游标写入 SQLite

refresh_markdown_recent_turns
→ 维护最近 10 条消息的 Markdown 轻量上下文

maybe_consolidate
→ 从较旧 Session 窗口异步提取新的长期记忆

maybe_invalidate
→ 根据当前用户纠正异步淘汰过时长期记忆
```

最核心的边界是：

```text
conversation_sessions = 原始对话
RECENT_CONTEXT.md       = 最近对话轻量副本
memory_items            = 长期记忆摘要
status=superseded       = 已失效但仍保留的旧记忆
```

理解这四种数据后，整个项目的 Session 与长期记忆闭环就完整了。
