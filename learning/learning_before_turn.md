---
title: BeforeTurnPhase：Session加载、长期记忆检索与上下文构建
created: 2026-08-21
updated: 2026-08-21
tags: [telegram-bot, pipeline, before-turn, session, memory, plugin, event-bus]
description: 逐步讲解 BeforeTurnPhase 如何从 InboundMessage 加载 Session、查询 conversation_sessions、组合检索文本、通过 MemoryEngine 检索长期记忆、运行插件模块和 EventBus GATE，最终构造 BeforeTurnCtx。
source:
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/agent/pipeline/phases/before_turn.py # 本文主要讲解对象
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/agent/core/types.py # InboundMessage、Session、MemoryItem、BeforeTurnCtx 数据结构
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/persistence/session_store.py # Session 加载和数据库查询
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/persistence/database.py # conversation_sessions 表结构
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/memory/engine.py # MemoryRetrieveRequest、MemoryScope 和检索结果
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/agent/lifecycle/phase.py # PhaseFrame 和插件模块调度
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/agent/core/event_bus.py # BeforeTurnCtx 的 GATE 生命周期处理
  - Cursor AI 对话，2026-08-21
---

# BeforeTurnPhase：Session加载、长期记忆检索与上下文构建

> <code>BeforeTurnPhase</code> 是一次对话进入 Pipeline 后的第一道准备阶段：它把一条简单的 <code>InboundMessage</code>，扩充成包含 Session、历史消息、相关长期记忆、检索轨迹和插件扩展信息的 <code>BeforeTurnCtx</code>。

## 一、这个系统是干什么的

TelegramAdapter 交给 Pipeline 的数据比较简单：

~~~python
InboundMessage(
    user_id=10001,
    chat_id=20001,
    content="我以前说过喜欢喝什么？",
    metadata={"channel": "telegram"},
)
~~~

这里已经包含：

- 谁发来的：<code>user_id</code>。
- 从哪个聊天发来的：<code>chat_id</code>。
- 用户本轮的问题：<code>content</code>。
- Telegram 等渠道附加信息：<code>metadata</code>。

但是 Reasoner 不能只看这一句话，它还需要知道：

- 这个会话以前聊过什么。
- 数据库里有没有保存过这个 Session。
- 长期记忆中有没有与本轮问题相关的内容。
- 插件是否要补充提示或提前拦截请求。
- EventBus 生命周期处理器是否允许继续。

这些工作就是 <code>BeforeTurnPhase</code> 负责的。

整体输入输出如下：

~~~text
InboundMessage
  只有当前消息
        │
        ▼
┌───────────────────────────────────────┐
│ BeforeTurnPhase                       │
│                                       │
│ 1. acquire_session()                  │
│    内存缓存 → SQLite                  │
│                                       │
│ 2. 组合检索 query                     │
│    最近历史用户消息 + 当前问题          │
│                                       │
│ 3. prepare_context()                  │
│    MemoryEngine 检索长期记忆            │
│                                       │
│ 4. 插件 PhaseModule                   │
│    补充信息或提前终止                   │
│                                       │
│ 5. EventBus GATE                      │
│    修改、放行或阻断 Context             │
└───────────────────────────────────────┘
        │
        ▼
BeforeTurnCtx
  当前消息 + Session + 历史 + 长期记忆 + 扩展信息
~~~

在完整 Pipeline 中，它位于：

~~~text
TelegramAdapter
      ↓
InboundMessage
      ↓
BeforeTurnPhase        ← 本文
      ↓
BeforeReasoningPhase
      ↓
Reasoner
      ↓
AfterReasoningPhase
      ↓
AfterTurnPhase
~~~

可以结合 [[learning_pipeline]] 一起阅读。

---

## 二、BeforeTurnPhase 的依赖

<code>main.py</code> 中的初始化代码是：

~~~python
before_turn = BeforeTurnPhase(
    event_bus=event_bus,
    plugin_modules=plugin_manager.before_turn_modules,
    memory_engine=memory_runtime.engine,
)
~~~

三个主要依赖分别是：

| 依赖 | 作用 |
|---|---|
| <code>memory_engine</code> | 根据用户问题检索长期记忆 |
| <code>event_bus</code> | 对构造好的 BeforeTurnCtx 执行 GATE 生命周期处理 |
| <code>plugin_modules</code> | 允许插件在 BeforeTurn 的不同节点插入处理逻辑 |

