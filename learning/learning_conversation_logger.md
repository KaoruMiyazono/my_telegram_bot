# telegram-bot 项目学习笔记

## `ConversationLogger`：真实对话数据采集系统

`main.py` 中的代码：

```python
conversation_logger = ConversationLogger()
await conversation_logger.start()
```

对应实现文件：

```text
evaluation/conversation_logger.py
```

---

## 一、先说明：这套系统是干什么的

`ConversationLogger` 是一个“真实对话数据采集器”。

它监听每一轮已经生成完毕的对话，把下面这些信息保存到 JSONL 文件：

- 用户说了什么。
- Bot 回复了什么。
- 用户 ID 和 Telegram chat ID。
- 本轮对话的唯一 `turn_id`。
- 本轮产生了哪些新记忆。
- 记录时间。

保存后的原始数据可以用于：

- 检查 Bot 的真实回答效果。
- 人工分析错误回答。
- 构建评估数据集。
- 回放或导入测试数据。
- 观察记忆系统是否正确产生了记忆。

整体数据流：

```text
用户发送消息
    ↓
Pipeline 生成 Bot 回复
    ↓
AfterTurnPhase 创建 TurnCommittedEvent
    ↓
EventBus 发布 "turn_committed"
    ↓
ConversationLogger 收到事件
    ↓
先放入 asyncio.Queue
    ↓
后台任务写入 raw_conversations.jsonl
    ↓
后续评估、标注或数据分析
```

它记录的是“业务对话数据”，不是普通运行日志。

```text
普通 logging 日志
├── Bot started
├── 请求失败
├── 插件加载成功
└── 用于排查程序运行问题

ConversationLogger
├── 用户原始问题
├── Bot 最终回复
├── 用户和聊天标识
├── 新记忆 ID
└── 用于评估对话质量
```

---

## 二、它与 Session、Memory 有什么区别

| 系统 | 保存什么 | 主要目的 | 默认存储位置 |
|---|---|---|---|
| `ConversationLogger` | 每轮用户输入、Bot 输出、记忆 ID | 评估、分析、构建数据集 | `data/evaluation/raw_conversations.jsonl` |
| SessionStore | 当前用户与聊天的消息历史 | 为后续对话提供上下文 | SQLite `conversation_sessions` |
| MemoryStore | 提炼后的事实、偏好、流程、事件等 | 跨会话长期召回 | SQLite/向量索引 |
| Python `logging` | 启动、报错、调试等运行信息 | 运维与排错 | 控制台或日志配置目标 |

可以这样理解：

```text
Session
保存“对话上下文”，供 Bot 下一轮继续聊天

Memory
保存“从对话中提炼的长期信息”，供未来检索

ConversationLogger
保存“真实问答样本”，供开发者评估系统
```

`ConversationLogger` 不是 Bot 回答问题时必须读取的记忆系统。删除它的日志不会直接清空 Session 或长期记忆。

---

## 三、`main.py` 中的完整生命周期

启动时：

```python
conversation_logger = ConversationLogger()
await conversation_logger.start()
```

关闭时：

```python
finally:
    await plugin_manager.terminate_all()
    await conversation_logger.stop()
```

完整生命周期：

```text
main() 启动
   ↓
ConversationLogger()
   ├── 创建日志目录
   ├── 创建异步队列
   └── 取得共享 EventBus
   ↓
await start()
   ├── 订阅 turn_committed
   └── 启动后台写入任务
   ↓
Bot 持续处理多轮对话
   ↓
程序关闭进入 finally
   ↓
await stop()
   ├── 停止后台任务
   └── 把队列剩余内容全部写完
```

`ConversationLogger()` 只是初始化对象；真正开始接收事件的是 `await start()`。

---

## 四、`__init__()`：初始化记录器

函数签名：

```python
def __init__(self, log_dir: str = "./data/evaluation") -> None:
```

输入：日志目录字符串。

默认输入：

```python
ConversationLogger()
```

等价于：

```python
ConversationLogger(log_dir="./data/evaluation")
```

输出：一个 `ConversationLogger` 对象。

### 1. 创建日志目录

```python
self.log_dir = Path(log_dir)
self.log_dir.mkdir(parents=True, exist_ok=True)
```

`parents=True`：父目录不存在时一起创建。

`exist_ok=True`：目录已存在时不报错。

