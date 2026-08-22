# telegram-bot 项目学习笔记

## `MarkdownMemoryStore`：每个用户的 Markdown 记忆文件层

文件位置：`memory/markdown_store.py`

`MarkdownMemoryStore` 为每个用户维护一套可读、可编辑的 Markdown 记忆文件。它和 `SessionStore`、`MemoryStore` 都与“记忆”有关，但承担不同职责：

```text
SessionStore
  └── 保存完整原始聊天记录，作为原始证据

MemoryStore
  └── 保存结构化摘要和向量，用于快速语义检索

MarkdownMemoryStore
  └── 保存人类可读的长期记忆、自我模型、历史、待整理内容和近期上下文
```

它同时使用一个很小的 SQLite 数据库记录已经执行过的写入，防止同一个对话窗口被重复追加到 Markdown 文件。

---

## 一、它在项目中是怎样创建的

`main.py` 没有直接实例化 `MarkdownMemoryStore`，而是调用：

```python
memory_runtime = build_memory_runtime(
    embedder=embedder,
    memory_store=memory_store,
    session_store=session_store,
)
```

`memory/bootstrap.py` 内部执行：

```python
markdown_store = MarkdownMemoryStore(
    markdown_root or default_markdown_memory_root()
)
```

默认根目录由下面的代码决定：

```python
def default_markdown_memory_root() -> Path:
    return Path(settings.DATABASE_PATH).parent / "markdown_memory"
```

如果数据库路径是：

```text
./data/memory.db
```

那么 Markdown 记忆根目录就是：

```text
./data/markdown_memory
```

### `MarkdownMemoryRuntime`

文件开头定义了一个只读 dataclass 包装器：

```python
@dataclass(frozen=True)
class MarkdownMemoryRuntime:
    store: "MarkdownMemoryStore"
```

它只保存一个 `store` 字段。`frozen=True` 表示 dataclass 创建后不能重新给字段赋值。

最终访问方式是：

```python
memory_runtime.markdown.store
```

这个包装器本身不读写文件，只是让整个 `MemoryRuntime` 的结构更加清楚。

---

## 二、`MarkdownMemoryStore.__init__()`

原始代码：

```python
class MarkdownMemoryStore:
    def __init__(self, root: Path) -> None:
        self.root = root
```

### 代码的作用

保存 Markdown 记忆的根目录。

执行：

```python
store = MarkdownMemoryStore(root)
```

时不会立即创建目录、Markdown 文件或数据库。真正创建用户目录发生在第一次调用 `ensure_user(user_id)` 或其他公开读写方法时。

这是一种延迟创建方式：只有实际使用某个用户的 Markdown 记忆时，才为该用户创建文件。

---

## 三、每个用户的目录结构

假设：

```text
root = ./data/markdown_memory
user_id = 42
```

用户目录结构为：

```text
data/markdown_memory/
└── users/
    └── 42/
        ├── PROACTIVE_CONTEXT.md
        └── memory/
            ├── MEMORY.md
            ├── SELF.md
            ├── HISTORY.md
            ├── PENDING.md
            ├── RECENT_CONTEXT.md
            ├── consolidation_writes.db
            └── journal/
                ├── 2026-08-19.md
                └── ...
```

各文件职责：

| 文件 | 作用 |
|---|---|
| `MEMORY.md` | 已整理好的长期记忆，供 Prompt 和后续向量同步使用。 |
| `SELF.md` | AI 对当前用户形成的自我/交互模型，例如沟通习惯。 |
| `HISTORY.md` | 按发生顺序追加的历史记忆记录。 |
| `PENDING.md` | 已提取但尚未合并进正式长期记忆的候选项。 |
| `RECENT_CONTEXT.md` | 压缩信息、正在进行的话题以及最近几轮对话预览。 |
| `PROACTIVE_CONTEXT.md` | 主动 Agent 相关上下文。当前类只负责创建，没有提供专门的读写方法。 |
| `journal/YYYY-MM-DD.md` | 按日期保存的记忆日记。 |
| `consolidation_writes.db` | 记录哪些 `source_ref + kind` 已经写入，防止重复追加。 |

---

## 四、`ensure_user()`：初始化用户文件

原始核心代码：

