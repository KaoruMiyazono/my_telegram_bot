# telegram-bot 项目学习笔记

## `EventBus`：模块之间的事件通知与生命周期扩展中心

`main.py` 中的代码：

```python
event_bus = EventBus.get_instance()
```

对应实现文件：

```text
agent/core/event_bus.py
```

Event Bus 可以理解成一个“程序内部广播站”：

```text
事件生产者
  └── 发布事件：“一轮对话完成了”
                ↓
             EventBus
                ↓
      ┌─────────┼──────────┐
      ↓         ↓          ↓
 Conversation  插件 A     插件 B
 Logger        记录指标    修改上下文
```

事件发布者不需要知道有哪些接收者，也不需要直接 import 和调用它们。接收者提前向 EventBus 注册，事件发生时由 EventBus 依次通知。

---

## 一、为什么需要 Event Bus

假设没有 EventBus，`AfterTurnPhase` 想在一轮对话完成后同时通知：

- `ConversationLogger` 写评测日志。
- 统计插件记录耗时。
- 其他插件收集工具使用情况。

它可能需要直接写成：

```python
conversation_logger.handle(event)
metrics_plugin.handle(event)
analytics_plugin.handle(event)
```

这样会产生问题：

```text
AfterTurnPhase
  ├── 依赖 ConversationLogger
  ├── 依赖 MetricsPlugin
  ├── 依赖 AnalyticsPlugin
  └── 每增加一个功能都需要修改 AfterTurnPhase
```

使用 EventBus 后：

```python
await event_bus.emit("turn_committed", event=event)
```

`AfterTurnPhase` 只负责宣布“一轮已经完成”，不用知道谁在监听。

接收者自己注册：

```python
event_bus.subscribe("turn_committed", handler)
```

依赖关系变成：

```text
AfterTurnPhase ──发布──→ EventBus
                            ↑
ConversationLogger ──订阅──┘
插件               ──订阅──┘
```

这叫解耦：发布者和接收者不直接依赖彼此。

---

## 二、这个项目有三种事件机制

`EventBus` 不是只有一种 publish/subscribe，它包含三套机制：

| 机制 | 注册方式 | 触发方式 | 能否影响主流程 |
|---|---|---|---|
| 字符串事件 | `subscribe("名字", handler)` | `await emit("名字", **data)` | 不能，主要用于广播通知 |
| Typed GATE | `on(CtxType, handler, priority=...)` | `await emit(ctx)` | 能，可以修改、替换或阻断 Context |
| Typed TAP | `observe(CtxType, handler, priority=...)` | `await observe(ctx)` | 不能，主要用于旁观和遥测 |

可以这样记：

```text
字符串事件 = 广播：“某件事发生了”

GATE = 关卡：“允许修改，也可以阻止流程继续”

TAP = 旁路观察：“看看发生了什么，但不改变主流程”
```

### 三种机制的路由图

```text
EventBus
│
├── 字符串事件
│     subscribe("turn_committed", handler)
│     emit("turn_committed", event=event)
│
├── Typed GATE
│     on(BeforeTurnCtx, handler, priority=10)
│     emit(before_turn_ctx)
│
└── Typed TAP
      observe(AfterTurnCtx, handler, priority=10)   注册
      await observe(after_turn_ctx)                 触发
```

---

## 三、`_TypedHandler`：带类型和优先级的处理器

```python
@dataclass
class _TypedHandler:
    ctx_type: type
    handler: Callable
    priority: int = 0
```

它包装一个 Typed GATE 或 TAP 处理器。

| 字段 | 作用 |
|---|---|
| `ctx_type` | 处理器关心的 Context 类型。 |
| `handler` | 真正被调用的同步或异步函数。 |
| `priority` | 执行优先级，数字越大越早执行。 |

输入示例：

```python
typed_handler = _TypedHandler(
    ctx_type=BeforeTurnCtx,
    handler=check_permission,
    priority=10,
)
```

结果是一个保存注册信息的对象；它本身不会执行 `check_permission`。

类名前面的下划线表示它是 EventBus 内部实现细节，外部通常不直接使用。

---

## 四、`EventBus.__init__()`：创建三组处理器容器

原始代码：