默认路径是相对路径，实际位置取决于程序启动时的当前工作目录。通常项目从仓库根目录启动，因此会得到：

```text
/Users/zhengzhiyong/Desktop/work/telegram-bot/data/evaluation
```

### 2. 确定 JSONL 文件路径

```python
self.raw_log_path = self.log_dir / "raw_conversations.jsonl"
```

得到：

```text
./data/evaluation/raw_conversations.jsonl
```

初始化时会创建目录，但不会立即创建 JSONL 文件。文件通常在第一条数据追加写入时产生。

### 3. 创建待写入队列

```python
self._pending_writes = asyncio.Queue()
```

这个队列存放“已经收到、尚未写入磁盘”的对话字典。

```text
_pending_writes
├── turn_data 1
├── turn_data 2
└── turn_data 3
```

当前 Queue 没有设置 `maxsize`，默认是无界队列。因此正常实现下 `QueueFull` 基本不会发生；代码仍然保留了防御性处理。

### 4. 初始化后台任务引用

```python
self._write_task = None
```

`start()` 后会变成：

```text
Task(name="conversation_logger_write")
```

### 5. 取得共享 EventBus

```python
self.event_bus = EventBus.get_instance()
```

这与 `main.py` 的：

```python
event_bus = EventBus.get_instance()
```

通常是同一个进程内对象，所以 `AfterTurnPhase` 发布的事件能被 Logger 收到。

### 6. 创建会话缓存

```python
self._conversation_cache = {}
```

注释说它用于内存中的会话缓存，但当前文件中没有其他代码读取或写入它。

所以当前真实状态是：

> `_conversation_cache` 已定义，但尚未参与日志处理流程，属于未使用字段。

---

## 五、什么是 JSONL

JSONL 是 JSON Lines：每一行都是一条独立 JSON 对象。

文件示例：

```jsonl
{"turn_id":"turn-1","user_id":1001,"inbound_content":"你好"}
{"turn_id":"turn-2","user_id":1001,"inbound_content":"我喜欢拿铁"}
{"turn_id":"turn-3","user_id":1002,"inbound_content":"帮我查记忆"}
```

它与普通 JSON 数组不同：

```json
[
  {"turn_id": "turn-1"},
  {"turn_id": "turn-2"}
]
```

JSONL 的优点：

- 每次只需要在文件末尾追加一行。
- 不需要加载或重写整个文件。
- 即使某一行损坏，其他行仍可能读取。
- 适合持续产生的日志和数据流。

这个项目是一行记录“一轮对话”，不是一行记录“一个完整 Session”。

---

## 六、每条日志的数据结构

`_handle_turn_committed()` 构建的数据格式：

```json
{
  "turn_id": "8b864a6e-...",
  "timestamp": "2026-08-20T03:20:00.123456+00:00",
  "user_id": 1001,
  "inbound_content": "我喜欢喝拿铁",
  "outbound_message": {
    "chat_id": 1001,
    "content": "好的，我记住了。",
    "format": "text"
  },
  "new_memory_ids": [
    "cf04531a-..."
  ]
}
```

字段说明：

| 字段 | 类型 | 作用 |
|---|---|---|
| `turn_id` | `str` | 本轮对话的唯一标识，由 `AfterTurnPhase` 生成 UUID |
| `timestamp` | `str` | Logger 收到事件时的 UTC ISO 时间 |
| `user_id` | `int` | Telegram 用户 ID |
| `inbound_content` | `str` | 用户本轮输入 |
| `outbound_message.chat_id` | `int` | 回复目标聊天 ID |
| `outbound_message.content` | `str` | Bot 本轮回复内容 |
| `outbound_message.format` | `str` | 消息格式，默认 `text` |
| `new_memory_ids` | `list[str]` | 本轮新增记忆的 UUID 字符串列表 |

注意：Logger 没有直接使用 `TurnCommittedEvent.timestamp`，而是重新执行：

```python
datetime.now(timezone.utc).isoformat()
```

因此日志中的 `timestamp` 是“Logger 处理事件的时间”，不是 Event 对象原有时间字段的直接序列化。

---

## 七、`start()`：订阅事件并启动后台任务

```python
async def start(self) -> None:
```

输入：无。

输出：`None`。

它做两件事。

### 1. 订阅字符串事件

```python
self.event_bus.subscribe(
    "turn_committed",
    self._handle_turn_committed,
)
```

注册关系：

