---
title: AfterReasoningPhase：模型结果转换、回复加工与插件导出
created: 2026-08-21
updated: 2026-08-21
tags: [telegram-bot, pipeline, after-reasoning, outbound, plugin, event-bus]
description: 逐步讲解 AfterReasoningPhase 如何把 ReasonerResult 转换成 OutboundMessage 和 AfterReasoningCtx，通过 EventBus GATE 与 PhaseModule 插件修改回复、收集工具链、metadata 和 media，并说明 persist_messages 当前不写长期记忆及其与 Session 持久化的真实边界。
source:
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/agent/pipeline/phases/after_reasoning.py # 本文主要讲解对象
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/agent/core/types.py # ReasonerResult、OutboundMessage 与 AfterReasoningCtx
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/agent/pipeline/passive_turn.py # 阶段调用位置及后续流程
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/agent/core/event_bus.py # Typed GATE 处理机制
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/agent/lifecycle/phase.py # PhaseModuleRunner 与 slot 导出
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/tests/test_after_phases.py # 阶段测试与真实行为验证
  - Cursor AI 对话，2026-08-21
---

# AfterReasoningPhase：模型结果转换、回复加工与插件导出

> `AfterReasoningPhase` 位于 Reasoner 之后、消息真正发送之前。它把模型的 `ReasonerResult` 包装成 Telegram 可发送的 `OutboundMessage`，同时给 EventBus 和插件一次修改回复、添加 metadata 或 media 的机会。

## 一、它在 Pipeline 中的位置

```text
BeforeReasoningPhase
        │ BeforeReasoningCtx
        ▼
Reasoner.run_turn()
        │ ReasonerResult
        ▼
AfterReasoningPhase       ← 本文
        │ AfterReasoningCtx
        ▼
AfterTurnPhase
        │ 发送 Telegram 消息
        ▼
Session 追加与持久化
```

`PassiveTurnPipeline` 中的调用：

```python
result = await self.reasoner.run_turn(reasoning_ctx)

after_ctx = await self.after_reasoning.build_ctx(
    result=result,
    session=turn_ctx.session,
    chat_id=inbound_message.chat_id,
    user_id=inbound_message.user_id,
)
```

一句话区分：

```text
Reasoner：得到“模型回答了什么、调用过哪些工具”
AfterReasoningPhase：把回答加工成“系统准备发送什么”
AfterTurnPhase：真正执行发送和回合完成后的通知
```

---

## 二、输入与输出

### 2.1 输入一：`ReasonerResult`

```python
@dataclass
class ReasonerResult:
    content: str
    tool_calls: list[dict[str, Any]]
    finish_reason: str
```

示例：

```python
result = ReasonerResult(
    content="根据你的偏好，我推荐低酸的巴西黄波旁。",
    tool_calls=[
        {
            "id": "call_001",
            "type": "function",
            "function": {
                "name": "recall_memory",
                "arguments": '{"query":"用户的咖啡偏好"}',
            },
            "result": '{"ok":true,"data":{"items":[...]}}',
        },
        {
            "id": "call_002",
            "type": "function",
            "function": {
                "name": "fetch_messages",
                "arguments": '{"source_ref":"session:1:2#msg:3"}',
            },
            "result": '{"ok":true,"data":{"messages":[...]}}',
        },
    ],
    finish_reason="stop",
)
```

三个字段分别表示：

| 字段 | 作用 |
|---|---|
| `content` | 模型最终生成的文字 |
| `tool_calls` | 本轮实际执行过的模型工具调用和 guard 工具调用记录 |
| `finish_reason` | 本轮 Reasoner 的结束原因，如 `stop`、`early_stop`、`max_iterations` |

### 2.2 其他输入

```python
async def build_ctx(
    self,
    result: ReasonerResult,
    session: Session,
    chat_id: int,
    user_id: int,
) -> AfterReasoningCtx:
```

| 参数 | 作用 | 当前函数是否真正使用 |
|---|---|---|
| `result` | Reasoner 的最终结果 | 是 |
| `session` | 当前 Session | 当前 `build_ctx()` 内没有使用 |
| `chat_id` | 回复目标 Telegram Chat | 是 |
| `user_id` | 用于构造 `session_key` | 是 |

