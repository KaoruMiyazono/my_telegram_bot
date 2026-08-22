# telegram-bot 项目学习笔记

## `persistence/database.py`：`init_db()` 初始化数据库

### 代码的作用

`init_db()` 在程序启动时初始化 SQLite 数据库。它主要完成以下工作：

1. 根据配置得到数据库文件路径。
2. 创建数据库文件所在的目录。
3. 建立一个临时 SQLite 连接。
4. 加载 `sqlite-vec` 扩展，使 SQLite 支持向量数据和向量检索。
5. 执行 `TABLE_SCHEMA`，创建记忆、向量、记忆替换关系和聊天会话相关的表。
6. 对旧版本的 `conversation_sessions` 表执行轻量迁移。
7. 提交数据库事务并关闭初始化连接。

该函数只负责数据库的启动初始化。业务代码后续访问数据库时，使用的是 `get_connection()` 提供的线程本地连接。

### 代码是如何实现的

原始代码：

```python
def init_db() -> None:
    """Initialize database with schema."""
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
```

逐步理解：

#### 1. 获取数据库路径

```python
db_path = Path(settings.DATABASE_PATH)
```

- `settings.DATABASE_PATH` 来自项目配置，默认值是 `./data/memory.db`。
- `Path(...)` 把字符串路径转换成 `pathlib.Path` 对象，方便后续处理目录和文件路径。
- 如果使用默认配置并在项目根目录启动，数据库文件最终位于 `data/memory.db`。

#### 2. 创建数据库所在目录

```python
db_path.parent.mkdir(parents=True, exist_ok=True)
```

- `db_path.parent` 得到数据库文件的父目录，例如 `./data`。
- `parents=True` 表示缺少多层父目录时一起创建。
- `exist_ok=True` 表示目录已经存在时不报错。
- 这一步创建的是目录；真正的数据库文件会在连接 SQLite 时创建。

#### 3. 建立 SQLite 连接

```python
conn = sqlite3.connect(str(db_path))
```

- 连接指定的 SQLite 数据库文件。
- 文件不存在时，SQLite 会自动创建它。
- `conn` 是数据库连接对象，后面的建表、迁移和事务提交都通过它完成。

#### 4. 加载 `sqlite-vec` 扩展

```python
conn.enable_load_extension(True)
sqlite_vec.load(conn)
conn.enable_load_extension(False)
```

- Python 的 SQLite 连接默认不允许随意加载外部扩展。
- 第一行临时允许加载扩展。
- `sqlite_vec.load(conn)` 把 `sqlite-vec` 注册到当前连接，使它能够识别 `vec0` 虚拟表以及相关的向量操作。
- 加载完成后立即关闭扩展加载权限，缩小允许动态加载扩展的时间范围。
- 扩展是加载到当前数据库连接上的，因此其他新连接也要各自加载一次；这就是 `get_connection()` 中再次调用 `sqlite_vec.load(conn)` 的原因。

#### 5. 执行建表脚本

```python
conn.executescript(TABLE_SCHEMA)
```

- `TABLE_SCHEMA` 是一段包含多条 SQL 语句的字符串。
- `executescript()` 一次执行其中所有建表语句。
- 建表语句都使用了 `IF NOT EXISTS`，所以重复启动程序不会因为表已经存在而报错。
- `IF NOT EXISTS` 只能避免重复创建表，不会自动给旧表增加新字段。

#### 6. 兼容旧数据库结构

```python
_ensure_conversation_session_columns(conn)
```

这个函数先执行：

```sql
PRAGMA table_info(conversation_sessions)
```

查询 `conversation_sessions` 当前包含哪些字段。如果没有 `last_consolidated`，则执行：

```sql
ALTER TABLE conversation_sessions
ADD COLUMN last_consolidated INTEGER NOT NULL DEFAULT 0
```

这样做是因为旧版本数据库可能已经存在 `conversation_sessions` 表，但没有 `last_consolidated` 字段。前面的 `CREATE TABLE IF NOT EXISTS` 遇到已有表时不会修改其结构，所以需要单独执行迁移。

对于全新创建的数据库，`last_consolidated` 已包含在建表语句中，这一步不会执行 `ALTER TABLE`。

#### 7. 提交并关闭连接

```python
conn.commit()
conn.close()
```

- `commit()` 提交建表和迁移产生的数据库变更。
- `close()` 释放这次初始化使用的数据库连接。

### 数据库操作

`init_db()` 会执行以下数据库操作：

1. 连接或创建 `settings.DATABASE_PATH` 指定的 SQLite 数据库文件。
2. 创建 `memory_items` 普通表。
3. 创建 `vec_items` 向量虚拟表。
4. 创建 `memory_replacements` 普通表。
5. 创建 `conversation_sessions` 普通表。
6. 查询 `conversation_sessions` 的字段信息。
7. 必要时为旧表增加 `last_consolidated` 字段。
8. 提交事务并关闭连接。

### 表结构

#### `memory_items`：长期记忆主表

作用：保存从聊天中提炼出的长期记忆及其状态、来源和时间信息。