### 构造函数

~~~python
def __init__(
    self,
    *,
    memory_engine: object,
    event_bus: EventBus | None = None,
    plugin_modules: Sequence[object] | None = None,
) -> None:
~~~

输入示例：

~~~python
phase = BeforeTurnPhase(
    memory_engine=default_memory_engine,
    event_bus=EventBus.get_instance(),
    plugin_modules=[permission_module, hint_module],
)
~~~

构造函数没有显式返回值，只会初始化对象状态。

初始化后的重要字段：

| 字段 | 含义 |
|---|---|
| <code>event_bus</code> | 当前 Phase 使用的 EventBus；未传入则取全局单例 |
| <code>plugin_modules</code> | BeforeTurn 插件模块列表 |
| <code>memory_engine</code> | 统一记忆引擎 |
| <code>last_retrieved</code> | 最近一次检索得到的 MemoryItem 列表 |
| <code>last_query_text</code> | 最近一次使用的检索文本 |
| <code>last_retrieved_memory_block</code> | 最近一次格式化后的记忆文本 |
| <code>last_retrieval_trace</code> | 最近一次检索轨迹，例如 RRF、HyDE 信息 |

注意：

> <code>BeforeTurnPhase</code> 自己没有实现向量检索和 RRF 融合；这些逻辑被委托给 <code>memory_engine.retrieve()</code>。

---

## 三、核心输入输出数据结构

### 1. InboundMessage：本轮原始输入

~~~python
@dataclass(frozen=True)
class InboundMessage:
    user_id: int
    chat_id: int
    content: str
    metadata: dict[str, Any]
~~~

示例：

~~~python
InboundMessage(
    user_id=10001,
    chat_id=20001,
    content="根据我的偏好推荐一种饮料",
    metadata={
        "channel": "telegram",
        "username": "zhangsan",
        "update_id": 90001,
    },
)
~~~

它就是你前面理解的：

> 包含聊天定位信息，也包含用户本轮问题的统一数据结构。

### 2. Session：当前聊天的短期会话

~~~python
@dataclass
class Session:
    user_id: int
    chat_id: int
    messages: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime
    last_consolidated: int
~~~

示例：

~~~python
Session(
    user_id=10001,
    chat_id=20001,
    messages=[
        {"role": "user", "content": "我喜欢喝茶"},
        {"role": "assistant", "content": "记住了"},
    ],
    last_consolidated=0,
)
~~~

### 3. MemoryItem：长期记忆检索结果

~~~python
MemoryItem(
    id=UUID("..."),
    user_id=10001,
    memory_type="preference",
    summary="用户喜欢喝茶",
    embedding=[...],
    status="active",
    source_ref="session:10001:20001#msg:0",
)
~~~

### 4. BeforeTurnCtx：本阶段最终输出

重要字段可以分成五组：

| 分组 | 字段 |
|---|---|
| 当前输入 | <code>inbound_message</code>、<code>content</code> |
| Session | <code>session</code>、<code>session_key</code>、<code>history_messages</code> |
| 渠道定位 | <code>channel</code>、<code>chat_id</code> |
| 长期记忆 | <code>retrieved_memories</code>、<code>retrieved_memory_block</code>、<code>retrieval_trace_raw</code> |
| 扩展与控制 | <code>extra_hints</code>、<code>extra_metadata</code>、<code>abort</code>、<code>abort_reply</code> |

输出示例：

~~~python
BeforeTurnCtx(
    inbound_message=inbound,
    session=session,
    retrieved_memories=[tea_preference],
    session_key="10001:20001",
    channel="telegram",
    chat_id="20001",
    content="根据我的偏好推荐一种饮料",
    retrieved_memory_block=(
        "[preference] 用户喜欢喝茶 "
        "(source: session:10001:20001#msg:0)"
    ),
    history_messages=(
        {"role": "user", "content": "我喜欢喝茶"},
        {"role": "assistant", "content": "记住了"},
    ),
    abort=False,
)
~~~

注意类型变化：

