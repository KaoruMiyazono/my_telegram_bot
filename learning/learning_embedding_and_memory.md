# telegram-bot 项目学习笔记

## `Embedder` 和 `MemoryStore`：向量生成与长期记忆存储

`main.py` 中的相关代码：

```python
embedder = Embedder()
memory_store = MemoryStore(embedder)
```

这两行代码建立了长期记忆系统最底层的依赖关系：

```text
文本
  ↓
Embedder
  ↓ 调用阿里云 DashScope Embedding API
1024 维向量
  ↓
MemoryStore
  ├── memory_items：保存记忆文本、状态和来源
  ├── vec_items：保存向量并支持相似度搜索
  └── memory_replacements：保存新旧记忆替换关系
```

`Embedder` 只负责“文本和向量之间的转换”。

`MemoryStore` 负责“长期记忆的写入、查询和状态更新”，并在需要把文本写成记忆时调用 `Embedder`。

---

## 一、`Embedder`：将文本转换成向量

文件位置：`memory/embedder.py`

### 代码的作用

`Embedder` 封装阿里云 DashScope 的 Embedding 服务，将一段文本转换为浮点数向量。

普通文本不能直接进行语义距离计算。例如：

```text
我喜欢喝拿铁
我平时偏爱牛奶咖啡
```

两句话使用的词不完全一样，但语义比较接近。Embedding 模型会把它们转换为向量，后续可以通过向量距离判断语义是否相似。

这个类本身不操作数据库，数据库写入和查询由 `MemoryStore` 负责。

### `Embedder.__init__()` 如何实现

原始代码：

```python
class Embedder:
    def __init__(self) -> None:
        self.client = AsyncOpenAI(
            api_key=settings.ALIYUN_DASHSCOPE_API_KEY,
            base_url=settings.EMBEDDING_BASE_URL,
            timeout=30.0,
        )
        self.model = settings.EMBEDDING_MODEL
```

#### 1. 创建异步 API 客户端

```python
self.client = AsyncOpenAI(...)
```

项目使用 OpenAI Python SDK 的 `AsyncOpenAI` 客户端，但请求目标不是 OpenAI 官方服务，而是阿里云 DashScope 提供的 OpenAI 兼容接口。

配置参数：

| 参数 | 配置来源 | 作用 |
|---|---|---|
| `api_key` | `ALIYUN_DASHSCOPE_API_KEY` | 访问阿里云 Embedding 服务的密钥。 |
| `base_url` | `EMBEDDING_BASE_URL` | API 地址，默认是 DashScope 的 OpenAI 兼容接口。 |
| `timeout` | 固定为 `30.0` | 单次 API 请求最多等待 30 秒。 |

执行 `Embedder()` 时只创建客户端对象，不会立刻发送网络请求。真正的请求发生在调用 `embed()` 时。

#### 2. 保存模型名称

```python
self.model = settings.EMBEDDING_MODEL
```

默认模型为：

```text
text-embedding-v3
```

后面请求 Embedding API 时会把这个模型名称传给服务端。

### `Embedder.embed()` 如何实现

原始代码：

```python
async def embed(self, text: str) -> list[float]:
    """Generate embedding for the given text with retry."""
    for attempt in range(2):
        try:
            response = await self.client.embeddings.create(
                model=self.model,
                input=text,
            )
            return response.data[0].embedding
        except Exception as e:
            if attempt == 0:
                await asyncio.sleep(0.5)
                continue
            raise
    return []
```

#### 输入和输出

输入：

```python
text: str
```

例如：

```python
"用户喜欢喝拿铁"
```

输出：

```python
list[float]
```

也就是由许多浮点数组成的向量。当前数据库的 `vec_items.embedding` 被声明为 `FLOAT[1024]`，因此正常情况下这里需要得到 1024 维向量。

#### 调用 Embedding API

```python
response = await self.client.embeddings.create(
    model=self.model,
    input=text,
)
```

- `await` 表示等待异步网络请求完成。
- `model` 指定使用哪个 Embedding 模型。
- `input` 是要转换的原始文本。

API 返回结果后，取第一条数据的向量：

```python
return response.data[0].embedding
```

一次调用只传入一段文本，因此代码读取 `data[0]`。

#### 重试机制

```python
for attempt in range(2):
```

最多尝试两次：

1. 第一次请求失败时等待 0.5 秒。
2. 然后进行第二次请求。
3. 第二次仍然失败时，使用 `raise` 把原异常继续抛给上层。

```python
if attempt == 0:
    await asyncio.sleep(0.5)
    continue
raise
```