```python
def __init__(self) -> None:
    self._subscribers: dict[str, list[Callable]] = defaultdict(list)
    self._gate_handlers: dict[type, list[_TypedHandler]] = defaultdict(list)
    self._tap_handlers: dict[type, list[_TypedHandler]] = defaultdict(list)
```

创建三个字典：

```text
_subscribers
  └── 字符串事件名 → handler 列表

_gate_handlers
  └── Context 类型 → GATE handler 列表

_tap_handlers
  └── Context 类型 → TAP handler 列表
```

`defaultdict(list)` 的作用是：访问一个尚不存在的键时自动创建空列表。

例如：

```python
self._subscribers["turn_committed"].append(handler)
```

即使 `turn_committed` 第一次出现，也不需要提前初始化列表。

输入：无。

输出：无显式返回值；构造流程得到一个内部容器为空的 `EventBus` 对象。

创建方式有两种：

```python
EventBus.get_instance()  # 获取全局共享单例
EventBus()               # 创建一个独立实例，测试中常用
```

---

## 五、`get_instance()`：获取全局单例

原始代码：

```python
class EventBus:
    _instance: "EventBus | None" = None

    @classmethod
    def get_instance(cls) -> "EventBus":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
```

### 代码的作用

保证当前 Python 进程中的主要组件拿到同一个 EventBus。

第一次调用：

```text
EventBus._instance is None
  ↓
创建 EventBus()
  ↓
保存到 EventBus._instance
  ↓
返回该对象
```

之后调用：

```text
EventBus._instance 已存在
  ↓
直接返回同一个对象
```

输入和输出示例：

```python
a = EventBus.get_instance()
b = EventBus.get_instance()

assert a is b
```

### 为什么 `main.py` 使用单例

`main.py`：

```python
event_bus = EventBus.get_instance()
```

`ConversationLogger` 内部也执行：

```python
self.event_bus = EventBus.get_instance()
```

因为两者得到同一个对象：

```text
main.py 的 event_bus ───────┐
                            ├── 同一个 EventBus
ConversationLogger.event_bus┘
```

所以 Logger 注册的处理器，可以收到 Pipeline 发布的事件。

如果双方分别执行 `EventBus()`，它们会得到两个独立广播站，订阅和发布无法相遇。

---

## 六、字符串事件：`subscribe()`

原始代码：

```python
def subscribe(self, event_type: str, handler: Callable) -> None:
    self._subscribers[event_type].append(handler)
```

### 代码的作用

为一个字符串事件名注册处理器。

输入示例：

```python
def handle_turn(event):
    print(event.turn_id)

event_bus.subscribe("turn_committed", handle_turn)
```

注册后的内部结构：

```python
{
    "turn_committed": [handle_turn]
}
```

返回值：

```python
None
```

这个函数只完成注册，不会立刻调用 handler。

### 同一个事件可以有多个订阅者

```python
event_bus.subscribe("turn_committed", write_log)
event_bus.subscribe("turn_committed", record_metrics)
```

内部结构：

```text
turn_committed
  ├── write_log
  └── record_metrics
```

执行顺序是注册顺序。

---

## 七、字符串事件：`emit("事件名", **data)`

调用示例：

```python
await event_bus.emit(
    "turn_committed",
    event=turn_committed_event,
)
```

`emit()` 发现第一个参数是字符串后执行：

```python
await self._emit_string(event_or_type, **data)
return None
```

### `_emit_string()` 如何实现

```python
async def _emit_string(self, event_type: str, **data: Any) -> None:
    handlers = self._subscribers.get(event_type, [])
    for handler in handlers:
        try:
            result = handler(**data)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            logger.warning(...)
```

执行流程：

```text
emit("turn_committed", event=event)
  ↓
查找 _subscribers["turn_committed"]
  ↓
按注册顺序遍历 handler
  ↓
调用 handler(event=event)
  ↓
返回 coroutine？
  ├── 是 → await
  └── 否 → 继续
  ↓
处理下一个 handler
```

### 同步与异步处理器都支持

```python
def sync_handler(value: int):
    print("sync", value)

async def async_handler(value: int):
    await save_async(value)

bus.subscribe("test_event", sync_handler)
bus.subscribe("test_event", async_handler)

await bus.emit("test_event", value=42)
```

