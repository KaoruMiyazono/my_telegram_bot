---
title: AfterTurnPhase：回合提交事件、遥测观察与Telegram发送
created: 2026-08-21
updated: 2026-08-21
tags: [telegram-bot, pipeline, after-turn, telegram, event-bus, logging, plugin]
description: 逐步讲解 AfterTurnPhase 如何接收 AfterReasoningCtx，创建 TurnCommittedEvent，依次运行字符串 EventBus、PhaseModule 和 AfterTurnCtx Typed TAP，收集 telemetry 并通过 TelegramAdapter 发送消息，同时说明 ConversationLogger、Session 持久化与发送失败语义的真实先后关系。
source:
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/agent/pipeline/phases/after_turn.py # 本文主要讲解对象
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/agent/core/types.py # AfterTurnCtx、TurnCommittedEvent 与 OutboundMessage
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/agent/core/event_bus.py # 字符串事件与 Typed TAP
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/channels/telegram/adapter.py # Telegram 消息发送和重试
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/evaluation/conversation_logger.py # turn_committed 订阅与 JSONL 日志
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/agent/pipeline/passive_turn.py # AfterTurn 前后调用顺序与 Session 持久化
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/tests/test_after_phases.py # 事件和发送行为测试
  - Cursor AI 对话，2026-08-21
---

# AfterTurnPhase：回合提交事件、遥测观察与Telegram发送

> `AfterTurnPhase` 是用户收到回复之前的最后一个 Pipeline Phase：它广播“这一轮已经形成结果”的事件，运行回合后插件和遥测观察者，然后调用 TelegramAdapter 发送 `OutboundMessage`。

## 一、这个阶段负责什么

```text
AfterReasoningCtx
      │
      ▼
生成 TurnCommittedEvent
      │
      ▼
运行 after_turn PhaseModule
      │
      ▼
广播字符串事件 "turn_committed"
      │
      ▼
运行 fanout 后 PhaseModule
      │
      ▼
收集 telemetry
      │
      ▼
Typed TAP observe(AfterTurnCtx)
      │
      ▼
TelegramAdapter.send()
      │
      ▼
返回 None
```

它主要解决三件事：

1. 通知 ConversationLogger 等订阅者这一轮完成了；
2. 给插件提供回合完成后的统计、审计和遥测节点；
3. 真正把文本发到 Telegram。

它不负责：

- 调用 LLM；
- 执行工具；
- 生成回答文字；
- 直接保存 Session；
- 直接写长期记忆数据库。

---

## 二、在主 Pipeline 中的位置和真实顺序

```python
# Phase 4
after_ctx = await self.after_reasoning.build_ctx(...)

# 一个当前为空操作的后台任务
asyncio.create_task(
    self.after_reasoning.persist_messages(...)
)

# Phase 5
await self.after_turn.execute(
    ctx=after_ctx,
    user_id=inbound_message.user_id,
    new_memory_ids=[],
    inbound_content=inbound_message.content,
)

# AfterTurn 完成后，才追加和保存 Session
turn_ctx.session.messages.append(...)
get_session_store().save(...)
```

真实先后关系：

```text
AfterReasoningPhase
        ↓
AfterTurnPhase 创建事件、写日志队列、发送 Telegram
        ↓
Session.messages 追加本轮 user/assistant
        ↓
conversation_sessions 保存
        ↓
Consolidation / Invalidation 后台任务
```

这意味着 `turn_committed` 事件触发时，当前轮消息还没有写入 `conversation_sessions`。

---

## 三、初始化与 Adapter 延迟注入

### 3.1 构造函数

```python
def __init__(
    self,
    event_bus: EventBus,
    telegram_adapter: TelegramAdapter,
    plugin_modules: Sequence[object] | None = None,
) -> None:
    self.event_bus = event_bus
    self.telegram_adapter = telegram_adapter
    self.plugin_modules = list(plugin_modules or [])
```

### 3.2 `main.py` 为什么先传 `None`

```python
after_turn = AfterTurnPhase(
    event_bus,
    None,
    plugin_modules=plugin_manager.after_turn_modules,
)
```

此时 TelegramAdapter 还不能创建，因为 Adapter 需要完整 Pipeline：

```text
AfterTurnPhase 需要 TelegramAdapter
TelegramAdapter 又需要 PassiveTurnPipeline
```

项目通过延迟注入打破循环：