- <code>InboundMessage.chat_id</code> 是整数。
- <code>BeforeTurnCtx.chat_id</code> 被转换成字符串。
- <code>session_key</code> 使用 <code>user_id:chat_id</code> 格式。

---

## 四、完整 build_ctx 链路

<code>PassiveTurnPipeline.execute()</code> 首先调用：

~~~python
turn_ctx = await self.before_turn.build_ctx(inbound_message)
~~~

<code>build_ctx()</code> 的完整流程是：

~~~text
build_ctx(inbound_message)
        │
        ▼
acquire_session()
        │
        ├── 命中 _sessions 内存缓存
        │       └── 直接返回 Session
        │
        └── 未命中缓存
                └── SessionStore.load_state()
                        ├── 数据库存在 → 恢复消息和游标
                        └── 数据库不存在 → 创建空 Session
        │
        ▼
创建 PhaseFrame
        │
        ▼
运行“Session 已就绪”的插件
        │
        ├── 插件直接提供 BeforeTurnCtx → 立即返回
        └── 插件提供 abort_reply → 返回终止 Context
        │
        ▼
最近 3 条历史记录中筛选 user 消息
        │
        ▼
历史用户消息 + 当前 content → query_text
        │
        ▼
prepare_context()
        │
        └── MemoryEngine.retrieve()
        │
        ▼
运行“记忆已就绪”的插件
        │
        ▼
构造 BeforeTurnCtx
        │
        ▼
运行“Context 已就绪”的插件
        │
        ▼
EventBus.emit(ctx)
        │
        ├── 返回 None → abort
        └── 返回 Context → 继续
        │
        ▼
运行“EventBus 已完成”的插件
        │
        ▼
收集 extra_hint 和 abort_reply
        │
        ▼
返回 BeforeTurnCtx
~~~

---

## 五、acquire_session()：先缓存，后数据库

函数签名：

~~~python
async def acquire_session(
    self,
    message: InboundMessage,
) -> Session:
~~~

### 输入示例

~~~python
message = InboundMessage(
    user_id=10001,
    chat_id=20001,
    content="你好",
)
~~~

### 第一步：构造缓存键

~~~python
key = (message.user_id, message.chat_id)
~~~

示例：

~~~python
key == (10001, 20001)
~~~

为什么必须同时使用 <code>user_id</code> 和 <code>chat_id</code>？

- 同一个用户可能在私聊和群聊中与 Bot 对话。
- 同一个群聊也可能存在多个用户。
- 两个字段组合后，才能定位一个具体会话。

### 第二步：查询进程内缓存

模块顶部定义：

~~~python
_sessions: dict[tuple[int, int], Session] = {}
~~~

查询逻辑：

~~~python
session = _sessions.get(key)
if session is not None:
    return session
~~~

命中缓存时不会访问数据库。

~~~text
第 1 条消息：
内存没有 → 查 SQLite → 创建 Session → 放入 _sessions

第 2 条消息：
内存已有 → 直接返回同一个 Session 对象
~~~

### 第三步：缓存未命中时读取数据库

~~~python
session_store = get_session_store()
session_state = session_store.load_state(
    message.user_id,
    message.chat_id,
)
~~~

如果数据库中不存在：

~~~python
saved_messages = []
last_consolidated = 0
~~~

如果数据库中存在：

~~~python
saved_messages, last_consolidated = session_state
~~~

最后构造并缓存：

~~~python
session = Session(
    user_id=message.user_id,
    chat_id=message.chat_id,
    messages=saved_messages,
    last_consolidated=last_consolidated,
)
_sessions[key] = session
~~~

### 输出示例

~~~python
Session(
    user_id=10001,
    chat_id=20001,
    messages=[
        {"role": "user", "content": "我喜欢喝茶"},
        {"role": "assistant", "content": "记住了"},
    ],
    last_consolidated=2,
)
~~~

### acquire_session() 的缓存层级图

~~~text
                (user_id, chat_id)
                        │
                        ▼
              ┌─────────────────┐
              │ _sessions 缓存   │
              └─────────────────┘
                  │          │
               命中          未命中
                  │          │
                  │          ▼
                  │  conversation_sessions
                  │       SQLite 表
                  │          │
                  └──────────┴──────→ Session
~~~

---

## 六、这里操作了什么数据库