```python
def ensure_user(self, user_id: int) -> Path:
    base = self._user_root(user_id)
    memory_dir = base / "memory"
    journal_dir = memory_dir / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)

    _ensure_file(memory_dir / "MEMORY.md", "# Long-term Memory\n\n")
    _ensure_file(memory_dir / "SELF.md", "# Self Model\n\n")
    _ensure_file(memory_dir / "HISTORY.md", "# History\n\n")
    _ensure_file(memory_dir / "PENDING.md", "# Pending Memory\n\n")
    _ensure_file(memory_dir / "RECENT_CONTEXT.md", _default_recent_context())
    _ensure_file(base / "PROACTIVE_CONTEXT.md", "# Proactive Context\n\n")
    self._ensure_writes_db(user_id)
    return base
```

### 代码的作用

确保某个用户所需的全部目录、初始 Markdown 文件和去重数据库已经存在。

### 实现步骤

#### 1. 计算目录

```python
base = self._user_root(user_id)
memory_dir = base / "memory"
journal_dir = memory_dir / "journal"
```

#### 2. 创建目录

```python
journal_dir.mkdir(parents=True, exist_ok=True)
```

`parents=True` 会把缺失的上级目录一起创建；`exist_ok=True` 允许重复调用。

#### 3. 创建初始文件

通过 `_ensure_file()` 创建文件：

```python
def _ensure_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(content, encoding="utf-8")
```

只有文件不存在时才写入初始内容。因此反复调用 `ensure_user()` 不会覆盖用户已经积累的记忆。

#### 4. 创建去重数据库

```python
self._ensure_writes_db(user_id)
```

每个用户都有自己的 `consolidation_writes.db`。

#### 5. 返回用户根目录

返回值是：

```text
root/users/{user_id}
```

### 文件系统操作

- 创建用户目录。
- 创建 `memory/journal` 目录。
- 必要时创建六个默认 Markdown 文件。
- 创建或打开用户自己的 `consolidation_writes.db`。

---

## 五、基础路径辅助方法

```python
def _user_root(self, user_id: int) -> Path:
    return self.root / "users" / str(user_id)
```

返回用户根目录。

```python
def _memory_dir(self, user_id: int) -> Path:
    return self._user_root(user_id) / "memory"
```

返回用户的 `memory` 目录。

```python
def _memory_file(self, user_id: int, name: str) -> Path:
    return self._memory_dir(user_id) / name
```

返回指定记忆文件路径。

```python
def _writes_db(self, user_id: int) -> Path:
    return self._memory_file(user_id, "consolidation_writes.db")
```

返回该用户的去重数据库路径。

这些方法只计算路径，不创建文件。创建工作由 `ensure_user()` 和 `_ensure_file()` 完成。

---

## 六、长期记忆和 Self Model 的读写

### `read_long_term()`

```python
def read_long_term(self, user_id: int) -> str:
    self.ensure_user(user_id)
    return self._memory_file(user_id, "MEMORY.md").read_text(encoding="utf-8")
```

确保用户文件存在，然后读取整个 `MEMORY.md`。

### `write_long_term()`

```python
def write_long_term(self, user_id: int, content: str) -> None:
    self.ensure_user(user_id)
    self._memory_file(user_id, "MEMORY.md").write_text(
        content.rstrip() + "\n",
        encoding="utf-8",
    )
```

使用新内容覆盖整个 `MEMORY.md`。`rstrip() + "\n"` 去除结尾多余空白，并保证文件以一个换行结束。

### `read_self()` 和 `write_self()`

实现方式与长期记忆相同，但操作的是 `SELF.md`：

```text
read_self()  → 读取整个 SELF.md
write_self() → 覆盖整个 SELF.md
```

### 文件操作特点

| 方法 | 文件 | 操作方式 |
|---|---|---|
| `read_long_term()` | `MEMORY.md` | 整体读取 |
| `write_long_term()` | `MEMORY.md` | 整体覆盖 |
| `read_self()` | `SELF.md` | 整体读取 |
| `write_self()` | `SELF.md` | 整体覆盖 |

这些方法不操作主 SQLite 数据库和向量表。

---

## 七、`RECENT_CONTEXT.md` 和最近对话

### 默认内容

`_default_recent_context()` 返回：

```markdown
# Recent Context

## Compression
until: none
- none

## Ongoing Threads
- none

## Recent Turns
<!-- a-preview = assistant reply preview only -->
- none
```

它把近期上下文分成三部分：

- `Compression`：对旧上下文的压缩信息。
- `Ongoing Threads`：正在进行的话题。
- `Recent Turns`：最近几轮消息预览。

### 基础读写