```text
EventBus._subscribers
└── "turn_committed"
    └── conversation_logger._handle_turn_committed
```

### 2. 创建后台写入任务

```python
self._write_task = asyncio.create_task(
    self._write_loop(),
    name="conversation_logger_write",
)
```

`asyncio.create_task()` 不会在这里一直等待 `_write_loop()` 结束，而是让它在事件循环中后台运行。

```text
Bot 主任务
├── 接收 Telegram 消息
├── 调用 LLM
└── 继续处理新请求

后台 write task
└── 等待 Queue，有数据就写文件
```

### 为什么不能直接 `await self._write_loop()`

因为 `_write_loop()` 是无限循环：

```python
while True:
    ...
```

如果 `start()` 直接等待它，程序会一直停在 Logger，后面的 Telegram Bot 无法启动。

### 重复调用风险

当前 `start()` 没有“已经启动”的保护。如果对同一个对象调用两次：

- handler 可能被重复订阅。
- 会创建新的后台任务并覆盖 `_write_task` 引用。
- 一次事件可能被放入队列多次。

正常 `main.py` 只调用一次，因此标准启动链没有这个问题。

---

## 八、`TurnCommittedEvent` 是从哪里来的

事件结构定义：

```python
@dataclass
class TurnCommittedEvent:
    turn_id: str
    user_id: int
    inbound_content: str
    outbound_message: OutboundMessage
    new_memory_ids: list[UUID]
    timestamp: datetime = ...
```

`AfterTurnPhase.execute()` 创建它：

```python
event = TurnCommittedEvent(
    turn_id=str(uuid4()),
    user_id=user_id,
    inbound_content=inbound_content,
    outbound_message=ctx.outbound_message,
    new_memory_ids=new_memory_ids,
)
```

然后发布：

```python
await self.event_bus.emit(
    "turn_committed",
    event=event,
)
```

`event=event` 非常重要，因为 EventBus 最终会这样调用订阅者：

```python
handler(**data)
```

等价于：

```python
conversation_logger._handle_turn_committed(event=event)
```

参数名必须对得上。

---

## 九、`_handle_turn_committed()`：把事件转换为日志数据

```python
def _handle_turn_committed(
    self,
    event: TurnCommittedEvent,
) -> None:
```

输入：一个 `TurnCommittedEvent`。

输出：`None`。

它首先把 dataclass 事件转换成适合 JSON 序列化的普通字典。

输入示例：

```python
TurnCommittedEvent(
    turn_id="turn-001",
    user_id=1001,
    inbound_content="我喜欢拿铁",
    outbound_message=OutboundMessage(
        chat_id=2001,
        content="好的，我记住了。",
        format="text",
    ),
    new_memory_ids=[memory_uuid],
)
```

转换后：

```python
{
    "turn_id": "turn-001",
    "timestamp": "...+00:00",
    "user_id": 1001,
    "inbound_content": "我喜欢拿铁",
    "outbound_message": {
        "chat_id": 2001,
        "content": "好的，我记住了。",
        "format": "text",
    },
    "new_memory_ids": [str(memory_uuid)],
}
```

### 为什么 UUID 要转换成字符串

Python `UUID` 对象不能直接被标准 `json.dumps()` 序列化：

```python
[str(mid) for mid in event.new_memory_ids]
```

将其转换成：

```text
"cf04531a-e91f-..."
```

### 为什么这个函数是同步函数

它处在 EventBus 的回调链上，为了尽快返回，不直接执行磁盘 I/O，只调用：

```python
self._pending_writes.put_nowait(turn_data)
```

`put_nowait()` 把数据立即放入队列，不等待写盘完成。

```text
事件回调
   ↓
只做轻量数据转换
   ↓
put_nowait
   ↓
立即返回

耗时写盘
   ↓
交给后台任务
```

### QueueFull 分支

```python
except asyncio.QueueFull:
    logger.warning(...)
```

如果队列满，会丢弃这一轮并记录 Warning，避免日志系统阻塞对话主流程。

不过当前 Queue 没有限制容量，所以此分支正常情况下不会发生。

---

## 十、`_write_loop()`：后台消费队列

```python
async def _write_loop(self) -> None:
```

输入：无。

输出：通常不主动返回；任务被取消时结束。

核心代码：

```python
while True:
    turn_data = await self._pending_writes.get()
    await self._append_to_file(turn_data)
```

没有数据时：

```python
await queue.get()
```