```python
pipeline = PassiveTurnPipeline(
    ...,
    after_turn=after_turn,
)

adapter = TelegramAdapter(
    token=settings.TG_BOT_TOKEN,
    pipeline=pipeline,
    proxy=settings.HTTP_PROXY,
)

after_turn.telegram_adapter = adapter
```

依赖图：

```text
先创建 AfterTurnPhase(adapter=None)
              │
              ▼
创建完整 Pipeline
              │
              ▼
用 Pipeline 创建 TelegramAdapter
              │
              ▼
把 Adapter 回填给 AfterTurnPhase
```

虽然类型注解写的是 `TelegramAdapter`，运行时实际允许暂时为 `None`。

---

## 四、`execute()` 输入与输出

```python
async def execute(
    self,
    ctx: AfterReasoningCtx,
    user_id: int,
    new_memory_ids: list[UUID],
    inbound_content: str = "",
) -> None:
```

| 参数 | 作用 |
|---|---|
| `ctx` | 上一阶段生成的回复、工具链、metadata 等 |
| `user_id` | 当前 Telegram 用户 ID |
| `new_memory_ids` | 本轮新增长期记忆 ID；当前主链路固定传空列表 |
| `inbound_content` | 当前用户输入原文 |

返回值始终是：

```python
None
```

最终消息对象仍由 Pipeline 通过：

```python
return after_ctx.outbound_message
```

返回给 TelegramAdapter 的上层处理函数。

---

## 五、第一步：创建 `turn_id`

```python
turn_id = str(uuid4())
```

示例：

```python
turn_id
# "787f36de-2a36-4d39-ad5c-a3cfcb1a8777"
```

它是项目为每一轮对话自己生成的唯一标识，不是 Telegram 的 `update_id`、`user_id` 或 `chat_id`。

```text
user_id：谁发的
chat_id：在哪个聊天中
turn_id：这一次问答本身的唯一编号
```

---

## 六、第二步：创建 `TurnCommittedEvent`

```python
event = TurnCommittedEvent(
    turn_id=turn_id,
    user_id=user_id,
    inbound_content=inbound_content,
    outbound_message=ctx.outbound_message,
    new_memory_ids=new_memory_ids,
)
```

结构：

```python
@dataclass
class TurnCommittedEvent:
    turn_id: str
    user_id: int
    inbound_content: str
    outbound_message: OutboundMessage
    new_memory_ids: list[UUID]
    timestamp: datetime = field(default_factory=datetime.utcnow)
```

具体示例：

```python
TurnCommittedEvent(
    turn_id="787f36de-2a36-4d39-ad5c-a3cfcb1a8777",
    user_id=1001,
    inbound_content="EventBus 是什么？",
    outbound_message=OutboundMessage(
        chat_id=2002,
        content="EventBus 是事件分发系统。",
        format="text",
    ),
    new_memory_ids=[],
    timestamp=datetime(...),
)
```

这里的 “Committed” 更接近“回复已经确定、准备进行回合后处理”，并不严格表示数据库事务已经完成，因为 Session 保存发生在后面。

---

## 七、第三步：创建 `AfterTurnCtx`

```python
turn_ctx = AfterTurnCtx(
    session_key=(
        ctx.session_key
        or f"{user_id}:{ctx.outbound_message.chat_id}"
    ),
    channel=ctx.channel or "telegram",
    chat_id=ctx.chat_id or str(ctx.outbound_message.chat_id),
    reply=ctx.outbound_message.content,
    tools_used=ctx.tools_used,
    thinking=ctx.thinking,
    will_dispatch=self.telegram_adapter is not None,
    extra_metadata=dict(ctx.outbound_metadata),
)
```

字段含义：

| 字段 | 作用 |
|---|---|
| `session_key` | 一般为 `user_id:chat_id` |
| `channel` | 当前渠道，默认 `telegram` |
| `chat_id` | 字符串形式 Chat ID |
| `reply` | 准备发送的回复文本 |
| `tools_used` | 本轮用过的工具名称 |
| `thinking` | 可选思考信息；当前 Reasoner 通常没有填充 |
| `will_dispatch` | 是否存在 TelegramAdapter |
| `extra_metadata` | 从 AfterReasoning 收集来的 metadata 副本 |

示例：

```python
AfterTurnCtx(
    session_key="1001:2002",
    channel="telegram",
    chat_id="2002",
    reply="EventBus 是事件分发系统。",
    tools_used=("recall_memory",),
    thinking=None,
    will_dispatch=True,
    extra_metadata={"source": "memory-grounded"},
)
```