这里捕获的是所有 `Exception`，没有区分超时、限流、认证失败或者参数错误。因此，即使错误本身不适合重试，第一次失败后也会再尝试一次。

函数最后的：

```python
return []
```

按照当前控制流程实际上不会执行：成功时已经返回向量，第二次失败时会抛出异常。它主要用于满足静态类型检查器对函数返回值的判断。

### `Embedder.close()` 如何实现

```python
async def close(self) -> None:
    await self.client.close()
```

它关闭 `AsyncOpenAI` 客户端内部使用的 HTTP 连接资源。

当前 `main.py` 创建了 `Embedder`，但退出时没有显式调用 `await embedder.close()`。如果以后完善优雅退出，可以在 `main()` 的 `finally` 中考虑关闭它。

### `Embedder` 是否操作数据库

不操作。

它只负责：

```text
文本 → 网络请求 → 浮点向量
```

---

## 二、`MemoryStore`：管理长期记忆

文件位置：`memory/store.py`

### 代码的作用

`MemoryStore` 是长期记忆数据库的底层访问类，主要负责：

- 创建一条长期记忆并生成对应向量。
- 根据向量相似度搜索长期记忆。
- 根据关键词搜索长期记忆。
- 按类型、状态和时间列出记忆。
- 把已经失效的旧记忆标记为 `superseded`。
- 记录旧记忆被新记忆替代的关系。

长期记忆允许的主要类型定义为：

```python
LONG_TERM_MEMORY_TYPES = [
    "profile",
    "preference",
    "procedure",
    "event",
    "fact",
]
```

| 类型 | 大致含义 | 示例 |
|---|---|---|
| `profile` | 用户档案或身份信息 | 用户是一名后端工程师。 |
| `preference` | 用户偏好 | 用户喜欢喝拿铁。 |
| `procedure` | 操作规则或流程 | 运行评测前需要清理数据库。 |
| `event` | 发生过的事件 | 用户上周更换了手机。 |
| `fact` | 可以复用的事实 | 用户的打印机型号是 Brother HL-L2460DW。 |

### 为什么把 `embedder` 传给 `MemoryStore`

原始代码：

```python
class MemoryStore:
    def __init__(self, embedder: Embedder) -> None:
        self.embedder = embedder
```

`MemoryStore` 不自己创建 `Embedder`，而是从外部接收一个已经创建好的实例，这种方式叫依赖注入。

这样做的作用包括：

- 整个程序可以复用同一个 Embedding 客户端。
- 测试时可以传入假的 Embedder，避免调用真实 API。
- `MemoryStore` 只关心 `embedder.embed()` 能返回向量，不需要负责 API 客户端配置。
- 对象的创建顺序和依赖关系在 `main.py` 中非常清楚。

执行：

```python
memory_store = MemoryStore(embedder)
```

时只是把对象保存到：

```python
self.embedder
```

此时不会调用 Embedding API，也不会连接数据库。

---

## 三、`MemoryStore.upsert_item()`：写入长期记忆

### 代码的作用

把一段记忆摘要写入 `memory_items`，并把它的向量写入 `vec_items`。

虽然方法名叫 `upsert_item`，当前实现实际总是生成一个新的 UUID 并执行两次 `INSERT`；它没有根据已有 ID 执行更新，也没有使用 SQLite 的 `ON CONFLICT DO UPDATE`。因此从当前代码行为看，它更接近“创建一条新记忆”。

### 实现步骤

#### 1. 将记忆摘要转换为向量

```python
embedding = await self.embedder.embed(summary)
```

这一行会真正调用 DashScope Embedding API。如果网络请求最终失败，异常会向上传递，后续数据库插入不会执行。

#### 2. 生成记忆 ID

```python
item_id = uuid4()
```

每条记忆使用一个新的 UUID 作为唯一标识。

#### 3. 获取数据库连接和游标

```python
conn = get_connection()
cursor = conn.cursor()
```

`get_connection()` 返回当前线程复用的 SQLite 连接。`cursor` 用来执行 SQL。

#### 4. 写入 `memory_items`

```sql
INSERT INTO memory_items
    (id, user_id, memory_type, summary, embedding, status, source_ref)
VALUES (?, ?, ?, ?, ?, 'active', ?)
```

写入内容包括：

| 字段 | 写入值 |
|---|---|
| `id` | 新生成 UUID 的字符串。 |
| `user_id` | 记忆所属用户。 |
| `memory_type` | 记忆类型。 |
| `summary` | 记忆摘要文本。 |
| `embedding` | 使用 `_encode_embedding()` 转换后的向量二进制。 |
| `status` | 固定写入 `active`，表示当前有效。 |
| `source_ref` | 原始消息来源，可为空。 |