会挂起当前后台任务，但不会占用 CPU 忙循环，也不会阻塞其他 asyncio 任务。

有数据时：

```text
Queue
├── turn A  ← get()
├── turn B
└── turn C
       ↓
append turn A
       ↓
再 get turn B
```

因此当前写入是顺序进行的，基本保持 Queue 入队顺序。

异常处理：

- 收到 `CancelledError`：跳出循环，准备关闭。
- 其他异常：记录 Error，然后继续下一轮循环。

注意：这里没有调用 `queue.task_done()`，项目也没有使用 `queue.join()`；关闭时采用 `_flush_all()` 手动清空队列。

---

## 十一、`_append_to_file()`：追加一行 JSONL

```python
async def _append_to_file(
    self,
    turn_data: dict[str, Any],
) -> None:
```

输入：一条对话字典。

输出：`None`。

内部写入：

```python
with open(self.raw_log_path, "a", encoding="utf-8") as f:
    f.write(json.dumps(turn_data, ensure_ascii=False) + "\n")
```

### 为什么使用 `"a"`

`a` 表示 append：追加到文件末尾。

```text
原文件
├── turn 1
└── turn 2

追加 turn 3 后
├── turn 1
├── turn 2
└── turn 3
```

不会像 `"w"` 那样覆盖原文件。

### 为什么 `ensure_ascii=False`

它让中文直接写成中文：

```json
{"inbound_content":"我喜欢拿铁"}
```

而不是 Unicode 转义：

```json
{"inbound_content":"\u6211\u559c\u6b22\u62ff\u94c1"}
```

### 为什么使用线程池

普通文件写入是同步阻塞操作。代码使用：

```python
loop = asyncio.get_event_loop()
await loop.run_in_executor(None, _write)
```

把 `_write()` 交给默认线程池：

```text
asyncio 事件循环
   │
   ├── 继续处理网络、Bot、其他协程
   │
   └── 线程池执行文件 open/write/close
```

这样磁盘写入不会直接卡住 asyncio 事件循环。

### 输入输出示例

输入：

```python
{"turn_id": "turn-1", "user_id": 1001, ...}
```

文件增加一行：

```jsonl
{"turn_id": "turn-1", "user_id": 1001, ...}
```

函数返回：`None`。

---

## 十二、为什么需要“队列 + 后台任务 + 线程池”三层

三层分别解决不同问题：

```text
第一层：Queue
让事件回调不用等待文件写入

第二层：后台 asyncio Task
持续消费 Queue，不阻塞对话处理链

第三层：run_in_executor
避免同步磁盘 I/O 阻塞 asyncio 事件循环
```

完整时序：

```text
AfterTurnPhase       EventBus       Logger callback       Queue       Write Task       Thread
     │                  │                 │                 │              │              │
     │ emit(event)      │                 │                 │              │              │
     ├─────────────────→│                 │                 │              │              │
     │                  │ handler(event)  │                 │              │              │
     │                  ├────────────────→│                 │              │              │
     │                  │                 │ put_nowait      │              │              │
     │                  │                 ├────────────────→│              │              │
     │                  │                 │ return          │              │              │
     │                  │←────────────────┤                 │              │              │
     │←─────────────────┤                                   │              │              │
     │                                                      │ get          │              │
     │                                                      ├─────────────→│              │
     │                                                      │              │ run executor │
     │                                                      │              ├─────────────→│
     │                                                      │              │              │ append JSONL
```

重点是：EventBus 等待的是同步 callback 返回，而不是等待对话数据真正落盘。

---

## 十三、`stop()`：安全关闭并刷新剩余数据

```python
async def stop(self) -> None:
```

输入：无。

输出：`None`。

执行顺序：

```text
如果后台任务存在
   ↓
cancel()
   ↓
await 后台任务结束
   ↓
忽略正常的 CancelledError
   ↓
_flush_all()
   ↓
把 Queue 中剩余数据全部写入文件
```

为什么必须调用 `stop()`：

```text
程序直接退出
   ↓
Queue 里可能仍有 turn_data
   ↓
这些数据还没写到磁盘
   ↓
日志丢失
```

`main.py` 把它放在 `finally`，就是为了正常取消时尽量完成收尾。

### `stop()` 不会取消订阅

当前 EventBus 没有 `unsubscribe()`，所以 `stop()` 只停止写入任务并清空队列，不会从 EventBus 删除 handler。