`session` 保留在签名中，说明这个阶段原本计划承载消息持久化，但当前实现没有在 `build_ctx()` 里使用它。

### 2.3 输出：`AfterReasoningCtx`

```python
@dataclass
class AfterReasoningCtx:
    reasoner_result: ReasonerResult
    outbound_message: OutboundMessage
    session_key: str = ""
    channel: str = "telegram"
    chat_id: str = ""
    reply: str = ""
    thinking: str | None = None
    tools_used: tuple[str, ...] = ()
    tool_chain: tuple[dict[str, Any], ...] = ()
    media: list[str] = []
    outbound_metadata: dict[str, Any] = {}
```

它同时保留三层数据：

```text
reasoner_result
    原始推理结果，含 content、tool_calls、finish_reason

outbound_message
    准备发送到 Telegram 的最小消息对象

reply / tools_used / tool_chain / metadata / media
    供生命周期处理器和插件加工的扩展数据
```

---

## 三、初始化 `__init__()`

```python
def __init__(
    self,
    store: MemoryStore,
    event_bus: EventBus | None = None,
    plugin_modules: Sequence[object] | None = None,
) -> None:
    self.store = store
    self.event_bus = event_bus or EventBus.get_instance()
    self.plugin_modules = list(plugin_modules or [])
```

### 3.1 三个依赖

| 字段 | 作用 |
|---|---|
| `store` | 长期记忆存储；当前类保存了它，但当前两个方法都没有真正用它写数据 |
| `event_bus` | 发出 `AfterReasoningCtx` Typed GATE |
| `plugin_modules` | 由 `PhaseModuleRunner` 调度的 after_reasoning 插件模块 |

生产环境接线：

```python
after_reasoning = AfterReasoningPhase(
    memory_store,
    event_bus=event_bus,
    plugin_modules=plugin_manager.after_reasoning_modules,
)
```

---

## 四、`build_ctx()` 完整流程

### 4.1 总流程图

```text
ReasonerResult
      │
      ▼
取出 result.content
      │
      ▼
创建 OutboundMessage
      │
      ▼
创建 AfterReasoningCtx
      │
      ▼
运行依赖 build_ctx 的 PhaseModule
      │
      ▼
EventBus.emit(AfterReasoningCtx)
      │
      ▼
运行依赖 emit/persist_* 的 PhaseModule
      │
      ▼
收集 outbound metadata 和 media
      │
      ▼
如果 reply 被修改，重建 OutboundMessage
      │
      ▼
返回 AfterReasoningCtx
```

### 4.2 从模型结果创建发送消息

```python
content = result.content

outbound_msg = OutboundMessage(
    chat_id=chat_id,
    content=content,
    format="text",
)
```

`OutboundMessage` 是冻结的数据类：

```python
@dataclass(frozen=True)
class OutboundMessage:
    chat_id: int
    content: str
    format: str = "text"
```

示例：

```python
result.content
# "根据你的偏好，我推荐巴西黄波旁。"

outbound_msg
# OutboundMessage(
#     chat_id=2002,
#     content="根据你的偏好，我推荐巴西黄波旁。",
#     format="text",
# )
```

`format="text"` 是固定写入的；当前 TelegramAdapter 的 `send()` 也只使用 `chat_id` 与 `content`。

### 4.3 创建 `AfterReasoningCtx`

```python
ctx = AfterReasoningCtx(
    reasoner_result=result,
    outbound_message=outbound_msg,
    session_key=f"{user_id}:{chat_id}",
    channel="telegram",
    chat_id=str(chat_id),
    reply=content,
    tools_used=tuple(
        call.get("function", {}).get("name", "")
        for call in result.tool_calls
        if call.get("function", {}).get("name")
    ),
    tool_chain=tuple(result.tool_calls),
)
```

以上面的 `ReasonerResult` 为例：

```python
ctx.session_key
# "1001:2002"

ctx.reply
# "根据你的偏好，我推荐低酸的巴西黄波旁。"

ctx.tools_used
# ("recall_memory", "fetch_messages")

ctx.tool_chain
# (
#   {完整的 recall_memory 调用记录},
#   {完整的 fetch_messages 调用记录},
# )
```