```text
read_recent_context()  → 读取整个 RECENT_CONTEXT.md
write_recent_context() → 覆盖整个 RECENT_CONTEXT.md
```

### `write_recent_turns()`

核心流程：

```python
existing = self.read_recent_context(user_id)
recent_turns = _format_recent_turns(
    messages[-max(1, keep_count):]
)
self.write_recent_context(
    user_id,
    _replace_recent_turns(existing, recent_turns),
)
```

作用是只更新 `## Recent Turns` 区块，保留原来的 `Compression` 和 `Ongoing Threads`。

默认最多取消息列表最后 10 条：

```python
keep_count = 10
```

即使传入 0 或负数，也会因为 `max(1, keep_count)` 至少保留最后一条。

### `_format_recent_turns()`

只处理：

```text
role = user 或 assistant
content 非空
```

用户消息格式：

```text
[user] 完整内容
```

助手消息格式：

```text
[a-preview] 回复内容前 80 个字符
```

助手回复只保存预览，减少文件膨胀；用户消息当前不截断。

其他 role，例如 `system` 或 `tool`，会被忽略。

### `_replace_recent_turns()`

- 如果已有 `## Recent Turns`，删除旧区块并替换成新区块。
- 如果没有该标题，把新区块追加到文件末尾。
- 它不是不断追加最近对话，因此不会因为每轮更新而无限重复相同区块。

### 在 Pipeline 中的位置

每轮对话保存 Session 后，`PassiveTurnPipeline` 调用：

```python
store.write_recent_turns(
    user_id=user_id,
    messages=session.messages,
)
```

因此 `RECENT_CONTEXT.md` 会跟随最新 Session 更新。

### 一个容易忽略的细节

`BeforeReasoningPhase` 会把 `read_recent_context` 交给 Prompt Builder。不过当前 `RecentContextPromptBlock` 调用 `_strip_recent_turns()`，会在加入 Prompt 前去掉 `## Recent Turns` 及其后面的内容。

原因是最近原始对话已经通过 Session history 单独加入 LLM messages，去掉这个区块可以避免同一轮对话重复注入。当前真正从 `RECENT_CONTEXT.md` 注入 Prompt 的主要是 `Compression` 和 `Ongoing Threads`。

---

## 八、`PENDING.md` 的读取和清理

### `read_pending()`

```python
def read_pending(self, user_id: int) -> str:
    self.ensure_user(user_id)
    return _clean_pending_text(
        self._memory_file(user_id, "PENDING.md").read_text(encoding="utf-8")
    )
```

读取文件后通过 `_clean_pending_text()` 清理控制信息。

### `_clean_pending_text()`

它会忽略：

- 空行。
- `# Pending Memory` 标题。
- `<!-- consolidation:... -->` 去重标记。

最终只向调用者返回真正的待处理记忆项。

原始文件可能是：

```markdown
# Pending Memory

<!-- consolidation:"session:42:7#msg:0-3":pending_items -->
- [preference] 用户喜欢 Python
```

`read_pending()` 返回：

```text
- [preference] 用户喜欢 Python
```

---

## 九、只追加一次的幂等写入

`append_history_once()`、`append_pending_once()` 和 `append_journal()` 都需要避免同一个对话窗口被重复写入。

这里的“幂等”表示：对相同来源重复执行相同类型的追加操作，文件最终只出现一次内容。

### 1. 清理输入

```python
clean_entries = [entry.strip() for entry in entries if entry.strip()]
```

删除空内容并清理每条内容首尾空白。没有有效内容时直接返回 `False`。

### 2. 申请写入资格

```python
if not self._claim_write(user_id, source_ref=source_ref, kind=kind):
    return False
```

`source_ref + kind` 已经存在时，说明该写入以前执行过，不再重复追加。

### 3. 写入可追溯标记

```python
marker = _marker(source_ref, kind)
```

`_marker()` 使用 JSON 编码 `source_ref`：

```python
payload = json.dumps(source_ref, ensure_ascii=False)
return f"<!-- consolidation:{payload}:{kind} -->"
```

生成类似：

```html
<!-- consolidation:"session:42:7#msg:0-3":history_entry -->
```

这是 Markdown 的 HTML 注释，普通渲染时不会显示，但保留了内容来源。

---

## 十、`append_history_once()`

### 代码的作用

把历史记忆追加到 `HISTORY.md`，同一个 `source_ref + kind` 只写一次。

默认：

```python
kind = "history_entry"
```

写入格式：