执行结果：

```text
sync_handler(value=42)
async_handler(value=42)
```

`emit()` 返回：

```python
None
```

订阅者的返回值不会被收集，也不会传给下一个订阅者。

### handler 参数必须与 data 对得上

发布：

```python
await bus.emit("test_event", value=42)
```

处理器应该可以接收：

```python
def handler(value):
    ...
```

或者：

```python
def handler(**data):
    ...
```

如果参数不匹配会产生异常，EventBus 会记录 Warning，然后继续执行其他订阅者。

---

## 八、项目中的字符串事件实例：`turn_committed`

### 注册方：ConversationLogger

启动时：

```python
self.event_bus.subscribe(
    "turn_committed",
    self._handle_turn_committed,
)
```

### 发布方：AfterTurnPhase

一轮回复完成后：

```python
await self.event_bus.emit(
    "turn_committed",
    event=event,
)
```

### 完整链路

```text
PassiveTurnPipeline
  ↓
AfterTurnPhase.execute()
  ↓ 创建 TurnCommittedEvent
event_bus.emit("turn_committed", event=event)
  ↓
EventBus 查找订阅者
  ↓
ConversationLogger._handle_turn_committed(event)
  ↓
把日志数据放入 asyncio.Queue
  ↓
后台任务写入 raw_conversations.jsonl
```

`ConversationLogger` 的回调只把数据快速放进队列，不在 EventBus handler 中直接执行较慢的文件写入。

这是因为字符串事件处理器是依次 await 的。如果某个 handler 很慢，`emit()` 和当前 Pipeline 也会等待它。

---

## 九、Typed GATE：`on()` 注册处理器

原始代码：

```python
def on(
    self,
    ctx_type: type,
    handler: Callable,
    *,
    priority: int = 0,
) -> None:
    self._append_typed_handler(
        self._gate_handlers,
        ctx_type,
        handler,
        priority,
    )
```

### GATE 是什么

GATE 是生命周期关卡。Context 在进入下一阶段前依次经过多个 handler：

```text
原始 Context
  ↓
高优先级 GATE
  ↓ 可能修改或替换
中优先级 GATE
  ↓
低优先级 GATE
  ↓
最终 Context
```

handler 可以：

- 修改当前 Context。
- 返回另一个 Context。
- 返回 `None` 阻断后续链路。

### 注册输入示例

```python
def add_hint(ctx: BeforeTurnCtx) -> BeforeTurnCtx:
    ctx.extra_hints.append("请使用简洁中文回答")
    return ctx

event_bus.on(
    BeforeTurnCtx,
    add_hint,
    priority=10,
)
```

输出：

```python
None
```

注册后的内部状态类似：

```text
_gate_handlers[BeforeTurnCtx]
  └── _TypedHandler(add_hint, priority=10)
```

---

## 十、Typed GATE：`emit(ctx)` 执行链

当 `emit()` 收到的不是字符串时，会进入 Typed GATE 分支：

```python
event = event_or_type
current = event

for item in self._matching_handlers(
    self._gate_handlers,
    type(event),
):
    result = item.handler(current)
    if asyncio.iscoroutine(result):
        result = await result
    if result is None:
        return None
    current = result

return current
```

### 输入输出示例

定义 Context：

```python
@dataclass
class GateCtx:
    value: int
    trace: list[str]
```

注册两个 handler：

```python
def first(ctx: GateCtx):
    ctx.trace.append("first")
    ctx.value += 3
    return ctx

async def later(ctx: GateCtx):
    ctx.trace.append("later")
    ctx.value *= 2
    return ctx

bus.on(GateCtx, later, priority=0)
bus.on(GateCtx, first, priority=10)
```

触发：

```python
result = await bus.emit(
    GateCtx(value=2, trace=[])
)
```

执行过程：

```text
初始 value = 2
  ↓ priority=10 的 first
value = 2 + 3 = 5
trace = ["first"]
  ↓ priority=0 的 later
value = 5 × 2 = 10
trace = ["first", "later"]
```

输出：

```python
GateCtx(
    value=10,
    trace=["first", "later"],
)
```

### GATE handler 可以返回新对象

下一位 handler 接收的不是固定原对象，而是上一位 handler 的返回值：