区别：

```text
tools_used
    只有工具名称，适合统计和快速判断

tool_chain
    保存完整调用记录，含 call id、参数、结果、运行状态等
```

如果同一个工具调用两次，`tools_used` 也会保留两次，不会去重。

### 4.4 创建 PhaseFrame

```python
plugin_runner = PhaseModuleRunner(
    self.plugin_modules,
    phase_name="after_reasoning",
)

frame = PhaseFrame(
    input=result,
    slots={
        "reasoning:ctx": ctx,
        "after_reasoning.build_ctx": True,
    },
)
```

此时：

| slot | 作用 |
|---|---|
| `reasoning:ctx` | 当前 `AfterReasoningCtx` 对象 |
| `after_reasoning.build_ctx` | 表示上下文和初始 OutboundMessage 已创建 |

然后：

```python
frame = await plugin_runner.run_ready(frame)
ctx = frame.slots.get("reasoning:ctx", ctx)
```

依赖 `after_reasoning.build_ctx` 的插件可以修改或替换 `reasoning:ctx`。

### 4.5 Typed GATE：`event_bus.emit(ctx)`

```python
emitted = await self.event_bus.emit(ctx)
if emitted is not None:
    ctx = emitted
```

通过 `@on_after_reasoning` 注册的插件最终会映射为：

```python
event_bus.on(
    AfterReasoningCtx,
    handler,
    priority=...,
)
```

典型用途：

```python
@on_after_reasoning(priority=10)
async def add_prefix(self, event):
    event.reply = "机器人回复：" + event.reply
    return event
```

变化：

```text
ctx.reply = "你好"
      ↓ handler
ctx.reply = "机器人回复：你好"
```

注意：这里和 BeforeReasoningPhase 不同。如果 handler 返回 `None`，当前代码不会设置 `abort`，而是继续使用原来的 `ctx`。

```text
AfterReasoning handler 返回新 ctx → 使用新 ctx
AfterReasoning handler 返回 None   → 保留旧 ctx，继续执行
```

因此这里的 GATE 在当前阶段不能通过返回 `None` 阻止消息发送。

### 4.6 添加后续锚点并再次运行插件

```python
frame.slots["reasoning:ctx"] = ctx
frame.slots["after_reasoning.emit"] = True
frame.slots["after_reasoning.persist_user"] = True
frame.slots["after_reasoning.persist_assistant"] = True

frame = await plugin_runner.run_ready(frame)
```

可用锚点：

| slot | 表面含义 | 当前真实行为 |
|---|---|---|
| `after_reasoning.emit` | Context 已经过 EventBus | 确实如此 |
| `after_reasoning.persist_user` | 用户消息持久化节点 | 这里只设置标志，没有执行持久化 |
| `after_reasoning.persist_assistant` | Assistant 消息持久化节点 | 这里只设置标志，没有执行持久化 |

后两个名称容易造成误解。它们当前只是让依赖这些名字的插件获得执行机会，不代表消息已写入数据库。

项目测试中的模块示例：

```python
class AfterReasoningSlotModule:
    slot = "sample.outbound_metadata"
    requires = ("after_reasoning.emit",)

    async def run(self, frame):
        frame.slots["outbound:metadata:sample"] = "metadata-from-slot"
        return frame
```

### 4.7 收集 Outbound Metadata

```python
ctx.outbound_metadata.update(
    collect_prefixed_slots(
        frame.slots,
        "outbound:metadata:",
    )
)
```

假设插件写入：

```python
frame.slots = {
    "outbound:metadata:model": "deepseek-v4-flash-ascend",
    "outbound:metadata:source": "coffee-plugin",
}
```

收集后：

```python
ctx.outbound_metadata == {
    "model": "deepseek-v4-flash-ascend",
    "source": "coffee-plugin",
}
```

这些 metadata 不在 `OutboundMessage` 中，也不会直接发送给 Telegram；后面会复制到 `AfterTurnCtx.extra_metadata`，供遥测插件观察。

### 4.8 收集 Media

