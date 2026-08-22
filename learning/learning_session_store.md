# telegram-bot 项目学习笔记

## `SessionStore`：原始会话的持久化存储

`main.py` 中的相关代码：

```python
session_store = get_session_store()
```

这行代码取得整个进程共享的 `SessionStore` 实例。`SessionStore` 负责把 Telegram 用户的原始聊天消息保存到 SQLite，也负责加载、搜索、回查和删除会话。

整体关系：

```text
Telegram 用户消息
  ↓
Pipeline 中的 Session 对象
  ↓
SessionStore.save()
  ↓
conversation_sessions 表
  ├── messages_json：原始 user/assistant 消息
  └── last_consolidated：长期记忆提炼进度
```

`SessionStore` 不负责生成 Embedding，也不直接操作长期记忆向量表。

---

## 一、`get_session_store()` 的作用

文件位置：`persistence/session_store.py`

原始代码：

```python
_session_store: SessionStore | None = None


def get_session_store() -> SessionStore:
    """获取 SessionStore 单例"""
    global _session_store
    if _session_store is None:
        _session_store = SessionStore()
    return _session_store
```

### 代码的作用

使用延迟初始化的方式，在当前 Python 进程中只创建一个 `SessionStore` 对象。

第一次调用：

```text
_session_store 是 None
  ↓
创建 SessionStore()
  ↓
保存到模块变量 _session_store
  ↓
返回该对象
```

后续调用：

```text
_session_store 已经存在
  ↓
直接返回同一个对象
```

因此，下面两个变量通常指向同一个实例：

```python
a = get_session_store()
b = get_session_store()

assert a is b
```

### 为什么使用单例

项目中有多个位置需要访问原始会话：

- `main.py` 把它注入 `MemoryEngine`。
- `BeforeTurnPhase` 用它加载会话。
- `PassiveTurnPipeline` 用它保存每轮对话。
- `MemoryEngine` 用它执行 `search_messages` 和 `fetch_messages`。

统一通过 `get_session_store()` 获取对象，可以避免不同模块随意创建不同的存储管理对象。

不过需要注意：真正的 SQLite 连接不是保存在 `SessionStore` 中，而是每个方法调用 `get_connection()` 获取。因此这里的单例主要统一访问入口，本身几乎没有可变状态。

### “单例”不等于“一个用户的 Session”

`get_session_store()` 返回的是会话存储管理器，不是某个用户的会话。

一个 `SessionStore` 可以管理许多会话：

```text
SessionStore
  ├── user_id=1, chat_id=100 的会话
  ├── user_id=1, chat_id=200 的会话
  └── user_id=2, chat_id=300 的会话
```

具体会话通过联合键确定：

```text
(user_id, chat_id)
```

---

## 二、项目中“Session”的三层含义

理解这个项目时，要区分以下三层。

### 1. `Session` 数据对象

定义在 `agent/core/types.py`：

```python
@dataclass
class Session:
    user_id: int
    chat_id: int
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    last_consolidated: int = 0
```

它表示程序运行时的一次会话状态，包含：

- 用户 ID。
- Telegram 聊天 ID。
- 当前会话的消息列表。
- 已完成长期记忆提炼的位置。

### 2. `_sessions` 内存缓存

定义在 `agent/pipeline/phases/before_turn.py`：

```python
_sessions: dict[tuple[int, int], Session] = {}
```

它使用：

```text
(user_id, chat_id) → Session 对象
```

缓存当前进程已经加载过的 Session，避免每一轮对话都从 SQLite 重新解析完整的 `messages_json`。

这个缓存只存在于当前进程内：

- Bot 重启后缓存消失。
- SQLite 中保存的会话数据不会因此消失。

### 3. `conversation_sessions` 数据库记录

这是 Session 的持久化形式。Bot 重启后，`BeforeTurnPhase` 可以通过 `SessionStore.load_state()` 从数据库恢复会话。

三者的关系：