SQL 使用 `?` 参数绑定，而不是把业务值直接拼接进 SQL，可以避免业务文本破坏 SQL 结构。

#### 5. 写入 `vec_items`

```sql
INSERT INTO vec_items (embedding_id, embedding)
VALUES (?, ?)
```

- `embedding_id` 和 `memory_items.id` 使用同一个 UUID。
- `embedding` 保存同一条记忆的向量。
- 后续向量搜索通过 `vec_items.embedding_id = memory_items.id` 把向量结果和记忆内容连接起来。

#### 6. 提交事务

```python
conn.commit()
```

两次 `INSERT` 在同一次提交中持久化。只有提交完成，数据库修改才正式保存。

#### 7. 返回领域对象

```python
return MemoryItem(...)
```

方法不是返回数据库行，而是构造一个 `MemoryItem` 对象，让上层继续使用。

### 数据库操作

| 表 | 操作 | 作用 |
|---|---|---|
| `memory_items` | `INSERT` | 保存记忆内容、所属用户、类型、状态和来源。 |
| `vec_items` | `INSERT` | 保存用于相似度搜索的 1024 维向量。 |

---

## 四、`MemoryStore.vector_search()`：向量相似度搜索

### 代码的作用

根据已经生成好的查询向量，在指定用户的长期记忆中找到语义距离最近的若干条记忆。

注意：这个方法接收的是 `query_vec: list[float]`，不会在内部把查询文本转换为向量。上层需要先调用 `Embedder`，或者通过 `MemoryEngine` 完成这一步。

### 实现步骤

#### 1. 把查询向量编码成二进制

```python
query_bytes = _encode_embedding(query_vec)
```

这样才能把查询向量传给 SQLite 和 `sqlite-vec`。

#### 2. 动态生成记忆类型过滤条件

如果提供：

```python
memory_types=["profile", "preference"]
```

代码会生成：

```sql
AND mi.memory_type IN (?, ?)
```

#### 3. 确定状态范围

默认只搜索：

```text
active
```

如果 `include_superseded=True`，则同时搜索：

```text
active, superseded
```

这保证“当前偏好是什么”之类的问题默认不会被已失效的旧记忆干扰。

#### 4. 连接两张表并计算距离

SQL 核心逻辑：

```sql
FROM vec_items v
JOIN memory_items mi ON v.embedding_id = mi.id
WHERE mi.user_id = ?
ORDER BY vec_distance_l2(v.embedding, ?)
LIMIT ?
```

实际代码把距离计算为一列：

```sql
vec_distance_l2(v.embedding, ?) AS distance
```

`vec_distance_l2` 计算两个向量之间的 L2（欧氏）距离。距离越小，表示向量越接近，因此查询使用：

```sql
ORDER BY distance
```

按距离从小到大排列。

#### 5. 把数据库行转换为 `MemoryItem`

每一行结果都会转换为一个 `MemoryItem`：

- 数据库中的 ID 字符串转换为 `UUID`。
- BLOB 向量通过 `_decode_embedding()` 转回 `list[float]`。
- 时间字符串通过 `datetime.fromisoformat()` 转为 `datetime`。

SQL 虽然计算了 `distance`，但当前返回的 `MemoryItem` 不包含 distance，因此调用者只能得到排序后的记忆，不能直接读取每条结果的原始距离。

### 数据库操作

| 表 | 操作 | 作用 |
|---|---|---|
| `vec_items` | `SELECT` | 读取向量并计算 L2 距离。 |
| `memory_items` | `JOIN + SELECT` | 根据向量 ID 得到记忆正文、类型、状态和来源。 |

---

## 五、`MemoryStore.keyword_search()`：关键词搜索

### 代码的作用

直接在 `memory_items.summary` 中查找包含关键词的长期记忆，不使用 Embedding API，也不访问 `vec_items`。

核心条件：

```sql
summary LIKE ?
```

参数值为：

```python
f"%{terms}%"
```

例如 `terms="咖啡"` 时，查询条件相当于：

```sql
summary LIKE '%咖啡%'
```

### 查询条件

- 必须属于指定 `user_id`。
- 默认只查 `active` 记忆。
- 可以选择包含 `superseded` 记忆。
- 可以限制 `memory_type`。
- 按创建时间从新到旧排序。
- 使用 `limit` 限制返回数量。

这个方法是子字符串模糊匹配，不是全文搜索，也不会自动理解同义词。查询“拿铁”不会因为语义相似而必然命中只写了“牛奶咖啡”的记忆。

### 数据库操作