```text
handler A(current) → result A
                        ↓
                  current = result A
                        ↓
handler B(current) → result B
```

所以它既支持原地修改，也支持返回一个新的 Context。

---

## 十一、GATE 如何阻断流程

如果 handler 返回：

```python
None
```

EventBus 立即停止执行后续 GATE，并返回 `None`。

示例：

```python
def reject_blocked_user(ctx: BeforeTurnCtx):
    if ctx.inbound_message.user_id in BLOCKED_USERS:
        return None
    return ctx
```

流程：

```text
BeforeTurnCtx
  ↓
reject_blocked_user
  ├── 普通用户 → 返回 ctx → 继续
  └── 禁止用户 → 返回 None → 中断 GATE 链
```

`BeforeTurnPhase` 中：

```python
emitted = await self.event_bus.emit(ctx)
if emitted is None:
    ctx.abort = True
    if not ctx.abort_reply:
        ctx.abort_reply = "请求已被生命周期处理器阻断。"
    return ctx
```

因此 Typed GATE 的 `None` 会转换成 Pipeline 的 abort。

`BeforeReasoningPhase` 也使用相同模式。

---

## 十二、Typed TAP：`observe()` 的两种调用方式

`observe()` 是一个重载式接口，根据是否传入 `handler` 执行两种行为。

```python
def observe(
    self,
    event_or_type: Any,
    handler: Callable | None = None,
    *,
    priority: int = 0,
) -> Any:
    if handler is not None:
        # 注册
        ...
        return None
    # 触发
    return self._observe_event(event_or_type)
```

### 方式 1：注册 TAP

```python
event_bus.observe(
    AfterTurnCtx,
    collect_metrics,
    priority=10,
)
```

因为传入了 `handler`，它把处理器加入 `_tap_handlers`，返回 `None`。

### 方式 2：触发 TAP

```python
await event_bus.observe(after_turn_ctx)
```

因为没有传 `handler`，返回 `_observe_event()` 创建的 coroutine；调用者需要 `await`。

这两个写法很像，理解时要看第二个位置有没有 handler：

```text
observe(Context类型, handler) → 注册
observe(Context对象)          → 触发
```

---

## 十三、Typed TAP：`_observe_event()`

原始逻辑：

```python
async def _observe_event(self, event: Any) -> None:
    for item in self._matching_handlers(
        self._tap_handlers,
        type(event),
    ):
        try:
            result = item.handler(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            logger.warning(...)
```

### 输入输出示例

```python
seen = []

def collect(ctx: AfterTurnCtx):
    seen.append(ctx.reply)

bus.observe(AfterTurnCtx, collect)

await bus.observe(
    AfterTurnCtx(
        session_key="42:7",
        channel="telegram",
        chat_id="7",
        reply="你好",
        tools_used=(),
        thinking=None,
        will_dispatch=True,
    )
)
```

结果：

```python
seen == ["你好"]
```

`await bus.observe(ctx)` 自身返回：

```python
None
```

TAP handler 的返回值被忽略，所以它不能通过返回另一个 Context 改变主流程。

### TAP 的用途

适合：

- 日志记录。
- 指标统计。
- tracing。
- 观察调用了哪些工具。
- 收集一轮推理信息。

不适合：

- 拒绝请求。
- 修改下一阶段实际使用的 Context。
- 控制 Pipeline 是否继续。

---

## 十四、GATE 和 TAP 的区别

| 对比项 | GATE | TAP |
|---|---|---|
| 注册 | `on(Type, handler)` | `observe(Type, handler)` |
| 触发 | `await emit(ctx)` | `await observe(ctx)` |
| handler 返回值 | 会成为下一个 handler 的输入 | 被忽略 |
| 能否修改 Context | 可以 | 不应依赖修改影响主流程 |
| 能否阻断 | 返回 `None` 可以阻断 | 不可以 |
| 典型阶段 | BeforeTurn、BeforeReasoning、PromptRender、BeforeStep | AfterStep、AfterTurn、工具调用观察 |
| 主要用途 | 验证、改写、拦截、注入提示 | 日志、遥测、审计、统计 |

图示：