如果同一进程中停止后又重新启动 Logger，旧订阅仍可能存在，从而发生重复入队。正常 Bot 生命周期是一次启动、一次关闭，因此通常不会触发。

---

## 十四、`_flush_all()`：同步清空剩余队列

```python
async def _flush_all(self) -> None:
```

输入：无。

输出：`None`。

实现：

```python
while not self._pending_writes.empty():
    turn_data = self._pending_writes.get_nowait()
    await self._append_to_file(turn_data)
```

示例：关闭时 Queue 中还有三条：

```text
Queue
├── A
├── B
└── C
```

执行结果：

```text
写 A
写 B
写 C
Queue 为空
stop() 返回
```

它不再依赖已经被取消的 `_write_loop()`，而是在关闭流程里主动逐条写入。

---

## 十五、`load_raw_conversations()`：读取原始日志

```python
def load_raw_conversations(
    self,
    limit: int = 0,
) -> list[dict[str, Any]]:
```

输入：

- `limit=0`：读取全部。
- `limit>0`：从文件开头最多读取 N 条。

输出：对话字典列表。

### 文件不存在

```python
logger.load_raw_conversations()
```

输出：

```python
[]
```

同时记录 Warning，不抛异常。

### 全部读取

```python
conversations = logger.load_raw_conversations()
```

输出示例：

```python
[
    {"turn_id": "turn-1", ...},
    {"turn_id": "turn-2", ...},
]
```

### 限制数量

```python
logger.load_raw_conversations(limit=2)
```

返回文件开头前两条，也就是较早写入的两条，不是最后两条。

### 单行 JSON 损坏

代码逐行执行：

```python
json.loads(line)
```

某一行无法解析时：

- 记录 Warning。
- 跳过坏行。
- 继续读取下一行。

这也是 JSONL 相比单个巨大 JSON 数组更适合日志的原因之一。

### 同步读取的特点

这个方法使用普通 `open()`，是同步函数。日志很大时会阻塞调用它的线程，所以它更适合离线评估脚本，而不是放在 Bot 的高频对话主链中。

---

## 十六、`clear_raw_conversations()`：删除原始日志

```python
def clear_raw_conversations(self) -> None:
```

输入：无。

输出：`None`。

如果文件存在：

```python
self.raw_log_path.unlink()
```

会直接删除：

```text
raw_conversations.jsonl
```

文件不存在时什么也不做。

测试代码在开始前调用它，以免旧数据影响本次断言。

注意：这是破坏性操作，不是把内容移动到回收站，也没有备份。正式数据上调用前应确认文件是否还需要。

如果 Logger 正在运行时清除文件，后台任务之后收到新数据时，使用 append 模式会重新创建文件。

---

## 十七、与 `AfterTurnPhase` 的完整调用链

```text
PassiveTurnPipeline
   ↓
Reasoner 得到模型答案
   ↓
AfterReasoningPhase 创建 OutboundMessage
   ↓
AfterTurnPhase.execute()
   ├── 创建 turn_id
   ├── 创建 TurnCommittedEvent
   └── emit("turn_committed", event=event)
          ↓
       EventBus._emit_string()
          ↓
       ConversationLogger._handle_turn_committed(event)
          ↓
       Queue.put_nowait(turn_data)
          ↓
       ConversationLogger._write_loop()
          ↓
       _append_to_file()
          ↓
       raw_conversations.jsonl
```

### “committed” 是否表示 Telegram 已发送成功

当前 `AfterTurnPhase` 顺序是：

```text
1. emit("turn_committed")
2. observe(AfterTurnCtx)
3. telegram_adapter.send(...)
```

因此日志记录发生在 Telegram 发送之前。

这里的 `turn_committed` 更准确地表示：

> 本轮用户输入和 Bot 回复内容已经确定。

它不能严格证明 Telegram 消息已经成功送达用户。

---

## 十八、它为什么使用共享 EventBus

Logger 初始化时：

```python
self.event_bus = EventBus.get_instance()
```

AfterTurnPhase 使用 `main.py` 创建的：

```python
event_bus = EventBus.get_instance()
```

只要都使用 `get_instance()`，当前 Python 进程里通常是同一个对象：

```text
EventBus 对象 A
├── AfterTurnPhase 在这里 emit
└── ConversationLogger 在这里 subscribe
```

如果 Logger 和 AfterTurnPhase 使用两个独立的 `EventBus()`：