<code>BeforeTurnPhase</code> 本身不直接写 SQL，但 <code>acquire_session()</code> 会通过 <code>SessionStore.load_state()</code> 查询 SQLite。

执行的 SQL：

~~~sql
SELECT messages_json, last_consolidated
FROM conversation_sessions
WHERE user_id = ? AND chat_id = ?
~~~

参数示例：

~~~python
(10001, 20001)
~~~

### conversation_sessions 表结构

~~~sql
CREATE TABLE IF NOT EXISTS conversation_sessions (
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    messages_json TEXT NOT NULL DEFAULT '[]',
    last_consolidated INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, chat_id)
);
~~~

字段说明：

| 字段 | 类型 | 作用 |
|---|---|---|
| <code>user_id</code> | INTEGER | Telegram 用户 ID |
| <code>chat_id</code> | INTEGER | Telegram 聊天 ID |
| <code>messages_json</code> | TEXT | 完整 Session 消息列表的 JSON 字符串 |
| <code>last_consolidated</code> | INTEGER | 已经完成长期记忆 Consolidation 的消息位置 |
| <code>created_at</code> | TIMESTAMP | Session 第一次写入数据库的时间 |
| <code>updated_at</code> | TIMESTAMP | Session 最近一次保存时间 |

主键：

~~~text
PRIMARY KEY (user_id, chat_id)
~~~

表示一个用户在一个聊天中只能有一条 Session 记录。

数据库记录示例：

| user_id | chat_id | messages_json | last_consolidated |
|---:|---:|---|---:|
| 10001 | 20001 | [{"role":"user","content":"我喜欢喝茶"},{"role":"assistant","content":"记住了"}] | 2 |

读取后会执行：

~~~python
messages = json.loads(row[0])
last_consolidated = int(row[1] or 0)
~~~

得到：

~~~python
(
    [
        {"role": "user", "content": "我喜欢喝茶"},
        {"role": "assistant", "content": "记住了"},
    ],
    2,
)
~~~

### 本阶段会不会保存 Session

不会。

<code>BeforeTurnPhase</code> 只负责读取 Session。新一轮用户消息和助手回答是在后面的 <code>PassiveTurnPipeline.execute()</code> 中追加，并调用：

~~~python
get_session_store().save(...)
~~~

所以数据库方向是：

~~~text
BeforeTurnPhase
    conversation_sessions → Session
    只读取

对话完成以后
    Session → conversation_sessions
    才保存
~~~

---

## 七、怎样生成长期记忆检索问题

加载 Session 后，代码不会只拿当前问题检索长期记忆，而是加入最近的用户消息：

~~~python
user_messages = [
    msg["content"]
    for msg in session.messages[-3:]
    if msg.get("role") == "user"
]
user_messages.append(inbound_message.content)
query_text = " ".join(user_messages)
~~~

假设 Session 最后三条记录是：

~~~python
[
    {"role": "assistant", "content": "还需要什么帮助？"},
    {"role": "user", "content": "我最近想少喝咖啡"},
    {"role": "assistant", "content": "可以试试茶"},
]
~~~

当前问题是：

~~~text
那你给我推荐一种吧
~~~

得到的 <code>query_text</code>：

~~~text
我最近想少喝咖啡 那你给我推荐一种吧
~~~

这样比只检索“那你给我推荐一种吧”更容易找到与饮品偏好相关的长期记忆。

### 一个容易看错的细节

代码是：

~~~python
session.messages[-3:]
~~~

然后才过滤 <code>role == "user"</code>。

因此它不是“最近三条用户消息”，而是：

> 最近三条全部消息中，属于 user 的那些消息。

由于 Session 通常是 user、assistant 交替，最终往往只能取得一到两条历史用户消息。

---

## 八、prepare_context()：调用 MemoryEngine

函数签名：

~~~python
async def prepare_context(
    self,
    session: Session,
    query_text: str,
    user_id: int,
) -> list[MemoryItem]:
~~~

### 输入示例

~~~python
session = Session(
    user_id=10001,
    chat_id=20001,
    messages=[...],
)

query_text = "我最近想少喝咖啡 那你给我推荐一种吧"
user_id = 10001
~~~

### 构造 MemoryRetrieveRequest