```text
GATE

ctx ─→ handler A ─→ 修改后的 ctx ─→ handler B ─→ 最终 ctx
          │
          └─ 返回 None ─→ 停止


TAP

                    ┌─→ observer A
ctx ────────────────┼─→ observer B
                    └─→ observer C

主流程继续使用自己的 ctx，不接收 observer 返回值
```

---

## 十五、优先级是怎样实现的

注册 Typed handler 时调用：

```python
def _append_typed_handler(...):
    handlers = target[ctx_type]
    handlers.append(...)
    handlers.sort(key=lambda h: -h.priority)
```

使用负数排序键，优先级越大越靠前：

```text
priority=100 → 最先执行
priority=10
priority=0
priority=-10 → 最后执行
```

输入示例：

```python
bus.on(MyCtx, handler_a, priority=0)
bus.on(MyCtx, handler_b, priority=100)
bus.on(MyCtx, handler_c, priority=10)
```

内部顺序：

```text
handler_b → handler_c → handler_a
```

相同 priority 时，Python 的稳定排序通常保留注册先后顺序。

字符串订阅者没有 priority 参数，只按注册顺序执行。

---

## 十六、类型匹配 `_matching_handlers()`

```python
if issubclass(ctx_type, registered_type):
    matched.extend(handlers)
```

处理器不只匹配完全相同的类型，也匹配父类。

例如：

```python
class PipelineContext:
    ...

class BeforeTurnCtx(PipelineContext):
    ...
```

注册：

```python
bus.on(PipelineContext, observe_all_pipeline_contexts)
```

发布：

```python
await bus.emit(before_turn_ctx)
```

因为：

```python
issubclass(BeforeTurnCtx, PipelineContext) is True
```

父类处理器也会被匹配。

这个特性允许插件注册一个通用 handler 观察或处理多个子类型。

匹配完成后还会再次按 priority 从高到低排序，保证来自不同注册类型的处理器也遵循全局优先级。

---

## 十七、异常处理：fail-open

三种事件机制都捕获单个 handler 的异常：

```python
try:
    ...
except Exception as e:
    logger.warning(...)
```

一个 handler 失败后，其他 handler 仍然继续。

测试示例：

```python
def failing_handler():
    raise RuntimeError("handler error")

def working_handler():
    results.append("ok")

bus.subscribe("error_event", failing_handler)
bus.subscribe("error_event", working_handler)

await bus.emit("error_event")
```

输出结果：

```python
results == ["ok"]
```

这种策略叫 fail-open：扩展、日志或观察器的故障尽量不让整个 Bot 回复链崩溃。

需要区分：

```text
handler 抛异常
→ 记录 Warning，继续其他 handler

GATE handler 主动返回 None
→ 这是明确阻断，停止 GATE 链
```

---

## 十八、同步与异步 handler 的执行方式

每个 handler 都先正常调用：

```python
result = handler(...)
```

然后判断：

```python
if asyncio.iscoroutine(result):
    result = await result
```

所以支持：

```python
def sync_handler(ctx):
    return ctx

async def async_handler(ctx):
    await do_something()
    return ctx
```

### 处理器是顺序执行，不是并行执行

```text
handler A 完成
  ↓
handler B 完成
  ↓
handler C 完成
```

代码没有使用：

```python
asyncio.gather(...)
```

因此高优先级异步 handler 没完成前，低优先级 handler 不会开始。

优点：

- GATE 修改顺序确定。
- 容易调试。
- 后一个 handler 可以使用前一个 handler 的结果。

代价：

- 某个 handler 很慢会增加 `emit()` 总耗时。
- 同步 handler 执行阻塞操作会直接阻塞事件循环。

---

## 十九、插件是怎样注册到 EventBus 的

插件通过装饰器声明生命周期 handler：

```python
@on_before_turn(priority=10)
def before_turn(self, ctx):
    ...
    return ctx
```

装饰器会登记元数据，`PluginManager._bind_handlers()` 加载插件时根据类型绑定：

```python
if md.handler_type == HandlerType.TAP:
    self._event_bus.observe(
        ctx_type,
        bound,
        priority=md.priority,
    )
else:
    self._event_bus.on(
        ctx_type,
        bound,
        priority=md.priority,
    )
```

插件装饰器与类型：

