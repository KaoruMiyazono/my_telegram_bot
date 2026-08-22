---
title: telegram-bot Pipeline完整链路
created: 2026-08-21
updated: 2026-08-21
tags: [telegram-bot, pipeline, phase, session, memory]
description: 讲解 PassiveTurnPipeline 如何组织 BeforeTurn、BeforeReasoning、Reasoner、AfterReasoning、AfterTurn，以及 Session 持久化、Consolidation 和 Invalidation 后台记忆任务。
source:
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/main.py # Pipeline 初始化和依赖接线
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/agent/pipeline/passive_turn.py # 单轮对话主流程
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/agent/pipeline/phases # 四个 Phase 的具体实现
  - Cursor AI 对话，2026-08-21
---

# telegram-bot Pipeline完整链路

> Pipeline 是一次用户消息的总调度器：它把“收到消息、加载 Session、检索记忆、构建 Prompt、调用 LLM、执行工具、发送回复、保存会话、维护长期记忆”串成一条完整链路。

## 一、先说明：Pipeline 整套系统是干什么的

Telegram Adapter 收到消息以后，并不会自己调用大模型，也不会自己检索记忆，而是把消息交给：

```python
PassiveTurnPipeline.execute(inbound_message)
```

`PassiveTurnPipeline` 再按照固定顺序调用各个组件：

```text
Telegram 收到消息
        ↓
InboundMessage
        ↓
┌─────────────────────────────────────────────┐
│ PassiveTurnPipeline                         │
│                                             │
│  1. BeforeTurn                              │
│     加载 Session、检索长期记忆                │
│                  ↓                          │
│  2. BeforeReasoning                         │
│     构建 System Prompt、messages、tools      │
│                  ↓                          │
│  3. Reasoner                                │
│     调用 LLM、执行工具、得到最终答案           │
│                  ↓                          │
│  4. AfterReasoning                          │
│     把答案包装成 OutboundMessage             │
│                  ↓                          │
│  5. AfterTurn                               │
│     发布事件、发送 Telegram 回复              │
│                  ↓                          │
│  6. 保存 Session                            │
│                  ↓                          │
│  7. 后台维护长期记忆                          │
│     Consolidation + Invalidation            │
└─────────────────────────────────────────────┘
        ↓
OutboundMessage
```

一句话理解：

> Phase 负责准备或收尾，Reasoner 负责真正推理，PassiveTurnPipeline 负责决定它们的执行顺序。

---

## 二、`main.py` 为什么先分别创建 Phase，再创建 Pipeline

`main.py` 没有让 `PassiveTurnPipeline` 自己创建依赖，而是先在外部创建：

```python
before_turn = BeforeTurnPhase(...)
before_reasoning = BeforeReasoningPhase(...)
after_reasoning = AfterReasoningPhase(...)
after_turn = AfterTurnPhase(...)
```

然后注入 Pipeline：

```python
pipeline = PassiveTurnPipeline(
    before_turn=before_turn,
    before_reasoning=before_reasoning,
    reasoner=reasoner,
    after_reasoning=after_reasoning,
    after_turn=after_turn,
    store=memory_store,
    consolidation_worker=consolidation,
    invalidation_worker=invalidation,
    memory_runtime=memory_runtime,
)
```

这种方式叫依赖注入。好处是：

- Pipeline 只负责编排，不负责创建组件。
- 测试时可以传入假的 Phase、假的 Reasoner。
- 每个 Phase 可以单独替换。
- PluginManager 收集到的插件模块可以在创建 Phase 时注入。

依赖关系图：

```text
EventBus ───────────────┐
PluginManager ──────────┤
MemoryEngine ───────────┤
ToolRegistry ───────────┤
Reasoner ───────────────┤
MemoryStore ────────────┤
ConsolidationWorker ────┤
InvalidationWorker ─────┤
                        ↓
               PassiveTurnPipeline
```

---

## 三、Pipeline 的输入和输出

### 输入：`InboundMessage`