~~~python
MemoryRetrieveRequest(
    query=query_text,
    scope=MemoryScope(
        user_id=user_id,
        chat_id=session.chat_id,
        session_key=f"{session.user_id}:{session.chat_id}",
    ),
    top_k=8,
    memory_types=LONG_TERM_MEMORY_TYPES,
)
~~~

对应的实际数据：

~~~python
MemoryRetrieveRequest(
    query="我最近想少喝咖啡 那你给我推荐一种吧",
    scope=MemoryScope(
        user_id=10001,
        chat_id=20001,
        session_key="10001:20001",
    ),
    top_k=8,
    memory_types=[
        "profile",
        "preference",
        "procedure",
        "event",
        "fact",
    ],
)
~~~

字段作用：

| 字段 | 作用 |
|---|---|
| <code>query</code> | 用来检索的文本 |
| <code>scope.user_id</code> | 限制只查当前用户的长期记忆 |
| <code>scope.chat_id</code> | 标明当前聊天 |
| <code>scope.session_key</code> | 标明当前 Session |
| <code>top_k=8</code> | 最多取前 8 条长期记忆 |
| <code>memory_types</code> | 检索全部五种长期记忆类型 |

五种长期记忆类型是：

~~~text
profile     用户画像
preference 用户偏好
procedure  操作流程和规则
event      历史事件
fact       一般事实
~~~

### MemoryEngine 内部发生什么

简化理解：

~~~text
query_text
    │
    ├── 原始语义向量检索
    ├── HyDE 辅助查询
    └── 关键词检索
            │
            ▼
         RRF 融合
            │
            ▼
       前 8 条 MemoryItem
~~~

<code>BeforeTurnPhase</code> 只发出统一请求，真正的检索算法在 <code>DefaultMemoryEngine</code> 中。

可以结合 [[learning_default_memory_engine]] 阅读。

### 保存最近一次检索状态

~~~python
self.last_query_text = query_text
self.last_retrieved = list(result.items)
self.last_retrieved_memory_block = result.text_block
self.last_retrieval_trace = dict(result.trace or {})
self._last_hyde_used = bool(result.trace.get("hyde_used"))
self._last_hypothesis = str(result.trace.get("hypothesis") or "")
~~~

这些字段主要用于后续构造 Context、调试和观察检索过程。

### 输出示例

~~~python
[
    MemoryItem(
        memory_type="preference",
        summary="用户偏好喝茶，并希望减少咖啡摄入",
        ...
    ),
    MemoryItem(
        memory_type="profile",
        summary="用户经常在下午选择饮品",
        ...
    ),
]
~~~

同时 <code>result.text_block</code> 可能是：

~~~text
[preference] 用户偏好喝茶，并希望减少咖啡摄入
[profile] 用户经常在下午选择饮品
~~~

<code>prepare_context()</code> 的直接返回值只有：

~~~python
list[MemoryItem]
~~~

文本块和 trace 则先保存到 Phase 实例字段中，再由 <code>build_ctx()</code> 放进 <code>BeforeTurnCtx</code>。

---

## 九、PhaseFrame 和插件插槽

代码先创建：

~~~python
frame = PhaseFrame(
    input=inbound_message,
    slots={
        "session:session": session,
        "before_turn.acquire_session": True,
    },
)
~~~

可以把 <code>PhaseFrame</code> 理解成插件之间共享的工作台：

~~~text
PhaseFrame
├── input
│   └── InboundMessage
└── slots
    ├── session:session
    ├── before_turn.acquire_session
    ├── session:retrieved_memories
    ├── session:retrieved_memory_block
    ├── session:ctx
    └── ...
~~~

插件模块声明：

- <code>requires</code>：运行前需要哪些 slot。
- <code>produces</code>：运行后会产生哪些 slot。
- <code>slot</code>：插件模块自己的完成标记。

<code>PhaseModuleRunner.run_ready()</code> 会反复检查：

~~~text
插件依赖的 slot 是否都已经存在？
        │
        ├── 否 → 继续等待下一个内置阶段
        └── 是 → 立即运行该插件
~~~

因此 <code>build_ctx()</code> 会在多个节点调用：

~~~python
frame = await plugin_runner.run_ready(frame)
~~~

主要锚点如下：