| 字段 | 类型 | 约束/默认值 | 作用 |
|---|---|---|---|
| `id` | `TEXT` | 主键 | 一条记忆的唯一标识。项目中通常对应 UUID 的字符串形式。 |
| `user_id` | `INTEGER` | 非空 | 这条记忆所属的 Telegram 用户 ID，用于隔离不同用户的数据。 |
| `memory_type` | `TEXT` | 非空 | 记忆类型，例如 `profile`、`preference`、`procedure`、`event` 或 `fact`。 |
| `summary` | `TEXT` | 非空 | 提炼后的记忆文本，是提供给检索和 LLM 使用的主要内容。 |
| `embedding` | `BLOB` | 可为空 | 记忆对应的向量二进制数据。当前项目同时使用 `vec_items` 保存可供向量索引查询的数据。 |
| `status` | `TEXT` | 非空，默认 `active` | 记忆状态。`active` 表示当前有效，旧信息被替代后通常会变成 `superseded`。 |
| `source_ref` | `TEXT` | 可为空 | 记忆来源引用，例如 `session:用户ID:聊天ID#msg:消息序号`，用于回查原始聊天证据。 |
| `created_at` | `TIMESTAMP` | 默认当前时间 | 记忆创建时间。 |
| `updated_at` | `TIMESTAMP` | 默认当前时间 | 记忆最后更新时间。 |

#### `vec_items`：向量检索虚拟表

作用：使用 `sqlite-vec` 的 `vec0` 虚拟表保存固定维度的 embedding，支持相似度检索。

| 字段 | 类型 | 约束 | 作用 |
|---|---|---|---|
| `embedding_id` | `TEXT` | 主键 | 向量的唯一标识，逻辑上用于对应 `memory_items.id`。SQL 中没有声明外键约束。 |
| `embedding` | `FLOAT[1024]` | 1024 维 | 一条记忆的 1024 维浮点向量，维度需要与项目使用的 embedding 模型输出一致。 |

这里创建的是 `sqlite-vec` 虚拟表，不是普通 SQLite 表，也不是单独的一条传统 B-tree 索引。

#### `memory_replacements`：记忆替换关系表

作用：记录一条旧记忆被哪条新记忆替代，用于追踪记忆更新关系。

| 字段 | 类型 | 约束/默认值 | 作用 |
|---|---|---|---|
| `old_id` | `TEXT` | 非空、联合主键之一 | 被替代的旧记忆 ID。 |
| `new_id` | `TEXT` | 非空、联合主键之一 | 替代旧记忆的新记忆 ID。 |
| `replaced_at` | `TIMESTAMP` | 默认当前时间 | 替代关系建立的时间。 |

联合主键为：

```sql
PRIMARY KEY (old_id, new_id)
```

它防止同一组“旧记忆 → 新记忆”替换关系被重复写入。表结构没有声明外键，因此引用关系由应用代码维护。

#### `conversation_sessions`：聊天会话表

作用：按用户和聊天保存原始消息历史，以及长期记忆整理进度。

| 字段 | 类型 | 约束/默认值 | 作用 |
|---|---|---|---|
| `user_id` | `INTEGER` | 非空、联合主键之一 | Telegram 用户 ID。 |
| `chat_id` | `INTEGER` | 非空、联合主键之一 | Telegram 聊天 ID。一个用户可能参与不同聊天，因此需要和 `user_id` 一起确定会话。 |
| `messages_json` | `TEXT` | 非空，默认 `[]` | 使用 JSON 字符串保存完整的 user/assistant 消息列表。 |
| `last_consolidated` | `INTEGER` | 非空，默认 `0` | 记录已经完成长期记忆提炼的消息位置，避免重复整理相同消息。 |
| `created_at` | `TIMESTAMP` | 默认当前时间 | 会话首次创建时间。 |
| `updated_at` | `TIMESTAMP` | 默认当前时间 | 会话最后更新时间。 |

联合主键为：

```sql
PRIMARY KEY (user_id, chat_id)
```

因此同一名用户在同一个聊天中只会有一条会话记录。

### `init_db()` 在启动链路中的位置

项目在 `main.py` 开始启动时首先调用：

```python
init_db()
```

数据库准备完成后，程序才会继续创建 `Embedder`、`MemoryStore`、`SessionStore` 和其他组件。这样后续组件开始读写数据时，所需的表和 `sqlite-vec` 支持已经就绪。

### 阅读时需要记住的几个关键点

- `mkdir()` 创建目录，`sqlite3.connect()` 才会在需要时创建数据库文件。
- `sqlite_vec.load(conn)` 的作用范围是当前连接，新连接需要重新加载扩展。
- `CREATE TABLE IF NOT EXISTS` 不负责升级旧表结构。
- `_ensure_conversation_session_columns()` 是一次轻量数据库迁移。
- `memory_items` 保存记忆内容，`vec_items` 保存用于相似度检索的向量。
- `conversation_sessions` 保存原始消息，和长期记忆表承担不同职责。