`dict(ctx.outbound_metadata)` 创建浅拷贝，后续向 `turn_ctx.extra_metadata` 添加遥测字段，不会直接改动原字典。

---

## 八、第四步：创建 AfterTurn 插件 Frame

```python
plugin_runner = PhaseModuleRunner(
    self.plugin_modules,
    phase_name="after_turn",
)

frame = PhaseFrame(
    input=ctx,
    slots={
        "turn:ctx": turn_ctx,
        "turn:committed": event,
        "after_turn.build_work": True,
    },
)
```

三个初始 slot：

| slot | 内容 |
|---|---|
| `turn:ctx` | `AfterTurnCtx`，用于 typed telemetry |
| `turn:committed` | `TurnCommittedEvent` 对象 |
| `after_turn.build_work` | 表示本轮提交工作已经构造完成 |

然后运行依赖已经满足的插件：

```python
frame = await plugin_runner.run_ready(frame)
```

插件可以读取或修改这些对象，但此时字符串事件还没有广播。

---

## 九、第五步：广播字符串事件 `turn_committed`

```python
await self.event_bus.emit(
    "turn_committed",
    event=event,
)
```

这里使用的是 EventBus 的“字符串事件”系统：

```python
event_bus.subscribe(
    "turn_committed",
    handler,
)
```

EventBus 内部：

```python
if isinstance(event_or_type, str):
    await self._emit_string(event_or_type, **data)
    return None
```

它和下面的 Typed GATE/TAP 不同：

```text
字符串事件
    subscribe("turn_committed", handler)
    emit("turn_committed", event=event)

Typed GATE
    on(AfterReasoningCtx, handler)
    emit(after_reasoning_ctx)

Typed TAP
    observe(AfterTurnCtx, handler)
    await observe(after_turn_ctx)
```

### 9.1 ConversationLogger 在哪里订阅

应用启动时：

```python
conversation_logger = ConversationLogger()
await conversation_logger.start()
```

`start()` 中：

```python
self.event_bus.subscribe(
    "turn_committed",
    self._handle_turn_committed,
)
```

收到事件后，它转换成：

```python
turn_data = {
    "turn_id": event.turn_id,
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "user_id": event.user_id,
    "inbound_content": event.inbound_content,
    "outbound_message": {
        "chat_id": event.outbound_message.chat_id,
        "content": event.outbound_message.content,
        "format": event.outbound_message.format,
    },
    "new_memory_ids": [str(mid) for mid in event.new_memory_ids],
}
```

然后非阻塞放入队列：

```python
self._pending_writes.put_nowait(turn_data)
```

后台 `_write_loop()` 再写入：

```text
./data/evaluation/raw_conversations.jsonl
```

数据流：

```text
AfterTurnPhase
    │ emit("turn_committed")
    ▼
ConversationLogger._handle_turn_committed
    │ put_nowait
    ▼
asyncio.Queue
    │ 后台任务
    ▼
raw_conversations.jsonl
```

注意：Logger 用 `datetime.now(timezone.utc)` 重新生成日志时间，并没有直接使用 `event.timestamp`。

---

## 十、第六步：运行 fanout 后插件并收集 telemetry

事件广播完成后：

```python
frame.slots["after_turn.fanout_committed"] = True
frame = await plugin_runner.run_ready(frame)
```

测试中的真实插件示例：

```python
class AfterTurnTelemetryModule:
    slot = "sample.turn_telemetry"
    requires = ("after_turn.fanout_committed",)

    async def run(self, frame):
        frame.slots["turn:telemetry:sample"] = "telemetry-from-slot"
        return frame
```

随后：

```python
turn_ctx = frame.slots.get("turn:ctx", turn_ctx)

turn_ctx.extra_metadata.update(
    collect_prefixed_slots(
        frame.slots,
        "turn:telemetry:",
    )
)
```

收集结果：

```python
turn_ctx.extra_metadata == {
    "source": "memory-grounded",
    "sample": "telemetry-from-slot",
}
```

这类数据适合：

- 统计本轮用了哪些插件；
- 记录延迟和模型信息；
- 写审计日志；
- 发送监控指标；
- 评估工具调用质量。

它不会自动发送给 Telegram，也不会自动写入 SQLite。

---