| 锚点 slot | 表示什么已经完成 |
|---|---|
| <code>before_turn.acquire_session</code> | Session 已加载 |
| <code>before_turn.prepare_context</code> | 长期记忆已检索 |
| <code>before_turn.build_ctx</code> | BeforeTurnCtx 已构造 |
| <code>before_turn.emit</code> | EventBus GATE 已执行 |
| <code>before_turn.collect_exports</code> | 插件提示已收集 |
| <code>before_turn.return</code> | BeforeTurnPhase 即将返回 |

这使插件不用修改 <code>BeforeTurnPhase</code> 源代码，也能在特定时间点扩展处理逻辑。

---

## 十、插件如何提前返回或终止

### 1. 插件直接提供完整 Context

~~~python
early_ctx = frame.slots.get("session:ctx")
if isinstance(early_ctx, BeforeTurnCtx):
    return early_ctx
~~~

如果插件已经构造好 <code>BeforeTurnCtx</code>，本阶段直接返回。

这条路径会跳过：

- 默认长期记忆检索。
- 后续插件锚点。
- EventBus GATE。
- extra_hint 收集。

### 2. 插件要求终止请求

~~~python
early_abort = frame.slots.get("session:abort_reply")
~~~

例如插件写入：

~~~python
frame.slots["session:abort_reply"] = "当前请求不允许执行。"
~~~

BeforeTurnPhase 会返回：

~~~python
BeforeTurnCtx(
    inbound_message=inbound_message,
    session=session,
    retrieved_memories=[],
    abort=True,
    abort_reply="当前请求不允许执行。",
    ...
)
~~~

Pipeline 随后检测：

~~~python
if turn_ctx.abort:
    ...
~~~

于是不会进入 Reasoner，而是直接把 <code>abort_reply</code> 发给用户。

---

## 十一、EventBus GATE 做什么

构造好 Context 后执行：

~~~python
emitted = await self.event_bus.emit(ctx)
~~~

这不是普通广播，而是 GATE 链：

~~~text
BeforeTurnCtx
    ↓
高优先级 GATE handler
    ↓
下一个 GATE handler
    ↓
最终 Context
~~~

每个 handler 可以：

- 返回修改后的 Context：继续。
- 返回原 Context：直接放行。
- 返回 <code>None</code>：阻断本轮请求。

如果返回 <code>None</code>：

~~~python
ctx.abort = True
ctx.abort_reply = "请求已被生命周期处理器阻断。"
~~~

示例：

~~~python
async def block_empty_message(ctx: BeforeTurnCtx):
    if not ctx.content.strip():
        return None
    return ctx
~~~

EventBus 的完整设计可以结合 [[learning_event_bus]] 阅读。

---

## 十二、extra_hints 是怎样收集的

插件可以写入带此前缀的 slot：

~~~text
session:extra_hint:
~~~

例如：

~~~python
frame.slots["session:extra_hint:permission"] = (
    "回答时不要暴露用户隐私。"
)
frame.slots["session:extra_hint:style"] = [
    "使用中文回答。",
    "回答尽量简洁。",
]
~~~

代码先收集前缀：

~~~python
collect_prefixed_slots(
    frame.slots,
    "session:extra_hint:",
)
~~~

再追加到：

~~~python
ctx.extra_hints
~~~

最终得到：

~~~python
ctx.extra_hints == [
    "回答时不要暴露用户隐私。",
    "使用中文回答。",
    "回答尽量简洁。",
]
~~~

这些提示可以在后面的 <code>BeforeReasoningPhase</code> 中进入 Prompt 构造过程。

非字符串值会被忽略并记录 warning。

---

## 十三、build_ctx() 输入输出完整例子

### 输入

~~~python
inbound = InboundMessage(
    user_id=10001,
    chat_id=20001,
    content="按照我的习惯，下午喝什么比较好？",
    metadata={
        "channel": "telegram",
        "username": "zhangsan",
    },
)
~~~

数据库中的 Session：

~~~python
[
    {"role": "user", "content": "我喜欢喝茶"},
    {"role": "assistant", "content": "记住了"},
    {"role": "user", "content": "我不想喝太多咖啡"},
    {"role": "assistant", "content": "可以减少咖啡因摄入"},
]
~~~