```text
conversation_sessions 数据库记录
  ↓ load_state()
Session 数据对象
  ↓ 放入
_sessions 内存缓存
  ↓ 每轮追加消息
Session 数据对象
  ↓ save()
conversation_sessions 数据库记录
```

---

## 三、`SessionStore.save()`：保存会话

### 代码的作用

把一名用户在一个聊天中的完整消息列表和 `last_consolidated` 游标保存到 `conversation_sessions`。

方法参数：

```python
def save(
    self,
    user_id: int,
    chat_id: int,
    messages: list[dict[str, Any]],
    *,
    last_consolidated: int | None = None,
) -> None:
```

| 参数 | 作用 |
|---|---|
| `user_id` | Telegram 用户 ID。 |
| `chat_id` | Telegram 聊天 ID。 |
| `messages` | 完整消息列表，每项通常包含 `role` 和 `content`。 |
| `last_consolidated` | 已完成长期记忆提炼的消息位置；不传时保留数据库原值。 |

消息列表示例：

```python
[
    {"role": "user", "content": "我喜欢喝拿铁"},
    {"role": "assistant", "content": "好的，我知道了。"},
]
```

### 实现步骤

#### 1. 获取数据库连接

```python
conn = get_connection()
```

#### 2. 处理 consolidation 游标

```python
cursor = int(last_consolidated) if last_consolidated is not None else None
```

- 传入值时转换为整数。
- 没传时使用 `None`，后面的 SQL 会保留已有值。

这里的局部变量虽然名叫 `cursor`，但它是整数游标位置，不是 SQLite 的 Cursor 对象。

#### 3. 把消息转换为 JSON 字符串

```python
json.dumps(messages, ensure_ascii=False)
```

`ensure_ascii=False` 让中文直接以中文形式存入 JSON，而不是转换成 `\uXXXX`。

#### 4. 使用 UPSERT 写入数据库

核心 SQL：

```sql
INSERT INTO conversation_sessions
    (user_id, chat_id, messages_json, last_consolidated, updated_at)
VALUES (?, ?, ?, COALESCE(?, 0), CURRENT_TIMESTAMP)
ON CONFLICT(user_id, chat_id) DO UPDATE SET
    messages_json = excluded.messages_json,
    last_consolidated = COALESCE(?, conversation_sessions.last_consolidated),
    updated_at = CURRENT_TIMESTAMP
```

这是一条真正的 UPSERT：

- 如果 `(user_id, chat_id)` 不存在，就执行 `INSERT`。
- 如果联合主键已经存在，就执行 `UPDATE`。

新建记录时：

```sql
COALESCE(传入游标, 0)
```

如果没有传入游标，则从 `0` 开始。

更新记录时：

```sql
COALESCE(传入游标, conversation_sessions.last_consolidated)
```

如果没有传入游标，就保留数据库中已有的值。

`excluded.messages_json` 表示这次原本准备插入的新消息 JSON。

#### 5. 提交事务

```python
conn.commit()
```

保存完成后记录 Debug 日志，但日志不影响数据库内容。

### 数据库操作

| 表 | 操作 | 作用 |
|---|---|---|
| `conversation_sessions` | `INSERT` 或 `UPDATE` | 保存完整消息 JSON、consolidation 游标和更新时间。 |

---

## 四、`SessionStore.load_state()`：加载完整会话状态

### 代码的作用

根据 `(user_id, chat_id)` 读取：

```text
messages_json + last_consolidated
```

返回类型：

```python
tuple[list[dict[str, Any]], int] | None
```

可能得到：

```python
(
    [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好！"},
    ],
    0,
)
```

如果会话不存在，则返回 `None`。

### 实现步骤

执行查询：

```sql
SELECT messages_json, last_consolidated
FROM conversation_sessions
WHERE user_id = ? AND chat_id = ?
```

如果找到记录：

```python
messages = json.loads(row[0])
last_consolidated = int(row[1] or 0)
```

- 把 JSON 字符串还原成 Python 消息列表。
- 把游标转换为整数；空值按 `0` 处理。