```python
@dataclass(frozen=True)
class InboundMessage:
    user_id: int
    chat_id: int
    content: str
    metadata: dict[str, Any]
```

示例：

```python
InboundMessage(
    user_id=10001,
    chat_id=20001,
    content="我以前说过喜欢喝什么？",
    metadata={"channel": "telegram"},
)
```

### 输出：`OutboundMessage`

```python
@dataclass(frozen=True)
class OutboundMessage:
    chat_id: int
    content: str
    format: str = "text"
```

示例：

```python
OutboundMessage(
    chat_id=20001,
    content="根据之前的对话，你更喜欢喝茶。",
    format="text",
)
```

主函数签名：

```python
async def execute(
    self,
    inbound_message: InboundMessage,
) -> OutboundMessage:
```

---

## 四、Phase 1：`BeforeTurnPhase`

### 作用

`BeforeTurnPhase` 负责回答两个问题：

1. 这个用户当前 Session 里已经聊过什么？
2. 长期记忆里有没有与本次问题相关的内容？

调用位置：

```python
turn_ctx = await self.before_turn.build_ctx(inbound_message)
```

### 实现步骤

```text
InboundMessage
      ↓
acquire_session()
      ├── 先查内存缓存 _sessions
      └── 缓存没有 → SessionStore.load_state()
      ↓
拼检索 query
      ├── Session 最近 3 条 user 消息
      └── 当前用户消息
      ↓
MemoryEngine.retrieve(top_k=8)
      ↓
构建 BeforeTurnCtx
      ↓
运行插件 Module + EventBus
```

检索 Query 示例：

```text
历史最近用户消息：我最近想少喝咖啡
当前消息：我以前说过喜欢喝什么？

最终检索 query：
我最近想少喝咖啡 我以前说过喜欢喝什么？
```

### 输入和输出示例

输入：

```python
InboundMessage(
    user_id=10001,
    chat_id=20001,
    content="我以前说过喜欢喝什么？",
)
```

输出：

```python
BeforeTurnCtx(
    inbound_message=...,
    session=Session(...),
    retrieved_memories=[
        MemoryItem(
            memory_type="preference",
            summary="用户喜欢喝茶",
            source_ref="session:10001:20001#msg:4-5",
            ...,
        )
    ],
    session_key="10001:20001",
    content="我以前说过喜欢喝什么？",
    retrieved_memory_block="...",
)
```

### 这里操作了哪些存储

#### 1. 读取 `conversation_sessions`

第一次加载 Session 时执行：

```sql
SELECT messages_json, last_consolidated
FROM conversation_sessions
WHERE user_id = ? AND chat_id = ?
```

#### 2. 检索长期记忆

通过 `MemoryEngine.retrieve()` 检索：

- `memory_items`：长期记忆主体。
- `vec_items`：1024 维向量相似度检索。
- 关键词检索相关索引。

这一步只读，不会新增会话消息。

### 提前终止分支

插件或 EventBus 可以设置：

```python
turn_ctx.abort = True
turn_ctx.abort_reply = "该请求已被阻止"
```

Pipeline 会立即发送回复并返回：

```python
if turn_ctx.abort:
    await self._dispatch_abort(outbound)
    return outbound
```

此时不会继续执行 Reasoner，也不会走后面的 Session 保存和记忆维护。

---

## 五、Phase 2：`BeforeReasoningPhase`

### 作用

`BeforeReasoningPhase` 把 `BeforeTurnCtx` 转换成 Reasoner 可以直接使用的 `BeforeReasoningCtx`。

它准备三类核心数据：

```text
1. messages：发送给 LLM 的消息
2. tools：允许 LLM 调用的工具 Schema
3. Prompt：系统规则、记忆、自我模型、近期上下文
```

调用位置：

```python
reasoning_ctx = await self.before_reasoning.build_ctx(turn_ctx)
```

### 工具 Schema 从哪里来

```python
tools = self.tool_registry.get_schemas()
```