## 十一、第七步：Typed TAP 观察 `AfterTurnCtx`

```python
observed = self.event_bus.observe(turn_ctx)
if inspect.isawaitable(observed):
    await observed
```

通过装饰器注册：

```python
@on_after_turn()
async def count_turn(self, event):
    self.context.kv_store.increment("turns")
```

`@on_after_turn` 被定义为 `HandlerType.TAP`：

```python
_get_or_create_handler(
    func,
    PluginEventType.AFTER_TURN,
    HandlerType.TAP,
    **options,
)
```

PluginManager 最终注册：

```python
event_bus.observe(
    AfterTurnCtx,
    handler,
    priority=...,
)
```

TAP 的语义是旁路观察：

```text
AfterTurnCtx
   ├──统计插件
   ├──日志插件
   ├──审计插件
   └──监控插件
```

处理器的返回值不会替换 `turn_ctx`，返回 `None` 也不会阻断发送；异常会被 EventBus 捕获并记录 warning。

`inspect.isawaitable()` 是为了判断 `observe(turn_ctx)` 返回的是不是可等待对象。当前 EventBus 在触发模式下返回 `_observe_event()` 协程，因此这里会执行 `await`。

---

## 十二、第八步：通过 TelegramAdapter 发送消息

```python
if self.telegram_adapter is not None:
    await self.telegram_adapter.send(
        ctx.outbound_message
    )
```

发送的数据：

```python
OutboundMessage(
    chat_id=2002,
    content="EventBus 是一个事件分发系统。",
    format="text",
)
```

Adapter 最终调用：

```python
await self.application.bot.send_message(
    chat_id=message.chat_id,
    text=message.content,
)
```

`format` 当前没有被 `send()` 使用。

### 12.1 重试策略

最多尝试三次：

```python
max_retries = 3
```

遇到 Telegram 限流：

```python
except RetryAfter as e:
    delay = e.retry_after + 1.0
```

遇到超时或网络错误：

```python
except (TimedOut, NetworkError):
    delay = 2 ** attempt
```

等待时间大致是：

```text
第一次失败后：1 秒
第二次失败后：2 秒
第三次失败：不再等待，记录失败日志
```

如果三次全部失败，`send()` 只记录 error，不抛异常，仍然返回 `None`。所以 `AfterTurnPhase.execute()` 无法仅通过返回值确认消息是否真的送达 Telegram。

---

## 十三、最后的 slot 与返回

```python
frame.slots["after_turn.dispatch"] = True
frame.slots["after_turn.return"] = True
plugin_runner.warn_unresolved()
```

| slot | 含义 |
|---|---|
| `after_turn.dispatch` | 已经执行 Adapter 发送步骤；不严格代表 Telegram 成功送达 |
| `after_turn.return` | 阶段准备结束 |

和其他 Phase 类似，这两个 slot 设置后没有再次调用 `run_ready()`，因此依赖它们的 PhaseModule 当前不会在本次 `execute()` 中执行，只会在 `warn_unresolved()` 中留下未解析 warning。

函数没有显式 `return`，所以返回：

```python
None
```

---

## 十四、三套扩展机制在这里如何配合

### 14.1 PhaseModuleRunner

```python
plugin_manager.after_turn_modules
```

根据 slot 依赖插在不同步骤之间，适合结构化生成 telemetry。

### 14.2 字符串 EventBus

```python
emit("turn_committed", event=event)
```

按事件名称广播，当前典型消费者是 `ConversationLogger`。

### 14.3 Typed TAP EventBus

```python
observe(turn_ctx)
```

按 `AfterTurnCtx` 类型找到 `@on_after_turn` 处理器，适合统计和旁路观察。

```text
PhaseModuleRunner
    回答：依赖满足后，现在轮到哪个模块？

字符串 EventBus
    回答：谁订阅了 "turn_committed" 这个名字？

Typed TAP
    回答：谁想观察 AfterTurnCtx 这种生命周期对象？
```

---

## 十五、数据库与文件操作边界

### 15.1 AfterTurnPhase 本身不操作数据库

`after_turn.py` 中没有 SQL，也没有 `SessionStore.save()`。

### 15.2 ConversationLogger 异步写 JSONL

字符串事件会导致 ConversationLogger 把数据加入队列，稍后写入：

```text
data/evaluation/raw_conversations.jsonl
```

这是评估日志，不是 Session 数据库。