| 插件装饰器 | Context | EventBus 类型 |
|---|---|---|
| `@on_before_turn` | `BeforeTurnCtx` | GATE |
| `@on_before_reasoning` | `BeforeReasoningCtx` | GATE |
| `@on_prompt_render` | `PromptRenderCtx` | GATE |
| `@on_before_step` | `BeforeStepCtx` | GATE |
| `@on_after_reasoning` | `AfterReasoningCtx` | GATE |
| `@on_after_step` | `AfterStepCtx` | TAP |
| `@on_after_turn` | `AfterTurnCtx` | TAP |

所以插件开发者通常不会直接调用 `event_bus.on()`，而是使用装饰器，PluginManager 再完成真正注册。

---

## 二十、EventBus 在完整 Pipeline 中的位置

```text
Telegram 消息
  ↓
BeforeTurnCtx
  ↓ emit(ctx) ───────────────→ GATE 插件可修改/阻断
  ↓
BeforeReasoningCtx
  ↓ emit(ctx) ───────────────→ GATE 插件可修改/阻断
  ↓
PromptRenderCtx
  ↓ emit(ctx) ───────────────→ GATE 插件可修改 Prompt 内容
  ↓
BeforeStepCtx
  ↓ emit(ctx) ───────────────→ GATE 插件可提前停止推理
  ↓
LLM / Tool Calls
  ├── observe(BeforeToolCallCtx) → TAP 观察工具调用前
  └── observe(AfterToolResultCtx)→ TAP 观察工具结果
  ↓
AfterStepCtx
  ↓ observe(ctx) ─────────────→ TAP 收集推理遥测
  ↓
AfterReasoningCtx
  ↓ emit(ctx) ───────────────→ GATE 可修改最终回复
  ↓
AfterTurn
  ├── emit("turn_committed") ─→ ConversationLogger 等字符串订阅者
  └── observe(AfterTurnCtx) ───→ TAP 插件记录最后信息
  ↓
Telegram 发送回复
```

---

## 二十一、`main.py` 这一行到底完成了什么

```python
event_bus = EventBus.get_instance()
```

这行完成：

- 第一次调用时创建全局 EventBus。
- 后续调用时返回已有对象。
- 为 Pipeline、Reasoner、PluginManager 和 ConversationLogger 提供同一个事件中心。

这行没有：

- 发布任何事件。
- 注册任何处理器。
- 启动后台线程。
- 创建消息队列。
- 操作数据库。

后续调用才会注册或触发事件，例如：

```python
conversation_logger.start()
# 内部 subscribe("turn_committed", ...)

plugin_manager.load_all()
# 内部 on(...) 或 observe(...)

await event_bus.emit(ctx)
# 触发 GATE
```

依赖关系：

```text
main.py
  ↓ EventBus.get_instance()
共享 event_bus
  ├── Reasoner
  ├── PluginManager
  ├── BeforeTurnPhase
  ├── BeforeReasoningPhase
  ├── AfterReasoningPhase
  ├── AfterTurnPhase
  └── ConversationLogger（自行取得同一单例）
```

---

## 二十二、实际事件类型来自哪里

当前主 Pipeline 实际使用的 Context 和 `TurnCommittedEvent` 主要定义在：

```text
agent/core/types.py
```

包括：

```text
BeforeTurnCtx
BeforeReasoningCtx
PromptRenderCtx
BeforeStepCtx
AfterStepCtx
BeforeToolCallCtx
AfterToolResultCtx
AfterReasoningCtx
AfterTurnCtx
TurnCommittedEvent
```

`agent/lifecycle/types.py` 只是从 `agent/core/types.py` 重新导出这些类型，方便插件系统引用。

项目中还存在：

```text
agent/core/events.py
```

其中定义了另一套 `BeforeTurnEvent`、`AfterTurnEvent` 等 dataclass。但按照当前代码搜索结果，主 Pipeline 和 ConversationLogger 使用的是 `agent/core/types.py` 中的 Context/Event；阅读主链路时应优先以 `core/types.py` 为准，`core/events.py` 更像早期或备用事件模型。

---

## 二十三、没有订阅者时会怎样

字符串事件：

```python
handlers = self._subscribers.get(event_type, [])
```

没有订阅者时得到空列表，循环不执行，正常返回 `None`。