所以这里拿到的是已经注册完成的：

- 内置记忆工具。
- 插件工具。
- 以后可能接入的 MCP 包装工具。

### System Prompt 从哪里来

创建 Phase 时注入了三个 Markdown Reader：

```python
self_model_reader=memory_runtime.markdown.store.read_self
long_term_memory_reader=memory_runtime.markdown.store.read_long_term
recent_context_reader=memory_runtime.markdown.store.read_recent_context
```

它们会分别读取：

```text
Self Model
长期记忆 Markdown
近期上下文 Markdown
```

再结合 `BeforeTurn` 召回的结构化记忆，构建 System Prompt。

### 最终 messages 示例

```python
[
    {
        "role": "system",
        "content": "系统规则 + Self Model + 长期记忆 + 近期上下文",
    },
    {
        "role": "user",
        "content": "我最近想少喝咖啡",
    },
    {
        "role": "assistant",
        "content": "好的。",
    },
    {
        "role": "user",
        "content": "我以前说过喜欢喝什么？",
    },
]
```

### 插件的两个插入位置

```text
before_reasoning_modules
    修改 BeforeReasoningCtx、工具、提示信息

prompt_render_modules
    在 System Prompt 顶部或底部增加 Section
```

### `preheat()` 当前做了什么

`main.py` 启动时调用：

```python
await before_reasoning.preheat()
```

但当前实现是：

```python
async def preheat(self) -> None:
    pass
```

所以它目前什么都没有做，只是给将来的 Prompt 缓存、模型预热或资源加载预留接口。

### 提前终止分支

与 BeforeTurn 一样，插件或 EventBus 可以设置 `reasoning_ctx.abort`。一旦终止：

```text
发送 abort_reply
    ↓
直接返回 OutboundMessage
    ↓
不会调用 LLM
```

---

## 六、Phase 3：`Reasoner`

Reasoner 的完整细节见 [[learning_reasoner]]。

Pipeline 中只做一次调用：

```python
result = await self.reasoner.run_turn(reasoning_ctx)
```

Reasoner 内部可能多次调用 LLM：

```text
调用 LLM
  ├── 返回最终文字 → 完成
  └── 返回 tool_calls
           ↓
        执行工具
           ↓
        工具结果追加为 role="tool"
           ↓
        再次调用 LLM
```

输入：`BeforeReasoningCtx`。

输出示例：

```python
ReasonerResult(
    content="根据之前的对话，你更喜欢喝茶。",
    tool_calls=[
        {
            "function": {
                "name": "recall_memory",
                "arguments": "{...}",
            },
            "result": "{...}",
        }
    ],
    finish_reason="stop",
)
```

Pipeline 将结果保存到：

```python
self.last_reasoner_result = result
```

这主要方便测试或调试读取最近一次 Reasoner 结果。

---

## 七、Phase 4：`AfterReasoningPhase`

### 作用

Reasoner 返回的是推理结果，不是渠道消息。`AfterReasoningPhase` 负责把它包装成 Telegram 可以发送的 `OutboundMessage`。

```python
after_ctx = await self.after_reasoning.build_ctx(
    result=result,
    session=turn_ctx.session,
    chat_id=inbound_message.chat_id,
    user_id=inbound_message.user_id,
)
```

转换过程：

```text
ReasonerResult
├── content
├── tool_calls
└── finish_reason
        ↓
AfterReasoningPhase
        ↓
AfterReasoningCtx
├── outbound_message
├── tools_used
├── tool_chain
├── media
└── outbound_metadata
```

示例：

```python
AfterReasoningCtx(
    reasoner_result=result,
    outbound_message=OutboundMessage(
        chat_id=20001,
        content="根据之前的对话，你更喜欢喝茶。",
    ),
    tools_used=("recall_memory", "fetch_messages"),
    tool_chain=(...),
)
```

插件可以在这个阶段：

- 修改最终回复 `ctx.reply`。
- 增加 `outbound_metadata`。
- 增加媒体文件路径。