### 15.3 Session 在阶段结束后保存

`PassiveTurnPipeline` 在 `await after_turn.execute(...)` 之后执行：

```python
turn_ctx.session.messages.append(user_message)
turn_ctx.session.messages.append(assistant_message)
get_session_store().save(...)
```

对应数据库：

```text
conversation_sessions
└── messages_json：保存完整 Session 消息数组
```

表结构请参阅 [[learning_table]]，SessionStore 请参阅 [[learning_session_store]]。

---

## 十六、完整运行示例

### 16.1 输入

```python
ctx = AfterReasoningCtx(
    reasoner_result=ReasonerResult(
        content="你喜欢低酸咖啡。",
        tool_calls=[...],
        finish_reason="stop",
    ),
    outbound_message=OutboundMessage(
        chat_id=2002,
        content="你喜欢低酸咖啡。",
    ),
    session_key="1001:2002",
    channel="telegram",
    chat_id="2002",
    reply="你喜欢低酸咖啡。",
    tools_used=("recall_memory", "fetch_messages"),
    outbound_metadata={"grounded": True},
)
```

调用：

```python
await after_turn.execute(
    ctx=ctx,
    user_id=1001,
    new_memory_ids=[],
    inbound_content="你还记得我的咖啡偏好吗？",
)
```

### 16.2 执行过程

```text
生成 turn_id
    ↓
创建 TurnCommittedEvent
    ↓
ConversationLogger 收到事件并入队
    ↓
PhaseModule 导出 telemetry
    ↓
@on_after_turn TAP 插件执行统计
    ↓
TelegramAdapter.send_message(chat_id=2002, text="你喜欢低酸咖啡。")
    ↓
AfterTurnPhase 返回 None
    ↓
Pipeline 再把本轮问答写入 SessionStore
```

### 16.3 JSONL 日志示例

```json
{
  "turn_id": "787f36de-2a36-4d39-ad5c-a3cfcb1a8777",
  "timestamp": "2026-08-21T10:30:00+00:00",
  "user_id": 1001,
  "inbound_content": "你还记得我的咖啡偏好吗？",
  "outbound_message": {
    "chat_id": 2002,
    "content": "你喜欢低酸咖啡。",
    "format": "text"
  },
  "new_memory_ids": []
}
```

---

## 十七、阅读代码时要特别注意的设计细节

### 17.1 先广播 committed，再发送 Telegram

当前顺序是：

```text
emit("turn_committed")
        ↓
observe(AfterTurnCtx)
        ↓
TelegramAdapter.send()
```

因此日志可能已经记录“准备发送的回复”，但 Telegram 发送随后失败。这里的 committed 不是严格的“用户确认收到”。

### 17.2 先发送 Telegram，再保存 Session

Session 保存发生在 `AfterTurnPhase` 返回之后。如果进程恰好在发送成功后、SessionStore.save() 前崩溃，用户可能已收到回复，但数据库没有保存这一轮。

### 17.3 `will_dispatch=True` 不等于一定发送成功

它只表示：

```python
self.telegram_adapter is not None
```

不代表 Telegram API 最终成功，也不代表用户已经收到。

### 17.4 `new_memory_ids` 当前通常为空

主链路明确写着：

```python
new_memory_ids = []
```

因为 `persist_messages()` 当前为空操作。因此 ConversationLogger 中的 `new_memory_ids` 当前通常是：

```json
[]
```

### 17.5 `ctx.media` 没有进入 AfterTurn

虽然 AfterReasoning 可以收集 media，但 `AfterTurnCtx` 没有 media 字段，Adapter 也只发文字。因此当前只能发送文本。

### 17.6 当前生产插件可能为空

项目测试提供了 `after_turn_modules()` 和 `@on_after_turn` 示例，但本地生产 `plugins/` 下没有发现实际插件文件。因此正常运行时，PhaseModule 和 Typed TAP 很可能没有处理器；字符串事件仍有 ConversationLogger 订阅者。

---

## 十八、一句话总结

```text
AfterTurnPhase
= 回合完成事件广播器
+ after_turn 插件调度器
+ Typed TAP 遥测入口
+ Telegram 文本发送器
```

最关键的顺序：

```text
创建 TurnCommittedEvent
        ↓
ConversationLogger 入队
        ↓
插件/遥测观察
        ↓
Telegram 发送
        ↓
AfterTurn 返回
        ↓
Pipeline 保存 Session
```