| 表 | 操作 | 作用 |
|---|---|---|
| `memory_items` | `SELECT` | 根据用户、状态、类型和摘要关键词查询记忆。 |

---

## 六、`MemoryStore.list_memories()`：按条件列出记忆

### 代码的作用

列出一个用户的长期记忆，并支持以下过滤条件：

- 记忆类型 `memory_types`
- 创建开始时间 `created_start`
- 创建结束时间 `created_end`
- 是否包含旧记忆 `include_superseded`
- 返回数量 `limit`

### 实现方式

代码先构造一个条件列表：

```python
clauses = ["user_id = ?", "status IN (...)"]
```

然后根据传入参数追加条件，最后使用：

```python
" AND ".join(clauses)
```

组合为完整的 `WHERE` 语句。

时间范围采用左闭右开：

```sql
created_at >= created_start
created_at < created_end
```

`limit` 会被限制到 1～200：

```python
max(1, min(int(limit), 200))
```

查询结果按 `created_at DESC` 排序，也就是最新记忆排在前面。

### 数据库操作

| 表 | 操作 | 作用 |
|---|---|---|
| `memory_items` | `SELECT` | 按用户、状态、类型和创建时间列出记忆。 |

---

## 七、`MemoryStore.supersede()`：建立新旧记忆替换关系

### 代码的作用

当新记忆替代旧记忆时：

1. 把旧记忆状态改成 `superseded`。
2. 在 `memory_replacements` 中记录旧记忆与新记忆的关系。

例如：

```text
旧记忆：用户喜欢喝咖啡
新记忆：用户现在改喝茶
```

旧记忆不会直接被删除，而是保留并标记为已经被替代。这样系统仍然可以回答“用户以前喜欢喝什么”之类的历史问题。

### 数据库操作

对每一个 `old_id` 执行：

```sql
UPDATE memory_items
SET status = 'superseded', updated_at = CURRENT_TIMESTAMP
WHERE id = ?
```

然后记录替换关系：

```sql
INSERT INTO memory_replacements (old_id, new_id)
VALUES (?, ?)
```

全部处理完后统一执行一次：

```python
conn.commit()
```

| 表 | 操作 | 作用 |
|---|---|---|
| `memory_items` | `UPDATE` | 把旧记忆标记为 `superseded` 并更新时间。 |
| `memory_replacements` | `INSERT` | 记录旧记忆被哪条新记忆替代。 |

参数中虽然存在：

```python
relation_type: str = "supersede"
```

但当前方法内部没有使用 `relation_type`，数据库表里也没有对应字段。

---

## 八、`MemoryStore.mark_superseded_batch()`：批量失效记忆

### 代码的作用

批量把仍为 `active` 的记忆改成 `superseded`，并返回实际被更新的 ID。

### 实现步骤

1. 把 UUID 或字符串 ID 统一转换为字符串。
2. 去除空 ID 和重复 ID。
3. 查询其中哪些 ID 当前仍然是 `active`。
4. 可选地使用 `user_id` 限制记忆所属用户。
5. 批量更新命中的记忆。
6. 返回真正发生更新的 ID。

它先查询再更新，因此返回值不会包含：

- 数据库中不存在的 ID。
- 已经处于 `superseded` 状态的 ID。
- 传入 `user_id` 后属于其他用户的 ID。

这个方法只更新 `memory_items.status`，不会向 `memory_replacements` 写入替换关系，因为它没有接收新的记忆 ID。

### 数据库操作

| 表 | 操作 | 作用 |
|---|---|---|
| `memory_items` | `SELECT` | 确认哪些目标记忆仍然有效。 |
| `memory_items` | `UPDATE` | 批量把有效记忆改成 `superseded`。 |

---

## 九、向量的二进制编码和解码

### `_encode_embedding()`

```python
def _encode_embedding(vec: list[float]) -> bytes:
    import struct
    return struct.pack(f"{len(vec)}f", *vec)
```

作用：把 Python 的 `list[float]` 打包成连续二进制数据。

例如，1024 个浮点数会按单精度浮点格式写成 BLOB，供 SQLite 和 `sqlite-vec` 使用。单精度浮点通常占 4 字节，因此 1024 维向量的原始数据大约占 4096 字节。

### `_decode_embedding()`

```python
def _decode_embedding(data: bytes | None) -> list[float] | None:
    if data is None:
        return None
    import struct
    return list(struct.unpack(f"{len(data) // 4}f", data))
```

作用：把数据库读出的 BLOB 转回 `list[float]`。

- 如果数据库中的向量是 `NULL`，返回 `None`。
- 每个单精度浮点占 4 字节，所以向量长度为 `len(data) // 4`。

---

## 十、相关数据库表结构