如果插件修改了 `ctx.reply`，代码会同步更新：

```text
ctx.outbound_message.content
ctx.reasoner_result.content
```

### `persist_messages()` 当前实际没有持久化

Pipeline 创建了后台任务：

```python
asyncio.create_task(
    self.after_reasoning.persist_messages(...)
)
```

但当前实现只是：

```python
async def persist_messages(...) -> list[MemoryItem]:
    return []
```

所以这段代码目前不会写数据库，也不会把每条原始消息写进长期向量池。

原始会话真正的持久化发生在后面的 `SessionStore.save()`。

---

## 八、Phase 5：`AfterTurnPhase`

### 作用

`AfterTurnPhase` 负责两件事：

1. 发布“这一轮已经完成”的事件。
2. 通过 Telegram Adapter 发送消息。

```python
await self.after_turn.execute(
    ctx=after_ctx,
    user_id=inbound_message.user_id,
    new_memory_ids=[],
    inbound_content=inbound_message.content,
)
```

### 创建 `TurnCommittedEvent`

```python
TurnCommittedEvent(
    turn_id="随机 UUID",
    user_id=10001,
    inbound_content="我以前说过喜欢喝什么？",
    outbound_message=OutboundMessage(...),
    new_memory_ids=[],
)
```

然后发布：

```python
await self.event_bus.emit("turn_committed", event=event)
```

`ConversationLogger` 就是通过订阅这个事件记录原始对话。

### 发送 Telegram 消息

```python
await self.telegram_adapter.send(ctx.outbound_message)
```

因此这一阶段结束时，用户通常已经在 Telegram 中看到回复。

### 为什么初始化时传入 `None`

`main.py` 先创建：

```python
after_turn = AfterTurnPhase(event_bus, None, ...)
```

然后创建 Adapter：

```python
adapter = TelegramAdapter(
    token=settings.TG_BOT_TOKEN,
    pipeline=pipeline,
    proxy=settings.HTTP_PROXY,
)
```

最后反向注入：

```python
after_turn.telegram_adapter = adapter
```

原因是出现了循环依赖：

```text
Pipeline 需要 AfterTurn
AfterTurn 需要 TelegramAdapter
TelegramAdapter 又需要 Pipeline
```

解决方式：

```text
先创建 AfterTurn(adapter=None)
        ↓
创建 Pipeline
        ↓
创建 TelegramAdapter(pipeline)
        ↓
把 Adapter 补回 AfterTurn
```

---

## 九、回复发送后，Session 才真正保存

`AfterTurnPhase` 返回后，Pipeline 才向 Session 追加本轮消息：

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

然后写入数据库：

```python
get_session_store().save(
    user_id,
    chat_id,
    session.messages,
    last_consolidated=session.last_consolidated,
)
```

执行的是 UPSERT：

```sql
INSERT INTO conversation_sessions (...)
VALUES (...)
ON CONFLICT(user_id, chat_id) DO UPDATE SET ...
```

### `conversation_sessions` 表结构

| 字段 | 作用 |
|---|---|
| `user_id` | Telegram 用户 ID，联合主键之一 |
| `chat_id` | Telegram 聊天 ID，联合主键之一 |
| `messages_json` | 当前 Session 的全部原始消息 JSON |
| `last_consolidated` | 已经完成长期记忆提炼的消息下标 |
| `created_at` | Session 首次创建时间 |
| `updated_at` | Session 最近保存时间 |

联合主键：

```text
(user_id, chat_id)
```

保存后的 `messages_json` 示例：

```json
[
  {"role": "user", "content": "我喜欢喝茶"},
  {"role": "assistant", "content": "好的，我记住了"},
  {"role": "user", "content": "我以前说过喜欢喝什么？"},
  {"role": "assistant", "content": "你说过喜欢喝茶"}
]
```

### 一个需要注意的执行顺序

当前顺序是：

```text
先发送 Telegram 回复
        ↓
再把本轮消息写入 conversation_sessions
```