如果 JSON 内容损坏，`json.loads()` 会产生 `JSONDecodeError`。当前代码会记录 Warning，并返回 `None`，让上层把它当成没有有效会话。

### 数据库操作

| 表 | 操作 | 作用 |
|---|---|---|
| `conversation_sessions` | `SELECT` | 按用户和聊天加载消息及 consolidation 游标。 |

---

## 五、`SessionStore.load()`：只加载消息

### 代码的作用

`load()` 是对 `load_state()` 的兼容封装，只返回消息列表，不返回 consolidation 游标。

```python
def load(self, user_id: int, chat_id: int) -> list[dict[str, Any]] | None:
    state = self.load_state(user_id, chat_id)
    if state is None:
        return None
    messages, _last_consolidated = state
    return messages
```

变量名前面的下划线：

```python
_last_consolidated
```

表示这个值被解包出来，但当前方法不使用它。

数据库操作最终仍由 `load_state()` 完成。

---

## 六、`SessionStore.fetch_messages()`：按位置回查原文

### 代码的作用

从一个指定 Session 中读取原始消息，并给每条消息生成可追溯的 `source_ref`。

它主要用于：

- 根据长期记忆的来源回查原始对话。
- 读取某条消息前后的上下文。
- 为 LLM 的事实回答提供原文证据。

### 两种读取方式

#### 1. 没有传 `seq`

```python
seq is None
```

返回会话最近的 `limit` 条消息：

```python
selected = messages[-max(1, int(limit)):]
```

例如共有 100 条消息，`limit=20`，就返回第 80～99 条。

#### 2. 传入 `seq`

读取指定序号或序号范围，还可以通过 `context` 扩展前后文。

例如：

```python
fetch_messages(
    user_id=123,
    chat_id=456,
    seq=8,
    seq_end=9,
    context=2,
)
```

目标是第 8～9 条消息，同时读取它们前后各 2 条上下文。

代码会保证范围不超出消息列表：

```python
start = max(0, ref_start - ctx)
end = min(len(messages), ref_end + ctx + 1)
```

### 返回结构

每条消息被整理为：

```python
{
    "role": "user",
    "content": "我喜欢喝拿铁",
    "seq": 8,
    "source_ref": "session:123:456#msg:8",
    "in_source_ref": True,
}
```

字段含义：

| 字段 | 作用 |
|---|---|
| `role` | 消息发送方，例如 `user` 或 `assistant`。 |
| `content` | 原始消息正文。 |
| `seq` | 消息在该 Session 消息列表中的下标，从 0 开始。 |
| `source_ref` | 可供系统再次回查这条消息的引用。 |
| `in_source_ref` | 表示该消息是目标消息，还是仅仅因为 `context` 被带出的上下文。 |

方法返回：

```python
(消息列表, 实际匹配的目标消息数量)
```

### `source_ref` 格式

单条消息：

```text
session:{user_id}:{chat_id}#msg:{seq}
```

例如：

```text
session:123:456#msg:8
```

一段消息窗口在项目其他位置会写成：

```text
session:123:456#msg:8-11
```

`source_ref` 是 Session 与长期 Memory 之间的重要桥梁。

### 数据库操作

`fetch_messages()` 自己先调用 `load()`，最终执行：

| 表 | 操作 | 作用 |
|---|---|---|
| `conversation_sessions` | `SELECT` | 加载指定用户和聊天的完整消息 JSON，再在 Python 中切片。 |

这里不是在 SQL 中按单条消息查询，因为所有消息都存放在同一个 `messages_json` 字段里。

---

## 七、`SessionStore.search_messages()`：搜索原始消息

### 代码的作用

在某个用户的所有聊天 Session 中，根据文本查询原始消息。

它和 `MemoryStore.keyword_search()` 的区别是：

- `SessionStore.search_messages()` 搜索原始聊天记录。
- `MemoryStore.keyword_search()` 搜索提炼后的长期记忆摘要。

### 实现步骤

#### 1. 清理和限制输入

```python
term = (query or "").strip()
limit = max(1, min(int(limit), 50))
offset = max(0, int(offset))
```

