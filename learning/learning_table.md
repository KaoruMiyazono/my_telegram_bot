---
title: telegram-bot SQLite数据库四张核心表
created: 2026-08-21
updated: 2026-08-21
tags: [telegram-bot, sqlite, database, memory, session, embedding]
description: 讲解 memory_items、vec_items、memory_replacements、conversation_sessions 四张表的结构、字段、样例数据、关联关系、读写函数，以及长期记忆、向量检索、记忆替换和原始 Session 的完整数据流。
source:
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/persistence/database.py # 四张表的建表 SQL、迁移和数据库连接
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/memory/store.py # 长期记忆、向量和替换关系的写入与查询
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/persistence/session_store.py # Session 原文的保存、读取、搜索和删除
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/memory/engine.py # 记忆引擎如何组合两路查询结果
  - Cursor AI 对话，2026-08-21
---

# telegram-bot SQLite数据库四张核心表

> 这个项目把原始对话、长期记忆、向量索引和记忆替换关系都保存在同一个 SQLite 数据库中，但四张表承担完全不同的职责。

## 一、先看四张表的全貌

数据库由 <code>init_db()</code> 创建，路径来自：

~~~python
settings.DATABASE_PATH
~~~

核心表共有四张：

~~~text
SQLite memory.db
│
├── conversation_sessions
│     保存 Session 对话原文
│
├── memory_items
│     保存提炼后的长期记忆正文和元数据
│
├── vec_items
│     保存长期记忆的 1024 维向量索引
│
└── memory_replacements
      保存旧记忆被哪条新记忆替代
~~~

最简单的区分：

| 表 | 保存什么 | 一条记录代表什么 |
|---|---|---|
| <code>conversation_sessions</code> | 对话原文 | 一个用户在一个 Chat 中的完整 Session |
| <code>memory_items</code> | 长期记忆摘要 | 一条被提炼出来的长期记忆 |
| <code>vec_items</code> | embedding 向量 | 一条长期记忆对应的向量索引 |
| <code>memory_replacements</code> | 替代关系 | 一条旧记忆被一条新记忆替代 |

完整数据流：

~~~text
Telegram 原始对话
       │
       ▼
conversation_sessions
       │
       │ Consolidation 提炼
       ▼
memory_items ───────────────┐
       │                    │
       │ 生成 embedding      │ 记忆发生更新
       ▼                    ▼
vec_items          memory_replacements
       │
       │ 向量检索
       ▼
MemoryEngine
       │
       ▼
注入 LLM Prompt
~~~

---

## 二、这些表由谁创建

<code>persistence/database.py</code> 中定义：

~~~python
TABLE_SCHEMA = """
...
"""
~~~

启动时执行：