```markdown
<!-- consolidation:"session:42:7#msg:0-3":history_entry -->
[2026-08-19 10:00] 用户喜欢 Python
```

文件通过追加模式打开：

```python
open("a", encoding="utf-8")
```

因此旧历史不会被覆盖。

返回值：

- `True`：本次确实追加成功。
- `False`：没有有效内容，或者相同来源已经写过。

### 文件和数据库操作

| 位置 | 操作 |
|---|---|
| `consolidation_writes.db` | 尝试插入写入声明 |
| `HISTORY.md` | 取得声明成功后追加历史内容 |

---

## 十一、`append_pending_once()`

### 代码的作用

把待整理记忆追加到 `PENDING.md`，同一个 `source_ref + kind` 只写一次。

默认：

```python
kind = "pending_items"
```

典型内容：

```markdown
<!-- consolidation:"session:42:7#msg:0-3":pending_items -->
- [preference] 用户喜欢 Python
```

返回值和幂等逻辑与 `append_history_once()` 相同。

`PENDING.md` 中的内容之后会由 `MemoryOptimizer` 合并到 `MEMORY.md`，必要时也会更新 `SELF.md`。

### 文件和数据库操作

| 位置 | 操作 |
|---|---|
| `consolidation_writes.db` | 尝试插入写入声明 |
| `PENDING.md` | 取得声明成功后追加候选记忆 |

---

## 十二、`append_journal()`

### 代码的作用

按日期把历史条目追加到：

```text
memory/journal/{date}.md
```

例如：

```text
memory/journal/2026-08-19.md
```

首次写入时创建文件，默认标题为：

```markdown
# Journal 2026-08-19
```

它使用的去重 kind 是：

```python
kind = f"journal:{date}"
```

所以同一个 `source_ref` 可以分别写入：

- `HISTORY.md`，kind 为 `history_entry`。
- `PENDING.md`，kind 为 `pending_items`。
- 当日日记，kind 为 `journal:2026-08-19`。

三种写入不会互相冲突，但同一种写入不会重复。

### 文件和数据库操作

| 位置 | 操作 |
|---|---|
| `consolidation_writes.db` | 按 `source_ref + journal:日期` 申请写入 |
| `journal/日期.md` | 必要时创建文件并追加内容 |

---

## 十三、去重数据库 `consolidation_writes.db`

### 表结构

每个用户的数据库中创建一张 `writes` 表：

```sql
CREATE TABLE IF NOT EXISTS writes (
    source_ref TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_ref, kind)
)
```

| 字段 | 类型 | 约束 | 作用 |
|---|---|---|---|
| `source_ref` | `TEXT` | 非空、联合主键之一 | 内容来自哪个 Session 消息或消息窗口。 |
| `kind` | `TEXT` | 非空、联合主键之一 | 写入类型，例如 `history_entry`、`pending_items`、`journal:日期`。 |
| `created_at` | `TEXT` | 非空 | 申请写入的 UTC 时间，使用 ISO 格式字符串。 |

联合主键：

```sql
PRIMARY KEY (source_ref, kind)
```

它保证同一个来源的同一种写入只能登记一次。

### `_ensure_writes_db()`

```python
conn = sqlite3.connect(path)
conn.execute("CREATE TABLE IF NOT EXISTS writes (...)")
conn.commit()
conn.close()
```

确保数据库文件和表存在，每次使用后关闭连接。

### `_claim_write()`

尝试执行：

```sql
INSERT INTO writes (source_ref, kind, created_at)
VALUES (?, ?, ?)
```

结果分两种：

```text
联合主键不存在
  → INSERT 成功
  → commit
  → 返回 True

联合主键已经存在
  → SQLite 抛出 IntegrityError
  → 返回 False
```

`finally` 保证连接最终关闭。

### 这里操作的是哪个数据库

它不是项目的主数据库 `memory.db`，而是每个用户目录下单独的：

```text
users/{user_id}/memory/consolidation_writes.db
```

它不保存长期记忆正文，只保存 Markdown 追加操作的幂等记录。

### 实现上的一个细节

代码先在 SQLite 中登记写入声明，再向 Markdown 文件追加内容。如果登记成功后文件写入发生异常，下一次重试会因为声明已经存在而返回 `False`。因此它可以可靠防止重复，但当前实现没有把“登记”和“文件写入”组成真正的原子事务。

---

## 十四、PENDING 快照：近似事务式处理

`MemoryOptimizer` 要把 `PENDING.md` 合并进正式的 `MEMORY.md`。在调用 LLM 和写文件期间可能失败，因此需要快照机制。