- 空查询直接返回 `([], 0)`。
- `limit` 被限制为 1～50。
- `offset` 最小为 0。

#### 2. 查询该用户的全部 Session

```sql
SELECT chat_id, messages_json
FROM conversation_sessions
WHERE user_id = ?
ORDER BY updated_at DESC
```

这里没有限定 `chat_id`，因此会搜索该用户在数据库中的所有聊天，并优先处理最近更新的聊天。

#### 3. 在 Python 中解析和匹配消息

每个 Session 的 `messages_json` 都会通过 `json.loads()` 转换成消息列表，然后逐条检查：

- `role` 是否符合过滤条件。
- 完整查询字符串是否包含在消息中。
- 或者按空格拆分后的任意查询词是否包含在消息中。

匹配使用 `lower()` 进行英文大小写归一化。

这不是数据库全文检索，也不是向量语义检索。SQL 只负责取出 Session，实际文字匹配发生在 Python 中。

#### 4. 生成来源引用

每条命中结果包含：

```text
session:{user_id}:{chat_id}#msg:{seq}
```

#### 5. 在结果列表上分页

代码先收集所有命中，再返回：

```python
matches[offset:offset + limit]
```

第二个返回值 `total` 是分页前的总命中数。

### 数据库操作

| 表 | 操作 | 作用 |
|---|---|---|
| `conversation_sessions` | `SELECT` | 读取指定用户的所有 Session；原文匹配在 Python 中完成。 |

---

## 八、`SessionStore.delete()`：删除一个会话

### 代码的作用

删除指定用户在指定聊天中的持久化 Session：

```sql
DELETE FROM conversation_sessions
WHERE user_id = ? AND chat_id = ?
```

随后执行 `commit()`。

### 删除范围

这个方法只删除：

```text
conversation_sessions 中的一条会话记录
```

它不会自动删除：

- `memory_items` 中已经提炼出的长期记忆。
- `vec_items` 中的长期记忆向量。
- `memory_replacements` 中的替换关系。
- `BeforeTurnPhase._sessions` 中已经缓存的 Session 对象。

因此，如果业务上需要“彻底清除某个用户的全部数据”，只调用这个方法是不够的。

### 数据库操作

| 表 | 操作 | 作用 |
|---|---|---|
| `conversation_sessions` | `DELETE` | 删除一个 `(user_id, chat_id)` 对应的会话。 |

---

## 九、`conversation_sessions` 表结构

建表代码：

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

| 字段 | 类型 | 约束/默认值 | 作用 |
|---|---|---|---|
| `user_id` | `INTEGER` | 非空、联合主键之一 | Telegram 用户 ID。 |
| `chat_id` | `INTEGER` | 非空、联合主键之一 | Telegram 聊天 ID。 |
| `messages_json` | `TEXT` | 非空，默认 `[]` | 当前 Session 的完整原始消息数组。 |
| `last_consolidated` | `INTEGER` | 非空，默认 `0` | 已经完成长期记忆提炼的消息位置。 |
| `created_at` | `TIMESTAMP` | 默认当前时间 | 会话记录首次创建时间。 |
| `updated_at` | `TIMESTAMP` | 默认当前时间 | 会话最近保存时间。 |

联合主键：

```sql
PRIMARY KEY (user_id, chat_id)
```

它表示同一用户在同一聊天中只有一条 Session 记录。

---

## 十、`last_consolidated` 的作用

`last_consolidated` 不是“最后合并时间”，而是消息列表中的位置游标。

假设一个 Session 有 20 条消息：

```text
messages[0] ... messages[19]
```

如果：

```text
last_consolidated = 6
```

表示前面一部分消息已经经过长期记忆提炼，下一次不应该从头重复处理。

`ConsolidationWorker` 会根据：

```python
session.messages[session.last_consolidated:consolidate_up_to]
```

选择尚未提炼且不属于近期保留窗口的消息。提炼完成后，再推进：

```python
session.last_consolidated = consolidate_up_to
```