~~~python
def init_db() -> None:
    db_path = Path(settings.DATABASE_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    conn.executescript(TABLE_SCHEMA)
    _ensure_conversation_session_columns(conn)

    conn.commit()
    conn.close()
~~~

输入：

~~~python
settings.DATABASE_PATH = "data/memory.db"
~~~

输出不是 Python 返回值，而是数据库副作用：

~~~text
data/
└── memory.db
    ├── memory_items
    ├── vec_items
    ├── memory_replacements
    └── conversation_sessions
~~~

为什么必须加载 <code>sqlite_vec</code>？

普通 SQLite 不认识：

~~~sql
FLOAT[1024]
vec_distance_l2(...)
~~~

加载 <code>sqlite_vec</code> 后，SQLite 才具备向量表和向量距离计算能力。

---

## 三、四张表之间的关系

### 关系图

~~~text
conversation_sessions
┌───────────────────────────────┐
│ user_id + chat_id             │
│ messages_json                 │
│ last_consolidated             │
└───────────────────────────────┘
           │
           │ Session 原文被提炼
           ▼
memory_items
┌───────────────────────────────┐
│ id                            │◄──────────────┐
│ user_id                       │               │
│ memory_type                   │               │
│ summary                       │               │
│ embedding                     │               │
│ status                        │               │
│ source_ref ───────────────────┼── 指回 Session 原文
└───────────────────────────────┘               │
           │                                    │
           │ id = embedding_id                  │
           ▼                                    │
vec_items                                        │
┌───────────────────────────────┐               │
│ embedding_id                  │               │
│ embedding FLOAT[1024]         │               │
└───────────────────────────────┘               │
                                                │
memory_replacements                             │
┌───────────────────────────────┐               │
│ old_id ───────────────────────┼───────────────┤
│ new_id ───────────────────────┼───────────────┘
│ replaced_at                   │
└───────────────────────────────┘
~~~

### 关系是不是数据库外键

不是。

建表 SQL 中没有：

~~~sql
FOREIGN KEY
~~~

这些关系由应用代码维护：

~~~text
vec_items.embedding_id 逻辑上对应 memory_items.id
memory_replacements.old_id 逻辑上对应 memory_items.id
memory_replacements.new_id 逻辑上对应 memory_items.id
memory_items.source_ref 逻辑上指向 conversation_sessions 中的消息位置
~~~

因此 SQLite 不会自动保证：

- 删除 <code>memory_items</code> 时自动删除 <code>vec_items</code>。
- 删除一条记忆时自动删除替代关系。
- <code>old_id</code> 和 <code>new_id</code> 一定真实存在。

一致性主要依赖应用层代码。

---

## 四、memory_items：长期记忆正文表

### 这张表干什么

<code>memory_items</code> 保存经过 Consolidation 或记忆工具提炼后的长期记忆。

它保存的是：

~~~text
用户喜欢喝绿茶
~~~

而不是完整原始对话：

~~~text
用户：我平时下午喜欢喝绿茶，咖啡喝多了睡不着。
助手：明白了，以后可以优先推荐茶。
~~~

完整原文在 <code>conversation_sessions</code>。

### 表结构

~~~sql
CREATE TABLE IF NOT EXISTS memory_items (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    memory_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    embedding BLOB,
    status TEXT NOT NULL DEFAULT 'active',
    source_ref TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
~~~

### 每个字段的作用

| 字段 | 类型 | 是否必填 | 作用 |
|---|---|---:|---|
| <code>id</code> | TEXT | 是 | 长期记忆的 UUID，也是和 vec_items 关联的 ID |
| <code>user_id</code> | INTEGER | 是 | 这条长期记忆属于哪个 Telegram 用户 |
| <code>memory_type</code> | TEXT | 是 | 长期记忆类型 |
| <code>summary</code> | TEXT | 是 | 提炼后的长期记忆正文 |
| <code>embedding</code> | BLOB | 否 | summary 对应的向量二进制 |
| <code>status</code> | TEXT | 是 | 记忆是否仍有效，默认 active |
| <code>source_ref</code> | TEXT | 否 | 这条记忆来自哪段 Session 原文 |
| <code>created_at</code> | TIMESTAMP | 自动 | 首次创建时间 |
| <code>updated_at</code> | TIMESTAMP | 自动或代码更新 | 最近修改时间 |

### memory_type 有哪些值

项目定义：

~~~python
LONG_TERM_MEMORY_TYPES = [
    "profile",
    "preference",
    "procedure",
    "event",
    "fact",
]
~~~

| 类型 | 含义 | 示例 |
|---|---|---|
| <code>profile</code> | 用户画像 | 用户是 Python 开发者 |
| <code>preference</code> | 用户偏好 | 用户喜欢喝绿茶 |
| <code>procedure</code> | 操作流程或规则 | 用户要求修改前先备份 |
| <code>event</code> | 发生过的事件 | 用户今天部署了 Telegram Bot |
| <code>fact</code> | 一般长期事实 | 项目部署在 Ubuntu 服务器 |

### status 有什么意义

当前主要使用：

~~~text
active
superseded
~~~

含义：

| 状态 | 含义 |
|---|---|
| <code>active</code> | 当前有效，默认参与检索 |
| <code>superseded</code> | 已经被新记忆替代，默认不参与检索 |

例如：

~~~text
旧记忆：用户住在北京
status = superseded

新记忆：用户已经搬到上海
status = active
~~~

### source_ref 是什么

示例：

~~~text
session:10001:20001#msg:4
~~~

拆解：

~~~text
session
  表示来源是原始会话

10001
  user_id

20001
  chat_id

msg:4
  Session 中第 4 条消息
~~~

它让长期记忆可以追溯到原始证据。

### 一条具体数据

| 字段 | 示例值 |
|---|---|
| id | 11111111-1111-1111-1111-111111111111 |
| user_id | 10001 |
| memory_type | preference |
| summary | 用户喜欢喝绿茶 |
| embedding | 二进制向量 |
| status | active |
| source_ref | session:10001:20001#msg:4 |
| created_at | 2026-08-21 10:00:00 |
| updated_at | 2026-08-21 10:00:00 |

### 谁写入 memory_items

主要是：

~~~python
MemoryStore.upsert_item()
~~~

输入示例：

~~~python
await memory_store.upsert_item(
    memory_type="preference",
    summary="用户喜欢喝绿茶",
    user_id=10001,
    source_ref="session:10001:20001#msg:4",
)
~~~

执行 SQL：

~~~sql
INSERT INTO memory_items (
    id,
    user_id,
    memory_type,
    summary,
    embedding,
    status,
    source_ref
)
VALUES (?, ?, ?, ?, ?, 'active', ?)
~~~

返回：

~~~python
MemoryItem(
    id=generated_uuid,
    user_id=10001,
    memory_type="preference",
    summary="用户喜欢喝绿茶",
    embedding=[...],
    status="active",
    source_ref="session:10001:20001#msg:4",
)
~~~

注意：

> 函数名叫 <code>upsert_item()</code>，但当前 SQL 实际是普通 INSERT，并没有 ON CONFLICT UPDATE。因为每次都会生成新 UUID，所以它实际上更接近“创建新记忆”。

### 谁查询 memory_items

| 函数 | 查询方式 |
|---|---|
| <code>vector_search()</code> | 和 vec_items JOIN，按向量距离查询 |
| <code>keyword_search()</code> | 对 summary 使用 LIKE |
| <code>list_memories()</code> | 按用户、类型、状态、时间筛选 |
| <code>mark_superseded_batch()</code> | 查找仍为 active 的指定记忆 |

### 一个重要设计：没有 chat_id

<code>memory_items</code> 只有：

~~~text
user_id
~~~

没有：

~~~text
chat_id
~~~

因此长期记忆默认以用户为范围，可以跨不同聊天复用。

例如同一个用户：

~~~text
私聊 Bot：用户喜欢喝绿茶
        ↓
生成 memory_items(user_id=10001)
        ↓
用户以后在另一个 Chat 中询问饮品
        ↓
仍可能检索到这条长期记忆
~~~

原始来源属于哪个 Chat，则通过 <code>source_ref</code> 记录。

---

## 五、vec_items：向量索引表

### 这张表干什么

<code>vec_items</code> 是 <code>sqlite-vec</code> 创建的虚拟表，专门用于向量相似度检索。

它不保存可读的记忆正文，只保存：

~~~text
记忆 ID + 1024 维 embedding
~~~

### 表结构

~~~sql
CREATE VIRTUAL TABLE IF NOT EXISTS vec_items USING vec0(
    embedding_id TEXT PRIMARY KEY,
    embedding FLOAT[1024]
);
~~~

### 每个字段的作用

| 字段 | 类型 | 作用 |
|---|---|---|
| <code>embedding_id</code> | TEXT | 对应 memory_items.id |
| <code>embedding</code> | FLOAT[1024] | 用于相似度搜索的 1024 维向量 |

### 一条具体数据

为了方便阅读，向量只展示前几维：

| embedding_id | embedding |
|---|---|
| 11111111-1111-1111-1111-111111111111 | [0.12, -0.03, 0.27, 0.08, ... 共 1024 维] |

它对应：

~~~text
memory_items.id
11111111-1111-1111-1111-111111111111

memory_items.summary
用户喜欢喝绿茶
~~~

### 谁写入 vec_items

也是：

~~~python
MemoryStore.upsert_item()
~~~

<code>upsert_item()</code> 会先生成一次 embedding，然后在同一个事务里写两张表：

~~~text
summary
  "用户喜欢喝绿茶"
        │
        ▼
Embedder.embed(summary)
        │
        ▼
[0.12, -0.03, 0.27, ...]
        │
        ├── 写 memory_items.embedding
        │
        └── 写 vec_items.embedding
~~~

执行 SQL：

~~~sql
INSERT INTO vec_items (embedding_id, embedding)
VALUES (?, ?)
~~~

### 谁查询 vec_items

<code>MemoryStore.vector_search()</code>。

主要 SQL：

~~~sql
SELECT
    mi.id,
    mi.user_id,
    mi.memory_type,
    mi.summary,
    mi.embedding,
    mi.status,
    mi.source_ref,
    mi.created_at,
    mi.updated_at,
    vec_distance_l2(v.embedding, ?) AS distance
FROM vec_items v
JOIN memory_items mi
    ON v.embedding_id = mi.id
WHERE mi.user_id = ?
  AND mi.status IN (...)
ORDER BY distance
LIMIT ?
~~~

查询输入示例：

~~~python
await memory_store.vector_search(
    query_vec=[0.10, -0.01, 0.25, ...],
    user_id=10001,
    top_k=3,
    memory_types=["preference", "profile"],
    include_superseded=False,
)
~~~

查询过程：

~~~text
query_vec
   │
   ▼
vec_distance_l2(
    vec_items.embedding,
    query_vec
)
   │
   ▼
距离从小到大排序
   │
   ▼
JOIN memory_items
   │
   ▼
返回 MemoryItem 列表
~~~

输出示例：

~~~python
[
    MemoryItem(summary="用户喜欢喝绿茶", ...),
    MemoryItem(summary="用户希望减少咖啡摄入", ...),
]
~~~

### 为什么 memory_items 和 vec_items 都保存 embedding

当前代码把 embedding 保存了两份：

~~~text
memory_items.embedding
    BLOB
    用于恢复完整 MemoryItem

vec_items.embedding
    FLOAT[1024]
    用于 sqlite-vec 向量索引和距离计算
~~~

这是“业务数据”和“检索索引”分离的设计，但会带来一致性要求：

> 如果以后更新一条记忆的 embedding，必须同时更新两张表，否则正文返回的 embedding 与用于搜索的 embedding 可能不一致。

当前代码主要创建新记忆，没有提供更新 embedding 的完整同步流程。

### 为什么 superseded 记忆仍可能留在 vec_items

旧记忆被替代时，代码只更新：

~~~sql
UPDATE memory_items
SET status = 'superseded'
~~~

并不会删除对应的 <code>vec_items</code>。

不过向量查询会 JOIN <code>memory_items</code> 并过滤状态：

~~~sql
WHERE mi.status IN ('active')
~~~

所以默认不会返回已经 superseded 的向量记录。

---

## 六、memory_replacements：记忆替换关系表

### 这张表干什么

它记录：

> 哪条旧记忆被哪条新记忆替代。

例如用户以前说：

~~~text
用户住在北京
~~~

后来更新：

~~~text
用户已经搬到上海
~~~

数据库需要同时表达：

~~~text
旧记忆不再有效
旧记忆被哪条新记忆替代
~~~

前者由 <code>memory_items.status</code> 表达，后者由 <code>memory_replacements</code> 表达。

### 表结构

~~~sql
CREATE TABLE IF NOT EXISTS memory_replacements (
    old_id TEXT NOT NULL,
    new_id TEXT NOT NULL,
    replaced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (old_id, new_id)
);
~~~

### 每个字段的作用

| 字段 | 类型 | 作用 |
|---|---|---|
| <code>old_id</code> | TEXT | 被替代的旧 memory_items.id |
| <code>new_id</code> | TEXT | 替代它的新 memory_items.id |
| <code>replaced_at</code> | TIMESTAMP | 建立替代关系的时间 |

联合主键：

~~~sql
PRIMARY KEY (old_id, new_id)
~~~

表示同一组旧记忆、新记忆关系不能重复插入。

### 一条具体数据

| old_id | new_id | replaced_at |
|---|---|---|
| aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa | bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb | 2026-08-21 11:00:00 |

对应：

~~~text
old_id
  用户住在北京
  status = superseded

new_id
  用户已经搬到上海
  status = active
~~~

### 谁写入 memory_replacements

<code>MemoryStore.supersede()</code>：

~~~python
await memory_store.supersede(
    old_ids=[old_memory_id],
    new_id=new_memory_id,
)
~~~

它对每条旧记忆执行两步：

~~~sql
UPDATE memory_items
SET status = 'superseded',
    updated_at = CURRENT_TIMESTAMP
WHERE id = ?
~~~

然后：

~~~sql
INSERT INTO memory_replacements (old_id, new_id)
VALUES (?, ?)
~~~

数据变化图：

~~~text
更新前
memory_items
├── A 用户住在北京     active
└── B 用户搬到上海     active

执行 supersede([A], B)
        │
        ▼
更新后
memory_items
├── A 用户住在北京     superseded
└── B 用户搬到上海     active

memory_replacements
└── old_id=A, new_id=B
~~~

输入：

~~~python
old_ids = [
    UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
]
new_id = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
~~~

输出：

~~~python
None
~~~

真正结果是数据库副作用。

### relation_type 参数当前没有生效

函数签名包含：

~~~python
relation_type: str = "supersede"
~~~

但当前：

- SQL 没有使用它。
- <code>memory_replacements</code> 没有 <code>relation_type</code> 字段。

所以无论传入什么值，数据库只保存 old_id、new_id 和 replaced_at。

### mark_superseded_batch() 不写替换关系

另一个函数：

~~~python
mark_superseded_batch(...)
~~~

只会批量执行：

~~~sql
UPDATE memory_items
SET status = 'superseded'
~~~

它不会插入 <code>memory_replacements</code>。

所以两种操作不同：

| 方法 | 更新 status | 记录 old → new |
|---|---:|---:|
| <code>supersede()</code> | 是 | 是 |
| <code>mark_superseded_batch()</code> | 是 | 否 |

---

## 七、conversation_sessions：Session原文表

### 这张表干什么

<code>conversation_sessions</code> 保存用户和助手的完整对话原文。

它不是长期记忆摘要，而是类似：

~~~json
[
  {
    "role": "user",
    "content": "我喜欢喝绿茶"
  },
  {
    "role": "assistant",
    "content": "好的，我记住了"
  }
]
~~~

可以结合 [[learning_session_store]] 和 [[learning_before_turn]] 阅读。

### 表结构

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

### 每个字段的作用

| 字段 | 类型 | 是否必填 | 作用 |
|---|---|---:|---|
| <code>user_id</code> | INTEGER | 是 | Telegram 用户 ID |
| <code>chat_id</code> | INTEGER | 是 | Telegram 私聊、群聊或频道的 Chat ID |
| <code>messages_json</code> | TEXT | 是 | 当前 Session 的完整消息数组 |
| <code>last_consolidated</code> | INTEGER | 是 | 已经完成长期记忆提炼的消息游标 |
| <code>created_at</code> | TIMESTAMP | 自动 | Session 首次创建时间 |
| <code>updated_at</code> | TIMESTAMP | 自动或代码更新 | Session 最近一次保存时间 |

### 主键为什么是两个字段

~~~sql
PRIMARY KEY (user_id, chat_id)
~~~

因为一个 Session 由：

~~~text
哪个用户
    +
在哪个 Chat
~~~

共同确定。

例如：

| user_id | chat_id | Session |
|---:|---:|---|
| 10001 | 10001 | 用户 10001 与 Bot 的私聊 |
| 10001 | -100888 | 用户 10001 在群聊 -100888 中的会话 |
| 20002 | -100888 | 用户 20002 在同一群聊中的会话 |

### 一条具体数据

| user_id | chat_id | messages_json | last_consolidated |
|---:|---:|---|---:|
| 10001 | 20001 | [{"role":"user","content":"我喜欢喝绿茶"},{"role":"assistant","content":"好的，我记住了"}] | 2 |

这里一行不是一条消息，而是：

> 一个 Session 的全部消息都序列化到同一个 messages_json 字段中。

### 谁写入 conversation_sessions

<code>SessionStore.save()</code>。

输入示例：

~~~python
session_store.save(
    user_id=10001,
    chat_id=20001,
    messages=[
        {"role": "user", "content": "我喜欢喝绿茶"},
        {"role": "assistant", "content": "好的，我记住了"},
    ],
    last_consolidated=2,
)
~~~

保存前：

~~~python
json.dumps(messages, ensure_ascii=False)
~~~

得到：

~~~json
[{"role":"user","content":"我喜欢喝绿茶"},{"role":"assistant","content":"好的，我记住了"}]
~~~

执行的是 UPSERT：

~~~sql
INSERT INTO conversation_sessions (
    user_id,
    chat_id,
    messages_json,
    last_consolidated,
    updated_at
)
VALUES (?, ?, ?, COALESCE(?, 0), CURRENT_TIMESTAMP)
ON CONFLICT(user_id, chat_id) DO UPDATE SET
    messages_json = excluded.messages_json,
    last_consolidated = COALESCE(
        ?,
        conversation_sessions.last_consolidated
    ),
    updated_at = CURRENT_TIMESTAMP
~~~

输出：

~~~python
None
~~~

结果是：

- Session 不存在就插入。
- Session 已存在就覆盖整个 <code>messages_json</code>。
- 传入 <code>last_consolidated</code> 就更新。
- 未传游标时保留数据库原值。

### 谁读取 conversation_sessions

#### load_state()

输入：

~~~python
session_store.load_state(
    user_id=10001,
    chat_id=20001,
)
~~~

SQL：

~~~sql
SELECT messages_json, last_consolidated
FROM conversation_sessions
WHERE user_id = ? AND chat_id = ?
~~~

输出：

~~~python
(
    [
        {"role": "user", "content": "我喜欢喝绿茶"},
        {"role": "assistant", "content": "好的，我记住了"},
    ],
    2,
)
~~~

不存在则返回：

~~~python
None
~~~

#### search_messages()

SQL 先取出用户的所有 Session：

~~~sql
SELECT chat_id, messages_json
FROM conversation_sessions
WHERE user_id = ?
ORDER BY updated_at DESC
~~~

然后在 Python 中：

~~~text
json.loads(messages_json)
        ↓
逐条遍历消息
        ↓
比较 content 是否包含 query
        ↓
生成 seq 和 source_ref
~~~

输出示例：

~~~python
(
    [
        {
            "role": "user",
            "content": "我喜欢喝绿茶",
            "seq": 0,
            "chat_id": 20001,
            "source_ref": (
                "session:10001:20001#msg:0"
            ),
        }
    ],
    1,
)
~~~

注意：

> <code>search_messages()</code> 不是 SQL 全文搜索。它先把整段 JSON 读进 Python，再逐条扫描。

#### fetch_messages()

它先调用 <code>load()</code> 读取整个 messages_json，然后根据 <code>seq</code> 在 Python list 中切片。

输入：

~~~python
session_store.fetch_messages(
    user_id=10001,
    chat_id=20001,
    seq=0,
    context=1,
)
~~~

输出：

~~~python
(
    [
        {
            "role": "user",
            "content": "我喜欢喝绿茶",
            "seq": 0,
            "source_ref": "session:10001:20001#msg:0",
            "in_source_ref": True,
        },
        {
            "role": "assistant",
            "content": "好的，我记住了",
            "seq": 1,
            "source_ref": "session:10001:20001#msg:1",
            "in_source_ref": False,
        },
    ],
    1,
)
~~~

### 谁删除 conversation_sessions

~~~python
session_store.delete(
    user_id=10001,
    chat_id=20001,
)
~~~

SQL：

~~~sql
DELETE FROM conversation_sessions
WHERE user_id = ? AND chat_id = ?
~~~

输出：

~~~python
None
~~~

### last_consolidated 怎么理解

假设 Session 有 10 条消息：

~~~text
seq 0
seq 1
seq 2
...
seq 9
~~~

如果：

~~~text
last_consolidated = 6
~~~

可以理解成前面一部分消息已经参与过长期记忆提炼，后续 Consolidation 主要关注新增加的消息。

它是游标，不是摘要正文。

### 老数据库的迁移

旧版本可能没有：

~~~text
last_consolidated
~~~

启动时执行：

~~~sql
PRAGMA table_info(conversation_sessions)
~~~

如果字段不存在：

~~~sql
ALTER TABLE conversation_sessions
ADD COLUMN last_consolidated INTEGER NOT NULL DEFAULT 0
~~~

这是一种轻量级迁移。

---

## 八、一次新长期记忆会写哪些表

调用：

~~~python
await memory_store.upsert_item(
    memory_type="preference",
    summary="用户喜欢喝绿茶",
    user_id=10001,
    source_ref="session:10001:20001#msg:0",
)
~~~

完整链路：

~~~text
summary
  "用户喜欢喝绿茶"
        │
        ▼
Embedder.embed(summary)
        │
        ▼
生成 1024 维向量
        │
        ▼
生成 UUID：1111...
        │
        ├──────────────────────────┐
        ▼                          ▼
INSERT memory_items          INSERT vec_items
id=1111...                   embedding_id=1111...
summary=用户喜欢喝绿茶         embedding=[...]
        │                          │
        └───────────┬──────────────┘
                    ▼
                conn.commit()
                    ▼
             返回 MemoryItem
~~~

两次 INSERT 使用同一个数据库事务。只有最后 <code>commit()</code> 后才正式保存。

---

## 九、一次混合检索会查询哪些表

<code>DefaultMemoryEngine._search_memories()</code>：

~~~text
用户查询
   │
   ├── 向量检索
   │      │
   │      ├── vec_items
   │      │     计算向量距离
   │      │
   │      └── JOIN memory_items
   │            获取正文、类型、状态、来源
   │
   └── 关键词检索
          │
          └── memory_items
                summary LIKE '%query%'
   │
   ▼
Python 中执行 RRF 融合
   │
   ▼
_MemorySearchResult
~~~

不会查询：

~~~text
conversation_sessions
memory_replacements
~~~

### 向量检索

涉及：

~~~text
vec_items + memory_items
~~~

### 关键词检索

只涉及：

~~~text
memory_items
~~~

### RRF 融合

不涉及数据库表，在 Python 内存中完成。

---

## 十、Session怎样变成长期记忆

~~~text
conversation_sessions
  保存原始对话
        │
        │ ConsolidationWorker
        │ 读取尚未 consolidated 的消息
        ▼
LLM 提取长期信息
        │
        ▼
MemoryStore.upsert_item()
        │
        ├── memory_items
        └── vec_items
        │
        ▼
更新 conversation_sessions.last_consolidated
~~~

举例：

原始 Session：

~~~text
用户：我平时下午喜欢喝绿茶。
助手：记住了。
~~~

<code>conversation_sessions.messages_json</code>：

~~~json
[
  {"role":"user","content":"我平时下午喜欢喝绿茶。"},
  {"role":"assistant","content":"记住了。"}
]
~~~

提炼后的 <code>memory_items</code>：

~~~text
memory_type = preference
summary = 用户下午偏好喝绿茶
source_ref = session:10001:20001#msg:0-1
~~~

对应的 <code>vec_items</code>：

~~~text
embedding_id = memory_items.id
embedding = Embedder("用户下午偏好喝绿茶")
~~~

---

## 十一、每张表的增删改查汇总

| 表 | 创建 | 查询 | 更新 | 删除 |
|---|---|---|---|---|
| <code>memory_items</code> | upsert_item() INSERT | vector_search、keyword_search、list_memories | supersede、mark_superseded_batch | 业务代码中没有通用单条物理删除方法 |
| <code>vec_items</code> | upsert_item() INSERT | vector_search | 当前没有常规更新方法 | 测试和评估清理代码会删除 |
| <code>memory_replacements</code> | supersede() INSERT | 当前主要用于记录，普通检索链路不读取 | 无 | 测试和评估清理代码会删除 |
| <code>conversation_sessions</code> | SessionStore.save() INSERT | load、load_state、fetch_messages、search_messages | SessionStore.save() UPSERT | SessionStore.delete() |

---

## 十二、四张表的具体样例

### conversation_sessions

| user_id | chat_id | messages_json | last_consolidated |
|---:|---:|---|---:|
| 10001 | 20001 | [{"role":"user","content":"我喜欢喝绿茶"},{"role":"assistant","content":"记住了"}] | 2 |

### memory_items

| id | user_id | memory_type | summary | status | source_ref |
|---|---:|---|---|---|---|
| 1111... | 10001 | preference | 用户喜欢喝绿茶 | active | session:10001:20001#msg:0-1 |

### vec_items

| embedding_id | embedding |
|---|---|
| 1111... | [0.12, -0.03, 0.27, ... 共1024维] |

### memory_replacements

假设以后新记忆 2222 替代 1111：

| old_id | new_id | replaced_at |
|---|---|---|
| 1111... | 2222... | 2026-08-21 12:00:00 |

同时：

~~~text
memory_items 1111...
status = superseded

memory_items 2222...
status = active
~~~

---

## 十三、几个容易忽略的设计点

### 1. Session 是原文，MemoryItem 是摘要

~~~text
conversation_sessions
  原始 user/assistant 消息

memory_items
  从原始消息提炼出的长期信息
~~~

### 2. 一条 Session 记录包含多条消息

这个项目不是：

~~~text
一条消息 = 数据库一行
~~~

而是：

~~~text
一个 user_id + chat_id Session = 数据库一行
全部消息 = messages_json 一个字段
~~~

Session 很长时，每次保存会重写整段 JSON。

### 3. 长期记忆按 user_id 隔离

<code>memory_items</code> 没有 chat_id，因此长期记忆可以跨 Chat 使用。

### 4. embedding 固定 1024 维

~~~sql
embedding FLOAT[1024]
~~~

因此所用 embedding 模型必须输出 1024 维向量，或者在写入前进行兼容转换。

如果模型改成其他维度，<code>vec_items</code> 可能无法正常插入或检索。

### 5. embedding 保存两份

~~~text
memory_items.embedding
vec_items.embedding
~~~

创建时两份来自同一个向量，但以后更新必须注意同步。

### 6. 替代不是删除

旧记忆仍保存在：

~~~text
memory_items
vec_items
~~~

只是：

~~~text
memory_items.status = superseded
~~~

默认查询通过 status 把它过滤掉。

### 7. 没有外键约束

四张表之间的关系靠 ID 字符串和应用代码维护。

### 8. memory_replacements 当前不参与普通检索

默认召回主要查询：

~~~text
memory_items
vec_items
~~~

<code>memory_replacements</code> 主要用于记录历史关系，不直接决定 RRF 排序。

### 9. search_messages 是 Python 扫描

<code>conversation_sessions.messages_json</code> 是 JSON 文本，因此原始消息搜索会把 Session 读出来后在 Python 中遍历。

数据量增大后，这种方式的性能会弱于“一条消息一行 + 数据库索引”的设计。

---

## 十四、get_connection() 如何管理数据库连接

~~~python
def get_connection() -> sqlite3.Connection:
    ...
~~~

它使用：

~~~python
threading.local()
~~~

为每个线程保存自己的 SQLite 连接。

流程：

~~~text
当前线程调用 get_connection()
        │
        ▼
thread-local 中是否已有 conn
        │
        ├── 有 → 直接复用
        │
        └── 没有
              ↓
          获取全局锁
              ↓
          sqlite3.connect()
              ↓
          加载 sqlite_vec
              ↓
          保存到当前线程
~~~

输入：

~~~python
get_connection()
~~~

输出：

~~~python
sqlite3.Connection
~~~

每个新连接都需要重新加载 <code>sqlite_vec</code>，否则这个连接无法执行向量相关 SQL。

---

## 十五、快速记忆

~~~text
conversation_sessions
  对话原文仓库

memory_items
  长期记忆正文仓库

vec_items
  长期记忆向量索引

memory_replacements
  旧记忆到新记忆的替代关系
~~~

最关键的关联：

~~~text
vec_items.embedding_id
    =
memory_items.id
~~~

最关键的来源追踪：

~~~text
memory_items.source_ref
    →
conversation_sessions 中的某条原始消息
~~~

最关键的替代关系：

~~~text
memory_replacements.old_id
    →
memory_replacements.new_id
~~~

最关键的 Session 主键：

~~~text
(user_id, chat_id)
~~~

一句话总结：

> <code>conversation_sessions</code> 负责保存“说过什么”，<code>memory_items</code> 负责保存“长期记住什么”，<code>vec_items</code> 负责回答“哪条记忆最相关”，<code>memory_replacements</code> 负责记录“旧记忆被什么新记忆替代”。