### `snapshot_pending()`

流程：

```text
确保用户文件存在
  ↓
如果旧快照仍存在，先 rollback
  ↓
读取并清理 PENDING.md
  ↓
没有有效内容：重置 PENDING.md，返回空字符串
  ↓ 有内容
把 PENDING.md 重命名为 PENDING.snapshot.md
  ↓
创建一个新的空 PENDING.md
  ↓
返回清理后的待处理内容
```

核心操作：

```python
pending.rename(snapshot)
pending.write_text("# Pending Memory\n\n", encoding="utf-8")
```

为什么重命名后立即创建新的 `PENDING.md`？

因为优化器处理旧候选期间，新的 consolidation 仍然可能产生候选记忆。新内容可以继续写进新的 `PENDING.md`，不会混进正在处理的快照。

### `commit_pending_snapshot()`

```python
if snapshot.exists():
    snapshot.unlink()
```

当优化成功时删除快照，表示旧候选已经正式处理完成。

### `rollback_pending_snapshot()`

当优化失败时：

1. 读取旧快照。
2. 读取优化期间新写入的 `PENDING.md`。
3. 把两部分合并回 `PENDING.md`。
4. 删除快照文件。

它不会简单覆盖当前 `PENDING.md`，因此不会丢失快照创建后新追加的候选记忆。

### 文件状态变化

```text
开始：
PENDING.md = 旧候选 A

snapshot：
PENDING.snapshot.md = 旧候选 A
PENDING.md          = 空，可接收新候选 B

commit：
删除 snapshot
PENDING.md = 新候选 B

rollback：
PENDING.md = 旧候选 A + 新候选 B
删除 snapshot
```

这不是数据库事务，但达到了类似“成功提交、失败恢复”的文件处理效果。

---

## 十五、`backup_long_term()`：备份长期记忆

```python
def backup_long_term(
    self,
    user_id: int,
    backup_name: str = "MEMORY.bak.md",
) -> None:
```

使用：

```python
shutil.copyfile(source, source.with_name(backup_name))
```

把当前 `MEMORY.md` 复制为备份，默认文件名是：

```text
MEMORY.bak.md
```

`MemoryOptimizer` 在覆盖已有的长期记忆前调用它，为优化前内容保留一个副本。

如果备份文件已经存在，`copyfile()` 会覆盖旧备份。因此这里只保存最近一次备份，不是多版本历史系统。

---

## 十六、它在长期记忆链路中的位置

### 1. Consolidation 产生候选记忆

`ConsolidationWorker` 从 Session 的旧消息窗口中提取摘要，然后同时执行：

```text
append_history_once()
append_pending_once()
append_journal()
```

这被代码称为 Markdown shadow write。

同时，摘要还会通过：

```python
MemoryStore.upsert_item(...)
```

写入向量记忆库。

### 2. MemoryOptimizer 整理 Markdown

```text
snapshot_pending()
  ↓
读取 MEMORY.md 和 SELF.md
  ↓
LLM 合并候选记忆
  ↓
backup_long_term()
  ↓
write_long_term() / write_self()
  ↓
成功：commit_pending_snapshot()
失败：rollback_pending_snapshot()
```

### 3. Markdown 内容进入 Prompt

`main.py` 把以下绑定方法传给 `BeforeReasoningPhase`：

```python
self_model_reader=memory_runtime.markdown.store.read_self
long_term_memory_reader=memory_runtime.markdown.store.read_long_term
recent_context_reader=memory_runtime.markdown.store.read_recent_context
```

构建 Prompt 时：

- `SELF.md` 进入 Self Model 区块。
- `MEMORY.md` 进入 Long-term Memory 区块。
- `RECENT_CONTEXT.md` 的稳定部分进入 Recent Context 区块。

### 4. Markdown 与向量库同步

项目中的 `MarkdownVectorSync` 可以解析 `MEMORY.md`，把缺失的长期记忆同步到 `MemoryStore` 的向量数据库。

因此 Markdown 层既是人类可读的记忆文件，也可以成为向量记忆的数据来源之一。

---

## 十七、Markdown Memory、Vector Memory 和 Session 的区别