然后由 `SessionStore.save()` 把新游标持久化。

这个游标解决的问题是：

- 避免每一轮都让 LLM 重复阅读全部历史。
- 避免从相同对话反复生成重复长期记忆。
- Bot 重启后仍然知道长期记忆整理到了哪里。

---

## 十一、Session 和 Memory 的区别

这是理解该项目最关键的区别之一。

| 对比项 | Session | Memory |
|---|---|---|
| 保存内容 | 用户和助手的原始消息 | 从对话中提炼出的长期事实、偏好、流程和事件 |
| 主要数据结构 | `Session` | `MemoryItem` |
| 主要管理类 | `SessionStore` | `MemoryStore` / `MemoryEngine` |
| 数据库表 | `conversation_sessions` | `memory_items`、`vec_items`、`memory_replacements` |
| 数据粒度 | 一个用户在一个聊天中的完整消息列表 | 一条可独立检索的长期信息 |
| 主键/标识 | `(user_id, chat_id)` | 每条记忆一个 UUID |
| 是否保留原话 | 是 | 通常不是原话，而是提炼后的摘要 |
| 是否保存助手回复 | 是 | 长期记忆提炼原则上主要从用户明确表达的信息中产生 |
| 搜索方式 | 原始文本匹配、按消息序号回查 | 向量检索、关键词检索、状态与类型过滤 |
| 数据范围 | 同时按 `user_id` 和 `chat_id` 隔离 | 表中直接按 `user_id` 隔离，聊天来源通过 `source_ref` 关联 |
| 状态更新 | 追加消息、推进 consolidation 游标 | `active`、`superseded` 及替换关系 |
| 主要用途 | 保留完整上下文和原始证据 | 跨较长时间快速召回用户的重要信息 |

### 示例

用户发送：

```text
我以前每天喝咖啡，不过最近戒咖啡了，现在主要喝绿茶。
```

Session 中保存的是原始消息：

```python
{
    "role": "user",
    "content": "我以前每天喝咖啡，不过最近戒咖啡了，现在主要喝绿茶。",
}
```

长期 Memory 中可能提炼为：

```text
preference：用户以前每天喝咖啡，现在已经戒咖啡并主要喝绿茶。
```

两者的用途不同：

- Memory 适合快速回答“我现在喜欢喝什么”。
- Session 适合确认用户当时的准确原话和上下文。

### 为什么不能只保存 Memory

长期 Memory 是摘要，可能会省略：

- 精确措辞。
- 时间和上下文。
- 谁说了什么。
- 前后多轮对话关系。

因此需要 Session 作为原始证据。

### 为什么不能只保存 Session

如果原始消息越来越多，每次提问都把全部历史发送给 LLM，会产生：

- 上下文过长。
- Token 成本增加。
- 检索速度下降。
- 重要信息容易被大量闲聊淹没。

因此需要 Memory 提炼出少量、可搜索的长期信息。

### 二者如何配合

```text
原始对话
  ↓ SessionStore.save()
conversation_sessions
  ↓ ConsolidationWorker
提炼长期信息
  ↓ MemoryStore.upsert_item()
memory_items + vec_items
  ↓ 保存 source_ref
session:用户ID:聊天ID#msg:开始-结束
  ↓ 需要事实证据时
SessionStore.fetch_messages()
  ↓
返回原始消息
```

Memory 负责“快速找到线索”，Session 负责“回到原始证据”。

---

## 十二、`source_ref`：连接 Session 和 Memory 的桥梁

长期记忆写入 `memory_items` 时可以携带：

```text
source_ref = session:123:456#msg:8-11
```

含义是：

```text
user_id = 123
chat_id = 456
来源消息范围 = 第 8～11 条
```

之后系统通过 `MemoryStore` 召回该长期记忆时，会同时得到 `source_ref`。如果回答需要精确事实，就把它交给 `MemoryEngine.fetch_messages()`，最终调用：

```python
SessionStore.fetch_messages(...)
```

取得原始消息。

因此完整证据链为：