```python
media_exports = collect_prefixed_slots(
    frame.slots,
    "outbound:media:",
)

for value in media_exports.values():
    if isinstance(value, str) and value.strip():
        ctx.media.append(value)
    elif isinstance(value, list):
        ctx.media.extend(
            str(item)
            for item in value
            if str(item).strip()
        )
```

插件可以写：

```python
frame.slots["outbound:media:chart"] = "/tmp/chart.png"
frame.slots["outbound:media:photos"] = [
    "/tmp/a.png",
    "/tmp/b.png",
]
```

得到：

```python
ctx.media == [
    "/tmp/chart.png",
    "/tmp/a.png",
    "/tmp/b.png",
]
```

但是当前 `AfterTurnPhase` 和 `TelegramAdapter.send()` 没有读取 `ctx.media`，所以这些 media 当前只被收集，并不会真正发送。这是扩展接口而不是已完成的媒体发送链路。

### 4.9 同步被插件修改的回复

```python
if ctx.reply != ctx.outbound_message.content:
    ctx.outbound_message = OutboundMessage(
        chat_id=chat_id,
        content=ctx.reply,
        format=ctx.outbound_message.format,
    )
    ctx.reasoner_result.content = ctx.reply
```

因为 `OutboundMessage` 是 frozen dataclass，不能直接写：

```python
ctx.outbound_message.content = ctx.reply  # 不允许
```

所以需要创建一个新对象。

示例：

```python
# EventBus 前
ctx.reply = "你好"
ctx.outbound_message.content = "你好"

# 插件修改
ctx.reply = "机器人回复：你好"

# 同步后
ctx.outbound_message = OutboundMessage(
    chat_id=2002,
    content="机器人回复：你好",
    format="text",
)
ctx.reasoner_result.content
# "机器人回复：你好"
```

这样保证：

```text
最终发送文本
= AfterReasoningCtx.reply
= OutboundMessage.content
= ReasonerResult.content
```

### 4.10 返回

```python
frame.slots["after_reasoning.collect_exports"] = True
frame.slots["after_reasoning.return"] = True
plugin_runner.warn_unresolved()
return ctx
```

这两个 slot 设置后没有再次调用 `run_ready()`，所以当前版本中，声明依赖下面锚点的插件不会在本次函数中执行：

```python
requires = ("after_reasoning.collect_exports",)
# 或
requires = ("after_reasoning.return",)
```

`warn_unresolved()` 会对仍未执行的插件输出 warning，但不会中断流程。

---

## 五、`persist_messages()` 当前到底做了什么

```python
async def persist_messages(
    self,
    session: Session,
    user_message: str,
    assistant_message: str,
    user_id: int,
    chat_id: int,
) -> list[MemoryItem]:
    """Raw turns are persisted by SessionStore, not the long-term vector pool."""
    return []
```

输入示例：

```python
memories = await phase.persist_messages(
    session=session,
    user_message="你好",
    assistant_message="你好，有什么可以帮你？",
    user_id=1001,
    chat_id=2002,
)
```

输出永远是：

```python
memories
# []
```

它当前不会：

- 调用 `MemoryStore`；
- 创建 `MemoryItem`；
- 写入 `memory_items`；
- 写入 `vec_items`；
- 写入 `conversation_sessions`。

`PassiveTurnPipeline` 虽然异步调度了它：

```python
asyncio.create_task(
    self.after_reasoning.persist_messages(...)
)
```

但任务目前只会快速返回空列表。

因此：

```python
new_memory_ids = []
```

当前也是固定空列表，并传给 `AfterTurnPhase`。

---

## 六、真正的 Session 持久化在哪里

真正的原始对话持久化发生在 `AfterTurnPhase.execute()` 完成之后：

```python
turn_ctx.session.messages.append({
    "role": "user",
    "content": inbound_message.content,
})

turn_ctx.session.messages.append({
    "role": "assistant",
    "content": result.content,
})

get_session_store().save(
    inbound_message.user_id,
    inbound_message.chat_id,
    turn_ctx.session.messages,
    last_consolidated=turn_ctx.session.last_consolidated,
)
```

数据库流向：