因此如果 Telegram 发送成功、随后数据库保存失败，用户已经看到回复，但本轮 Session 可能没有持久化成功。

---

## 十、刷新 Markdown 近期上下文

Session 保存后调用：

```python
self._refresh_markdown_recent_turns(
    turn_ctx.session,
    inbound_message.user_id,
)
```

内部执行：

```python
markdown_store.write_recent_turns(
    user_id=user_id,
    messages=session.messages,
)
```

这一步把最新 Session 消息同步到 Markdown 近期上下文，供下一轮构建 System Prompt 时读取。

关系是：

```text
conversation_sessions
    保存完整原始 Session

Markdown recent context
    给 Prompt 快速注入近期上下文
```

---

## 十一、`ConsolidationWorker`：把旧对话提炼为长期记忆

创建方式：

```python
consolidation = ConsolidationWorker(
    keep_count=10,
    min_new_messages=6,
    markdown_store=memory_runtime.markdown.store,
)
```

### 两个参数

| 参数 | 作用 |
|---|---|
| `keep_count=10` | 最新 10 条消息留在近期窗口中，不参加本次提炼 |
| `min_new_messages=6` | `消息总数 - last_consolidated` 少于 6 时不启动 |

窗口计算：

```python
window = session.messages[
    session.last_consolidated : len(session.messages) - keep_count
]
```

示例：

```text
Session 一共 16 条消息
last_consolidated = 0
keep_count = 10

待提炼窗口：messages[0:6]
保留近期窗口：messages[6:16]
```

执行步骤：

```text
旧消息窗口
    ↓
拼成 conversation 文本
    ↓
调用 LLM 提取结构化记忆
    ├── profile
    ├── preference
    ├── procedure
    └── event
    ↓
MemoryStore.upsert_item()
    ↓
写入 memory_items + vec_items
    ↓
同步 Markdown history / pending / journal
    ↓
推进 session.last_consolidated
    ↓
再次保存 conversation_sessions
```

### 它是后台任务

Pipeline 使用：

```python
asyncio.create_task(_run())
```

所以 Telegram 回复不会等待长期记忆提炼完成。

`_consolidation_inflight` 用来避免同一个 `(user_id, chat_id)` 同时启动多个提炼任务。

---

## 十二、`InvalidationWorker`：淘汰已经过时的长期记忆

创建方式：

```python
invalidation = InvalidationWorker(
    memory_store,
    embedder,
)
```

它处理这种消息：

```text
以前：我住在北京
现在：你记错了，我已经搬到上海了
```

执行步骤：

```text
用户当前消息
    ↓
LLM 判断是否在明确纠正旧记忆
    ↓
提取主题，例如“用户居住地”
    ↓
向量检索 + 关键词检索候选旧记忆
    ↓
LLM 判断哪些候选应该失效
    ↓
MemoryStore.mark_superseded_batch()
    ↓
旧 memory_items.status = "superseded"
```

它不会删除旧记忆，而是把状态改为：

```text
active → superseded
```

这样系统仍然可以回答“你以前住在哪里”，但回答当前状态时优先使用新的 `active` 记忆。

它同样通过 `asyncio.create_task()` 后台运行，不阻塞 Telegram 回复。

---

## 十三、一次完整对话的时序示例

用户发送：

```text
你记错了，我已经不住北京了，现在住上海。
```

完整时序：

```text
TelegramAdapter
    │
    │ InboundMessage
    ▼
BeforeTurnPhase
    │ 读取 conversation_sessions
    │ 检索相关长期记忆
    ▼
BeforeReasoningPhase
    │ 读取 Markdown Self/Memory/Recent Context
    │ 构建 messages + tools
    ▼
Reasoner
    │ 调用 DeepSeek
    │ 可能调用 recall_memory / fetch_messages
    ▼
ReasonerResult
    │ content="明白，你现在住在上海。"
    ▼
AfterReasoningPhase
    │ 创建 OutboundMessage
    ▼
AfterTurnPhase
    │ 发布 turn_committed
    │ ConversationLogger 记录
    │ 发送 Telegram 回复
    ▼
SessionStore.save()
    │ conversation_sessions 追加 user + assistant
    ▼
Markdown recent context 刷新
    ▼
后台任务
    ├── Consolidation：提炼新长期记忆
    └── Invalidation：旧“住北京”标记 superseded
```