```text
EventBus A：Logger subscribe
EventBus B：AfterTurn emit
```

Logger 就不会收到事件。

---

## 十九、EventBus 如何执行 Logger 回调

字符串事件执行器会遍历所有订阅者：

```python
for handler in handlers:
    result = handler(**data)
    if asyncio.iscoroutine(result):
        await result
```

Logger 的 handler 是同步函数：

```python
def _handle_turn_committed(...):
    ...
```

所以 EventBus 调用后直接得到 `None`，不会 `await`。

如果 Logger handler 抛异常，EventBus 会记录 Warning，然后继续通知其他订阅者，不让日志故障直接打断主对话链。

---

## 二十、文件操作与数据库操作

### 文件操作

`ConversationLogger` 会进行以下文件系统操作：

| 操作 | 位置 | 作用 |
|---|---|---|
| 创建目录 | `__init__()` | 创建 `data/evaluation` |
| 追加文件 | `_append_to_file()` | 追加 JSONL 对话记录 |
| 读取文件 | `load_raw_conversations()` | 加载已记录对话 |
| 删除文件 | `clear_raw_conversations()` | 清空原始对话数据 |

### 数据库操作

`ConversationLogger` 本身：

- 不连接 SQLite。
- 不执行 SQL。
- 不创建数据库表。
- 不写入 `conversation_sessions`。
- 不直接写入长期记忆表。

它只写 JSONL 文件。

虽然日志中包含 `new_memory_ids`，但这只是记录 ID：

```text
Memory 系统负责真正创建记忆
        ↓
得到 memory UUID
        ↓
ConversationLogger 只把 UUID 写进日志
```

`evaluation/seed_memory.py` 可以在之后读取该 JSONL，再通过 MemoryStore 导入数据；那是另一个离线流程，不是 Logger 自己执行的数据库操作。

---

## 二十一、它与评估系统的关系

文件顶部说明：

```text
记录真实对话为原始数据，用于后续评估
```

可以理解为：

```text
真实 Bot 对话
   ↓ ConversationLogger
raw_conversations.jsonl
   ↓ 人工检查/标注/转换
评估用例
   ↓
green_set / red_set
   ↓
评估 Runner
   ↓
判断记忆检索和回答能力是否退化
```

`evaluation/dataset_builder.py` 把 `raw_conversations.jsonl` 描述为评估原料。

不过需要注意：Logger 当前输出是“每行一个 turn”；模拟数据生成器输出的是“每行一个 session，内部包含 messages”。两者结构不同，消费端必须知道自己读取的是哪一种格式。

---

## 二十二、一次真实记录示例

假设用户发送：

```text
我以后只喝茶，不喝咖啡了。
```

Bot 回复：

```text
好的，我会记住你现在更喜欢喝茶。
```

AfterTurnPhase 创建：

```python
TurnCommittedEvent(
    turn_id="turn-abc",
    user_id=1001,
    inbound_content="我以后只喝茶，不喝咖啡了。",
    outbound_message=OutboundMessage(
        chat_id=2001,
        content="好的，我会记住你现在更喜欢喝茶。",
    ),
    new_memory_ids=[UUID("...")],
)
```

Logger 的处理过程：

```text
TurnCommittedEvent
   ↓ 转普通 dict
UUID → str
datetime → ISO UTC 字符串
   ↓ put_nowait
asyncio.Queue
   ↓ 后台消费
run_in_executor
   ↓ append
raw_conversations.jsonl
```

最终文件增加：

```json
{"turn_id":"turn-abc","timestamp":"2026-08-20T03:20:00+00:00","user_id":1001,"inbound_content":"我以后只喝茶，不喝咖啡了。","outbound_message":{"chat_id":2001,"content":"好的，我会记住你现在更喜欢喝茶。","format":"text"},"new_memory_ids":["..."]}
```

---

## 二十三、当前实现需要注意的细节

### 1. 默认路径依赖当前工作目录

`./data/evaluation` 不是固定绝对路径。换一个目录启动程序，文件位置也会变化。

### 2. Queue 当前没有容量上限

如果磁盘长期写不动，而事件持续产生，Queue 可能不断占用内存。`QueueFull` 处理只有在未来设置 `maxsize` 后才会真正发挥作用。

### 3. `start()` 不是幂等的

重复调用会重复订阅并创建多个后台任务。