```text
用户原文 + Assistant 最终回复
             │
             ▼
       Session.messages
             │
             ▼
SessionStore.save()
             │
             ▼
conversation_sessions.messages_json
```

长期记忆则由后续 ConsolidationWorker 或显式 `memorize` 工具处理，详情参阅 [[learning_table]] 与 [[learning_default_memory_engine]]。

---

## 七、完整输入输出示例

### 7.1 输入

```python
result = ReasonerResult(
    content="你的职业是 Python 后端开发者。",
    tool_calls=[
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "recall_memory",
                "arguments": '{"query":"用户职业","memory_type":"profile"}',
            },
            "result": "...",
        },
        {
            "id": "call_2",
            "type": "function",
            "function": {
                "name": "fetch_messages",
                "arguments": '{"source_ref":"session:1:2#msg:8"}',
            },
            "result": "...",
        },
    ],
    finish_reason="stop",
)
```

### 7.2 插件操作

```text
EventBus handler：给回复加前缀“根据历史原文，”
PhaseModule：导出 metadata source=memory-grounded
```

### 7.3 输出

```python
AfterReasoningCtx(
    reasoner_result=ReasonerResult(
        content="根据历史原文，你的职业是 Python 后端开发者。",
        tool_calls=[...],
        finish_reason="stop",
    ),
    outbound_message=OutboundMessage(
        chat_id=2,
        content="根据历史原文，你的职业是 Python 后端开发者。",
        format="text",
    ),
    session_key="1:2",
    channel="telegram",
    chat_id="2",
    reply="根据历史原文，你的职业是 Python 后端开发者。",
    thinking=None,
    tools_used=("recall_memory", "fetch_messages"),
    tool_chain=(...),
    media=[],
    outbound_metadata={
        "source": "memory-grounded",
    },
)
```

---

## 八、所有 slot 汇总

| slot/prefix | 作用 |
|---|---|
| `reasoning:ctx` | 当前 `AfterReasoningCtx` |
| `after_reasoning.build_ctx` | Context 已构造 |
| `after_reasoning.emit` | Typed GATE 已执行 |
| `after_reasoning.persist_user` | 当前只是插件锚点，不代表已持久化 |
| `after_reasoning.persist_assistant` | 当前只是插件锚点，不代表已持久化 |
| `outbound:metadata:<name>` | 导出 Outbound metadata |
| `outbound:media:<name>` | 导出媒体路径字符串或列表 |
| `after_reasoning.collect_exports` | metadata/media 已收集的标记 |
| `after_reasoning.return` | 准备返回的标记 |

---

## 九、阅读时最容易误解的地方

### 9.1 这个阶段没有真正 persist

源码中有旧注释：

```python
# Persist user message and assistant message as memories
```

但紧接着没有对应实现。应以实际代码为准：`persist_messages()` 返回空列表，SessionStore 保存发生在 Pipeline 后面。

### 9.2 `MemoryStore` 当前没有被使用

构造函数要求传入 `MemoryStore` 并保存为 `self.store`，但当前类没有调用它。这是保留接口，不是当前运行链路中的数据库写入。

### 9.3 Media 目前不会发送

`ctx.media` 能被插件填充，但后续 Telegram 发送只调用：

```python
send_message(chat_id=message.chat_id, text=message.content)
```

因此媒体发送链路尚未接通。

### 9.4 最终发送内容可以被插件改写

`ReasonerResult.content` 并不一定就是最终用户收到的文本。`AfterReasoning` EventBus 或 PhaseModule 可以修改 `ctx.reply`，代码随后会同步修改 `OutboundMessage` 与 `ReasonerResult`。

---

## 十、一句话总结

```text
AfterReasoningPhase
= ReasonerResult → OutboundMessage 的转换器
+ 回复发送前的 Typed GATE
+ after_reasoning 插件调度器
+ 工具链/metadata/media 汇总器
```

当前真实链路：

```text
ReasonerResult.content
        ↓
OutboundMessage(content, chat_id, format="text")
        ↓
EventBus 与插件可修改 reply
        ↓
同步 OutboundMessage 和 ReasonerResult
        ↓
AfterReasoningCtx
        ↓
AfterTurnPhase
```