---

## 十四、正常分支与 Abort 分支的区别

### 正常分支

```text
BeforeTurn
  ↓
BeforeReasoning
  ↓
Reasoner
  ↓
AfterReasoning
  ↓
AfterTurn 发送回复
  ↓
保存 Session
  ↓
后台维护记忆
```

### Abort 分支

```text
BeforeTurn 或 BeforeReasoning 设置 abort
  ↓
构建简单 OutboundMessage
  ↓
TelegramAdapter.send()
  ↓
立即 return
```

Abort 分支不会执行：

- Reasoner。
- 工具调用。
- AfterReasoning。
- `turn_committed` 事件。
- Session 消息追加和保存。
- Consolidation。
- Invalidation。

---

## 十五、各组件职责不要混淆

| 组件 | 主要职责 | 不负责什么 |
|---|---|---|
| `TelegramAdapter` | 收消息、发消息 | 不组织推理流程 |
| `PassiveTurnPipeline` | 编排一整轮对话 | 不直接调用模型 API |
| `BeforeTurnPhase` | Session + 初始记忆检索 | 不生成最终回答 |
| `BeforeReasoningPhase` | Prompt + messages + tools | 不执行工具 |
| `Reasoner` | LLM + 工具循环 | 不发送 Telegram |
| `AfterReasoningPhase` | 包装和修改回复 | 当前不真正持久化消息 |
| `AfterTurnPhase` | 事件分发 + 渠道发送 | 不保存 Session |
| `SessionStore` | 原始会话持久化 | 不负责长期语义记忆 |
| `ConsolidationWorker` | 从旧会话提取长期记忆 | 不阻塞当前回复 |
| `InvalidationWorker` | 将过时长期记忆标为 superseded | 不删除 Session 原文 |

---

## 十六、当前实现中值得注意的地方

### 1. `preheat()` 目前是空实现

调用存在，但没有实际预热行为。

### 2. `persist_messages()` 目前也是空实现

原始消息实际由 `SessionStore.save()` 保存。

### 3. `new_memory_ids` 当前永远是空列表

```python
new_memory_ids = []
```

因此 `TurnCommittedEvent.new_memory_ids` 当前不会反映后台 Consolidation 新写入的记忆。

### 4. 发送回复早于 Session 数据库保存

Telegram 已成功回复，不代表 Session 一定已经成功落库。

### 5. 两个记忆 Worker 都是 fire-and-forget

它们的失败只会记录日志，不会撤回已经发送给用户的回复。

### 6. Adapter 采用后注入解决循环依赖

这是目前能工作的方法，但也说明渠道发送逻辑和 AfterTurn 绑定得比较紧。

---

## 十七、最终总结

把整个 Pipeline 压缩成一句伪代码：

```python
async def execute(message):
    turn_ctx = await before_turn.build_ctx(message)
    reasoning_ctx = await before_reasoning.build_ctx(turn_ctx)
    result = await reasoner.run_turn(reasoning_ctx)
    after_ctx = await after_reasoning.build_ctx(result)
    await after_turn.execute(after_ctx)
    session_store.save(...)
    schedule_consolidation()
    schedule_invalidation()
    return after_ctx.outbound_message
```

最重要的理解：

```text
BeforeTurn        负责“找资料”
BeforeReasoning   负责“整理给模型看的输入”
Reasoner          负责“推理和使用工具”
AfterReasoning    负责“整理模型输出”
AfterTurn         负责“广播事件和发送回复”
Pipeline          负责“按顺序调用所有组件”
```