```text
用户问题
  ↓
召回 Memory 摘要
  ↓ 得到 source_ref
回查 Session 原文
  ↓
基于原始证据回答
```

---

## 十三、它在 `main.py` 和 Pipeline 中的链路

### 1. `main.py` 取得单例

```python
session_store = get_session_store()
```

这里只创建或取得管理器，不会立刻连接数据库，也不会加载某个用户的消息。

### 2. 注入 `MemoryEngine`

```python
memory_runtime = build_memory_runtime(
    embedder=embedder,
    memory_store=memory_store,
    session_store=session_store,
)
```

这样 `MemoryEngine` 就能提供：

- `search_messages`：搜索原始消息。
- `fetch_messages`：根据 `source_ref` 回查原文。

### 3. 收到消息时加载 Session

`BeforeTurnPhase.acquire_session()`：

```text
先检查 _sessions 内存缓存
  ↓ 没有缓存
SessionStore.load_state(user_id, chat_id)
  ↓
创建 Session 对象并放入缓存
```

### 4. 一轮回复后保存 Session

`PassiveTurnPipeline.execute()` 会把本轮两条消息追加到 Session：

```python
session.messages.append({"role": "user", ...})
session.messages.append({"role": "assistant", ...})
```

随后调用：

```python
get_session_store().save(
    user_id,
    chat_id,
    session.messages,
    last_consolidated=session.last_consolidated,
)
```

### 5. 后台提炼长期记忆

消息数量满足条件时，`ConsolidationWorker`：

```text
读取 Session 中尚未整理的旧消息
  ↓
调用 LLM 提炼长期信息
  ↓
MemoryStore 写入长期 Memory
  ↓
推进 session.last_consolidated
  ↓
SessionStore.save() 保存新游标
```

---

## 十四、完整示例

第一轮收到用户消息：

```text
我喜欢喝拿铁。
```

Pipeline 回复后，Session 可能变成：

```python
[
    {"role": "user", "content": "我喜欢喝拿铁。"},
    {"role": "assistant", "content": "好的。"},
]
```

调用：

```python
session_store.save(
    user_id=123,
    chat_id=456,
    messages=messages,
    last_consolidated=0,
)
```

数据库中的 Session 记录类似：

```text
user_id          = 123
chat_id          = 456
messages_json    = [{"role":"user",...},{"role":"assistant",...}]
last_consolidated = 0
```

当消息积累到足够数量后，ConsolidationWorker 可能生成长期 Memory：

```text
memory_type = preference
summary     = 用户喜欢喝拿铁
source_ref  = session:123:456#msg:0-1
status      = active
```

以后用户问：

```text
你还记得我喜欢喝什么吗？
```

系统可以：

```text
MemoryStore 召回“用户喜欢喝拿铁”
  ↓
根据 source_ref 调用 SessionStore.fetch_messages()
  ↓
读取“我喜欢喝拿铁”的原始消息
  ↓
基于证据回答
```

---

## 十五、阅读时需要记住的关键点

- `get_session_store()` 返回进程级存储管理器，不是单个用户的 Session。
- `SessionStore` 自身不保存用户消息，消息实际存在 `conversation_sessions` 表中。
- `Session` 是运行时对象，`_sessions` 是内存缓存，`conversation_sessions` 是持久化数据。
- 会话由 `(user_id, chat_id)` 唯一确定。
- `save()` 是真正的 UPSERT：不存在则插入，存在则更新。
- `messages_json` 保存完整原始消息，不是长期记忆摘要。
- `last_consolidated` 是消息位置游标，不是时间。
- `search_messages()` 搜索原始对话，`MemoryStore.keyword_search()` 搜索长期记忆摘要。
- `fetch_messages()` 在 Python 中对完整消息数组切片，不是在 SQL 中按单条消息查询。
- Session 和 Memory 通过 `source_ref` 建立可追溯关系。
- Memory 用于快速找到重要线索，Session 用于提供原始事实证据。
- `delete()` 只删除数据库中的 Session，不会自动清理长期 Memory 或内存 Session 缓存。