输入：

```python
await bus.emit("nonexistent_event")
```

输出：

```python
None
```

Typed GATE 没有匹配 handler 时，会直接返回原 Context：

```python
ctx = GateCtx(value=1, trace=[])
result = await bus.emit(ctx)

assert result is ctx
```

Typed TAP 没有匹配 handler 时正常返回 `None`。

---

## 二十四、EventBus 是否操作数据库

不操作。

`EventBus` 内部数据全部保存在当前 Python 进程的内存中：

```text
_subscribers
_gate_handlers
_tap_handlers
```

它不创建数据库表，也不直接读写文件。

但是 EventBus 的 handler 可以自己操作数据库或文件。例如：

```text
EventBus 发布 turn_committed
  ↓
ConversationLogger handler
  ↓
后台写 JSONL 文件
```

文件写入是订阅者完成的，不是 EventBus 自己完成的。

---

## 二十五、需要注意的实现细节

### 1. 当前没有取消订阅接口

代码只有注册：

```python
subscribe()
on()
observe(Type, handler)
```

没有：

```text
unsubscribe
remove_handler
clear
```

全局单例上的处理器通常会一直存在到进程退出。重复启动某个组件并再次注册，可能产生重复 handler。

### 2. 单例只在当前进程内共享

如果部署多个 Python 进程，每个进程都有自己的 `EventBus._instance`，它们不会互相通信。

这不是 Redis、Kafka 或跨进程消息总线。

### 3. EventBus 不保存事件历史

事件发布后只通知当前已注册的 handler。之后才注册的 handler 看不到过去事件。

### 4. 字符串事件没有优先级

`subscribe()` 只追加列表，顺序由注册顺序决定。

### 5. Typed handler 支持父类匹配

注册父类可能一次匹配多个 Context，需要避免处理范围过大。

### 6. handler 应该避免慢速同步操作

同步文件读写、长时间计算等会阻塞 asyncio 事件循环。可以像 `ConversationLogger` 一样先放入 Queue，再由后台任务处理。

---

## 二十六、最小可运行理解示例

### 字符串广播

```python
bus = EventBus()
received = []

def handler(value: int):
    received.append(value)

bus.subscribe("number", handler)
await bus.emit("number", value=42)

assert received == [42]
```

图示：

```text
subscribe("number", handler)
               ↓
emit("number", value=42)
               ↓
handler(value=42)
               ↓
received = [42]
```

### GATE 修改数据

```python
@dataclass
class PriceCtx:
    price: int

def add_tax(ctx: PriceCtx):
    ctx.price += 10
    return ctx

bus = EventBus()
bus.on(PriceCtx, add_tax)

result = await bus.emit(PriceCtx(price=100))

assert result.price == 110
```

### TAP 只观察

```python
seen = []

def audit(ctx: PriceCtx):
    seen.append(ctx.price)

bus.observe(PriceCtx, audit)
await bus.observe(PriceCtx(price=100))

assert seen == [100]
```

---

## 二十七、阅读时需要记住的关键点

- EventBus 是程序内部的广播和生命周期扩展中心。
- `get_instance()` 让主要模块共享同一个 EventBus。
- `EventBus()` 会创建独立实例，常用于测试。
- 字符串事件使用 `subscribe()` 注册、`emit("名字")` 发布。
- GATE 使用 `on(Type, handler)` 注册、`emit(ctx)` 触发。
- GATE 可以修改、替换 Context，也可以返回 `None` 阻断流程。
- TAP 使用 `observe(Type, handler)` 注册、`await observe(ctx)` 触发。
- TAP 返回值被忽略，适合日志、审计和遥测。
- Typed handler 按 priority 从高到低执行。
- 类型匹配支持父类 handler 接收子类 Context。
- 同步和异步 handler 都支持，但全部按顺序执行。
- 单个 handler 抛异常时记录 Warning，其他 handler 继续执行。
- `turn_committed` 字符串事件连接 AfterTurnPhase 和 ConversationLogger。
- 插件生命周期装饰器最终由 PluginManager 注册到 EventBus。
- EventBus 不操作数据库，也不是跨进程消息系统。
- 当前没有取消订阅接口，单例上的 handler 会保留到进程结束。