### 中间检索文本

最后三条 Session 消息中只有一条 user 消息：

~~~text
我不想喝太多咖啡
~~~

加上当前问题后：

~~~text
我不想喝太多咖啡 按照我的习惯，下午喝什么比较好？
~~~

### 检索结果

~~~python
[
    MemoryItem(
        memory_type="preference",
        summary="用户喜欢喝茶",
        ...
    ),
    MemoryItem(
        memory_type="preference",
        summary="用户希望减少咖啡摄入",
        ...
    ),
]
~~~

### 最终输出

~~~python
BeforeTurnCtx(
    inbound_message=inbound,
    session=Session(...),
    retrieved_memories=[memory_1, memory_2],
    session_key="10001:20001",
    channel="telegram",
    chat_id="20001",
    content="按照我的习惯，下午喝什么比较好？",
    retrieved_memory_block=(
        "[preference] 用户喜欢喝茶\n"
        "[preference] 用户希望减少咖啡摄入"
    ),
    history_messages=(
        {"role": "user", "content": "我喜欢喝茶"},
        {"role": "assistant", "content": "记住了"},
        {"role": "user", "content": "我不想喝太多咖啡"},
        {"role": "assistant", "content": "可以减少咖啡因摄入"},
    ),
    abort=False,
)
~~~

然后它被交给：

~~~python
reasoning_ctx = await before_reasoning.build_ctx(turn_ctx)
~~~

---

## 十四、状态分别保存在哪里

这一阶段容易把不同状态混在一起，可以按下图区分：

~~~text
┌──────────────────────────────────────────────┐
│ 1. SQLite                                    │
│ conversation_sessions                       │
│ 保存可跨重启恢复的 Session 消息和游标          │
└──────────────────────────────────────────────┘
                    │ load_state()
                    ▼
┌──────────────────────────────────────────────┐
│ 2. _sessions 内存缓存                         │
│ key = (user_id, chat_id)                     │
│ 保存当前进程正在使用的 Session 对象             │
└──────────────────────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────────┐
│ 3. BeforeTurnCtx                             │
│ 保存本轮处理需要的 Session、历史和长期记忆       │
└──────────────────────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────────┐
│ 4. BeforeTurnPhase.last_*                    │
│ 保存最近一次检索的调试状态                     │
└──────────────────────────────────────────────┘
~~~

服务器或进程重启后：

- <code>_sessions</code> 会丢失。
- <code>BeforeTurnCtx</code> 会丢失。
- <code>BeforeTurnPhase.last_*</code> 会丢失。
- SQLite 中的 <code>conversation_sessions</code> 仍然存在。
- 下一条消息到来时会重新从 SQLite 恢复 Session。

---

## 十五、几个容易忽略的细节

### 1. history_messages 不是深拷贝

~~~python
history_messages=tuple(session.messages)
~~~

这里只把外层 list 转成 tuple，其中每个消息 dict 仍是原来的对象引用。

因此它是外层不可追加的快照，但不是完全不可变的深拷贝。

### 2. Context 中还没有追加当前消息

<code>BeforeTurnPhase</code> 读取的是进入本轮之前的 <code>session.messages</code>。

当前用户问题保存在：

~~~python
ctx.content
ctx.inbound_message.content
~~~

它会在 Reasoner 完成后才被追加进 Session。

### 3. prepare_context() 名字容易误解

它并没有构造完整的 <code>BeforeTurnCtx</code>，只是检索长期记忆。

真正构造 Context 的函数是：

~~~python
build_ctx()
~~~

### 4. 注释中的 RRF 不在当前文件实现

类注释写着“RRF 融合检索”，但当前文件只是调用：

~~~python
self.memory_engine.retrieve(...)
~~~

RRF、向量检索、关键词检索和 HyDE 都由 MemoryEngine 完成。

### 5. channel 有默认值

~~~python
channel=str(
    inbound_message.metadata.get("channel")
    or "telegram"
)
~~~

如果 metadata 没有 channel，就默认认为来自 Telegram。

---

## 十六、并发情况下要注意什么

### 1. last_retrieved 等字段属于 Phase 实例

<code>BeforeTurnPhase</code> 通常是全局共用的一个实例。

但是这些数据存在实例字段：