| 对比项 | Session | Vector Memory | Markdown Memory |
|---|---|---|---|
| 主要类 | `SessionStore` | `MemoryStore` / `MemoryEngine` | `MarkdownMemoryStore` |
| 保存位置 | `conversation_sessions` | `memory_items`、`vec_items` | 每用户一组 `.md` 文件 |
| 保存内容 | 完整原始消息 | 可检索的结构化记忆摘要和向量 | 人类可读的长期记忆、自我模型、历史和候选项 |
| 数据粒度 | 一个聊天的消息数组 | 每条记忆一个 UUID | 按文件和 Markdown 区块组织 |
| 主要查询方式 | 按会话加载、原文匹配 | 向量与关键词检索 | 整体读取文件并加入 Prompt |
| 是否保留原话 | 是 | 通常是摘要 | 通常是整理后的内容或历史条目 |
| 是否支持语义搜索 | 否 | 是 | 自身不支持；可同步到向量库 |
| 是否方便人工查看和编辑 | 一般 | 不方便 | 非常方便 |
| 可追溯方式 | 自身就是原文 | `source_ref` 指向 Session | HTML marker 和去重数据库记录 `source_ref` |

三者协作：

```text
原始聊天
  ↓
SessionStore 保存完整证据
  ↓
ConsolidationWorker 提取长期信息
  ├── MemoryStore：写入摘要和向量
  └── MarkdownMemoryStore：写入 HISTORY、PENDING 和 journal
                              ↓
                        MemoryOptimizer
                              ↓
                       MEMORY.md / SELF.md
                              ↓
                    Prompt + 向量同步
```

---

## 十八、数据库操作总结

`markdown_store.py` 不操作主数据库中的 `memory_items`、`vec_items` 或 `conversation_sessions`。

它只操作每用户自己的 `consolidation_writes.db`：

| 方法 | SQL 操作 | 作用 |
|---|---|---|
| `_ensure_writes_db()` | `CREATE TABLE IF NOT EXISTS writes` | 初始化去重表。 |
| `_claim_write()` | `INSERT INTO writes` | 申请某个来源和类型的唯一写入资格。 |

Markdown 正文通过文件系统读写，而不是通过 SQL 保存。

---

## 十九、完整示例

假设 consolidation 从 Session 中提取出：

```text
用户是后端工程师
```

来源：

```text
session:42:7#msg:0-1
```

### 写入 HISTORY

```python
store.append_history_once(
    user_id=42,
    entries=["[2026-08-19 10:00] 用户是后端工程师"],
    source_ref="session:42:7#msg:0-1",
)
```

### 写入 PENDING

```python
store.append_pending_once(
    user_id=42,
    items=["- [identity] 用户是后端工程师"],
    source_ref="session:42:7#msg:0-1",
)
```

### 写入日记

```python
store.append_journal(
    user_id=42,
    date="2026-08-19",
    entries=["[2026-08-19 10:00] 用户是后端工程师"],
    source_ref="session:42:7#msg:0-1",
)
```

去重数据库会产生三条记录：

```text
(session:42:7#msg:0-1, history_entry)
(session:42:7#msg:0-1, pending_items)
(session:42:7#msg:0-1, journal:2026-08-19)
```

如果相同代码再次执行，这三个联合主键已经存在，三个追加方法都会返回 `False`，不会重复写文件。

---

## 二十、阅读时需要记住的关键点

- `MarkdownMemoryStore(root)` 只保存根路径，不会立刻创建用户文件。
- `ensure_user()` 是幂等初始化，不会覆盖已经存在的 Markdown 内容。
- 每个用户拥有独立的 Markdown 目录和 `consolidation_writes.db`。
- `MEMORY.md` 是正式长期记忆，`PENDING.md` 是尚未合并的候选记忆。
- `HISTORY.md` 和 `journal` 采用追加方式；`MEMORY.md`、`SELF.md` 和 `RECENT_CONTEXT.md` 采用整体覆盖方式。
- `write_recent_turns()` 只替换 Recent Turns 区块，不破坏其他近期上下文区块。
- 助手最近回复只保留前 80 个字符的预览。
- 当前 Prompt 会去掉 `RECENT_CONTEXT.md` 中的 Recent Turns，避免与 Session history 重复。
- `source_ref + kind` 联合主键防止同一种 Markdown 写入重复执行。
- 去重 SQLite 数据库不保存记忆正文，只保存写入声明。
- PENDING 快照允许优化成功时提交、失败时恢复，并保留优化期间新到达的候选。
- Markdown 文件便于人类查看和编辑，但自身不提供向量检索。
- Session 是原始证据，Vector Memory 用于快速检索，Markdown Memory 用于持久、可读的记忆组织。