### `memory_items`

保存长期记忆的主要内容。

| 字段 | 类型 | 作用 |
|---|---|---|
| `id` | `TEXT PRIMARY KEY` | 记忆唯一 ID。 |
| `user_id` | `INTEGER NOT NULL` | 记忆所属用户。 |
| `memory_type` | `TEXT NOT NULL` | 记忆类型。 |
| `summary` | `TEXT NOT NULL` | 提炼后的记忆文本。 |
| `embedding` | `BLOB` | 向量的二进制形式。 |
| `status` | `TEXT NOT NULL` | 当前状态，默认 `active`。 |
| `source_ref` | `TEXT` | 原始聊天消息引用。 |
| `created_at` | `TIMESTAMP` | 创建时间。 |
| `updated_at` | `TIMESTAMP` | 更新时间。 |

### `vec_items`

保存用于 `sqlite-vec` 相似度查询的向量。

| 字段 | 类型 | 作用 |
|---|---|---|
| `embedding_id` | `TEXT PRIMARY KEY` | 对应 `memory_items.id`。 |
| `embedding` | `FLOAT[1024]` | 1024 维向量。 |

### `memory_replacements`

保存新旧记忆替换关系。

| 字段 | 类型 | 作用 |
|---|---|---|
| `old_id` | `TEXT NOT NULL` | 被替代的旧记忆 ID。 |
| `new_id` | `TEXT NOT NULL` | 替代它的新记忆 ID。 |
| `replaced_at` | `TIMESTAMP` | 替换发生时间。 |

---

## 十一、一条记忆的完整写入链路

假设上层调用：

```python
memory = await memory_store.upsert_item(
    memory_type="preference",
    summary="用户喜欢喝拿铁",
    user_id=123,
    source_ref="session:123:456#msg:8",
)
```

完整链路为：

```text
MemoryStore.upsert_item()
  ↓
Embedder.embed("用户喜欢喝拿铁")
  ↓
DashScope 返回 1024 维向量
  ↓
生成 UUID
  ↓
INSERT memory_items
  ↓
INSERT vec_items
  ↓
commit
  ↓
返回 MemoryItem
```

写入后的数据逻辑上类似：

```text
memory_items
  id          = 记忆 UUID
  user_id     = 123
  memory_type = preference
  summary     = 用户喜欢喝拿铁
  status      = active
  source_ref  = session:123:456#msg:8

vec_items
  embedding_id = 同一个记忆 UUID
  embedding    = 对应的 1024 维向量
```

---

## 十二、一条记忆的向量检索链路

上层首先把用户问题转换为向量：

```text
用户问题：“我喜欢喝什么？”
  ↓
Embedder.embed()
  ↓
查询向量
```

然后调用 `vector_search()`：

```text
查询向量
  ↓
vec_distance_l2
  ↓
在 vec_items 中计算距离
  ↓
JOIN memory_items
  ↓
过滤 user_id、status 和 memory_type
  ↓
按距离从小到大排序
  ↓
返回最相似的 MemoryItem
```

实际项目中，上层通常不是直接调用这两个步骤，而是通过后面创建的 `MemoryEngine` 统一完成检索和结果融合。

---

## 十三、这两行代码在 `main.py` 中的准确含义

```python
embedder = Embedder()
```

这行完成：

- 读取 Embedding API 配置。
- 创建异步 DashScope 兼容客户端。
- 保存模型名称。

这行没有：

- 请求 Embedding API。
- 创建 embedding 向量。
- 连接数据库。

```python
memory_store = MemoryStore(embedder)
```

这行完成：

- 创建长期记忆存储对象。
- 把前面创建的 `embedder` 注入其中。

这行没有：

- 写入长期记忆。
- 查询数据库。
- 生成 embedding。

只有后续真正调用 `upsert_item()`、`vector_search()`、`keyword_search()` 等方法时，才会发生网络请求或数据库操作。

### 阅读时需要记住的关键点

- `Embedder` 负责文本到向量，不负责数据库。
- `MemoryStore` 负责长期记忆的数据库操作。
- `MemoryStore` 通过依赖注入复用 `Embedder`。
- `upsert_item()` 当前行为是新增记忆，并不是真正的“存在则更新”。
- 向量写入时同时保存到 `memory_items.embedding` 和 `vec_items.embedding`。
- 向量检索需要连接 `vec_items` 和 `memory_items`。
- 默认搜索只返回 `active` 记忆。
- `superseded` 记忆没有被物理删除，仍可用于历史问题。
- `mark_superseded_batch()` 不记录新旧替换关系，`supersede()` 才会写 `memory_replacements`。