~~~python
self.last_retrieved
self.last_query_text
self.last_retrieved_memory_block
self.last_retrieval_trace
~~~

如果两个用户的消息并发执行：

~~~text
用户 A prepare_context()
    写入 last_retrieval_trace=A
            │
            ├── 切换任务
            ▼
用户 B prepare_context()
    写入 last_retrieval_trace=B
            │
            ▼
用户 A build_ctx()
    可能读到 B 的 trace
~~~

因此这些本轮数据更安全的做法是：

- 让 <code>prepare_context()</code> 一次返回 items、text_block、trace。
- 或直接保存到当前 <code>PhaseFrame</code>。
- 不要使用共享实例字段传递一次请求中的临时状态。

这是当前实现中值得留意的并发风险。

### 2. _sessions 是模块级共享字典

~~~python
_sessions: dict[tuple[int, int], Session] = {}
~~~

同一进程内所有 <code>BeforeTurnPhase</code> 实例都共享它。

它没有锁。两个相同 Session 的请求同时首次到达时，理论上可能重复读取数据库并创建两个 Session 对象。

### 3. SQLite load_state() 是同步函数

<code>acquire_session()</code> 虽然声明为 async，但内部调用：

~~~python
session_store.load_state(...)
~~~

这是同步 SQLite 查询。数据库较小时通常很快，但它仍会短暂占用 asyncio 事件循环线程。

---

## 十七、终止链路是什么样的

正常链路：

~~~text
BeforeTurnCtx.abort = False
        ↓
BeforeReasoning
        ↓
Reasoner
        ↓
回复
~~~

插件或 EventBus 阻断：

~~~text
BeforeTurnCtx.abort = True
BeforeTurnCtx.abort_reply = "..."
        ↓
PassiveTurnPipeline 检测 abort
        ↓
构造 OutboundMessage
        ↓
直接发送 abort_reply
        ↓
不进入 Reasoner
~~~

对应 Pipeline 代码：

~~~python
turn_ctx = await self.before_turn.build_ctx(inbound_message)

if turn_ctx.abort:
    outbound = OutboundMessage(
        chat_id=inbound_message.chat_id,
        content=turn_ctx.abort_reply or "",
    )
    await self._dispatch_abort(outbound)
    return outbound
~~~

因此 <code>BeforeTurnPhase</code> 同时也是一次对话进入 LLM 之前的第一道控制门。

---

## 十八、四个函数快速总结

| 函数 | 输入 | 输出 | 主要作用 |
|---|---|---|---|
| <code>__init__()</code> | MemoryEngine、EventBus、插件模块 | 无 | 保存依赖和初始化最近检索状态 |
| <code>acquire_session()</code> | InboundMessage | Session | 从内存缓存或 SQLite 恢复会话 |
| <code>prepare_context()</code> | Session、query_text、user_id | list[MemoryItem] | 调用 MemoryEngine 检索长期记忆 |
| <code>build_ctx()</code> | InboundMessage | BeforeTurnCtx | 编排 Session、记忆、插件和 EventBus |

一句话记忆：

~~~text
acquire_session：找到“以前聊了什么”
prepare_context：找到“长期记住了什么”
build_ctx：把这些信息组装成本轮上下文
~~~

---

## 十九、阅读这个文件时最重要的结论

1. <code>InboundMessage</code> 是本轮输入，包含聊天定位信息和用户问题。
2. <code>BeforeTurnPhase</code> 把简单输入升级成完整 <code>BeforeTurnCtx</code>。
3. Session 优先从 <code>_sessions</code> 内存缓存获取，未命中才查询 SQLite。
4. 本阶段只读取 <code>conversation_sessions</code>，不会保存本轮消息。
5. 检索文本由最近三条全部历史记录中的 user 消息，加当前问题组成。
6. 长期记忆最多取 8 条，覆盖五种长期记忆类型。
7. RRF 和 HyDE 在 MemoryEngine 中实现，不在 <code>before_turn.py</code> 中实现。
8. 插件通过 PhaseFrame slots 在不同阶段插入逻辑。
9. EventBus GATE 可以修改 Context，也可以返回 <code>None</code> 阻断本轮请求。
10. <code>last_retrieved</code> 等共享实例字段在并发消息下存在状态串线风险。