### 4. `stop()` 不会取消 EventBus 订阅

因为 EventBus 当前没有 unsubscribe 接口。

### 5. `_conversation_cache` 当前未使用

它不会把多个 turn 自动合并成一个 Session。

### 6. 日志时间不是 Event 自带时间

Logger 在 handler 中重新获取 UTC 时间。

### 7. 日志不等于消息送达证明

事件在 Telegram `send()` 之前发布。

### 8. `limit` 返回最早 N 条

读取从文件头开始，不是从文件尾取最近 N 条。

### 9. 没有文件轮转

所有数据持续追加到同一个 `raw_conversations.jsonl`，文件可能长期增长。

### 10. 没有并发文件锁

正常运行只有一个 Logger；如果多个进程或多个实例同时写同一文件，当前代码没有跨进程锁来协调。

### 11. 日志包含真实用户内容

文件可能包含用户输入、Bot 回复和用户 ID，应按真实数据处理，注意访问权限、备份、保留周期和隐私。

---

## 二十四、最小使用示例

```python
from evaluation.conversation_logger import ConversationLogger


conversation_logger = ConversationLogger(
    log_dir="./data/evaluation"
)

await conversation_logger.start()

# Bot 运行期间，AfterTurnPhase 会自动发布 turn_committed。
# Logger 不需要被手动逐轮调用。

try:
    await run_bot()
finally:
    await conversation_logger.stop()
```

如果手动模拟事件：

```python
event_bus = EventBus.get_instance()

await event_bus.emit(
    "turn_committed",
    event=TurnCommittedEvent(...),
)
```

之后可以读取：

```python
items = conversation_logger.load_raw_conversations()
```

---

## 二十五、每个函数的输入输出速查表

| 函数 | 输入 | 输出 | 主要副作用 |
|---|---|---|---|
| `__init__(log_dir)` | 日志目录 | Logger 对象 | 创建目录和内存队列 |
| `start()` | 无 | `None` | 订阅事件，创建后台任务 |
| `stop()` | 无 | `None` | 取消后台任务，刷新剩余数据 |
| `_handle_turn_committed(event)` | `TurnCommittedEvent` | `None` | 转换数据并放入 Queue |
| `_write_loop()` | 无 | 被取消时结束 | 持续消费 Queue |
| `_append_to_file(turn_data)` | 对话字典 | `None` | 向 JSONL 追加一行 |
| `_flush_all()` | 无 | `None` | 写完 Queue 中所有剩余数据 |
| `load_raw_conversations(limit)` | 最大读取数量 | `list[dict]` | 同步读取 JSONL |
| `clear_raw_conversations()` | 无 | `None` | 删除 JSONL 文件 |

---

## 二十六、一句话理解每个核心对象

```text
TurnCommittedEvent
一轮对话结果的数据包

EventBus
把数据包从 AfterTurnPhase 广播给订阅者

ConversationLogger
把事件转换成适合保存的字典

asyncio.Queue
隔离事件处理和磁盘写入速度

_write_loop Task
持续从 Queue 取数据

run_in_executor
把阻塞文件操作移出 asyncio 事件循环

raw_conversations.jsonl
最终保存真实对话样本的文件
```

---

## 二十七、阅读时需要记住的关键点

- `ConversationLogger` 是评估数据采集器，不是普通运行日志器。
- 它记录每一轮用户输入和 Bot 输出，一行 JSON 对应一个 turn。
- `ConversationLogger()` 会创建目录和队列，但不会开始监听事件。
- `start()` 才会订阅 `turn_committed` 并启动后台写任务。
- `AfterTurnPhase` 是事件发布方，ConversationLogger 是订阅方。
- 回调只负责 `put_nowait()`，不会在 EventBus 中直接写磁盘。
- 后台 `_write_loop()` 顺序消费 Queue。
- `_append_to_file()` 使用线程池，避免同步文件 I/O 阻塞 asyncio。
- `stop()` 很重要，它负责把 Queue 中剩余数据写完。
- 它写的是 JSONL 文件，不操作 SQLite，也不创建数据库表。
- 它和 SessionStore、MemoryStore 的用途不同，不能互相替代。
- 当前日志产生在 Telegram 发送之前，不能证明消息已经送达。
- 当前没有重复启动保护、取消订阅、文件轮转和跨进程文件锁。
- 原始日志含真实用户内容，应按敏感数据谨慎管理。
