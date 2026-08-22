# telegram-bot 项目学习笔记

## 工具系统：`ToolRegistry`、`ToolExecutor` 和 `ToolRuntime`

`main.py` 中有两行代码：

```python
tool_registry = ToolRegistry()
tool_executor = ToolExecutor()
```

表面上只是创建两个对象，实际上它们是整个工具调用系统的两个基础组件：

```text
ToolRegistry（工具注册表）
    负责：系统有哪些工具、工具叫什么、参数格式是什么、真正执行谁

ToolExecutor（工具执行门卫）
    负责：执行工具之前，运行插件 Hook，允许改参数或拒绝调用
```

不过，只看这两个类还不够。项目中真正把所有步骤串起来的是 `ToolRuntime`：

```text
                      ToolRuntime
                           │
          ┌────────────────┴────────────────┐
          ↓                                 ↓
   ToolRegistry                       ToolExecutor
   查找并调用工具                     执行调用前 Hook
```

相关核心文件：

```text
main.py
agent/tools/base.py
agent/tools/registry.py
agent/tools/runtime.py
agent/tool_hooks/base.py
agent/tool_hooks/types.py
agent/tool_hooks/executor.py
agent/tools/memory.py
agent/pipeline/phases/before_reasoning.py
agent/pipeline/reasoner.py
agent/plugins/decorators.py
agent/plugins/manager.py
```

---

## 一、先理解什么是“工具”

大模型本身只能根据输入生成文字。它不能直接查询数据库、读取长期记忆或调用业务函数。

工具系统给大模型提供了一组可以调用的函数，例如：

```text
recall_memory   检索长期记忆摘要
search_messages 搜索历史消息
fetch_messages  读取历史消息原文
memorize         写入一条长期记忆
```

假设用户问：

```text
用户：我以前说过自己喜欢喝什么吗？
```

模型不会直接编造答案，而是可以产生一次工具调用：

```json
{
  "name": "recall_memory",
  "arguments": {
    "query": "用户的饮品偏好",
    "memory_type": "preference"
  }
}
```

程序执行工具，把结果再交给模型：

```text
用户问题
   ↓
LLM 判断需要查记忆
   ↓
生成 recall_memory 工具调用
   ↓
程序执行工具
   ↓
将工具结果作为 role=tool 消息交回 LLM
   ↓
LLM 根据证据生成最终回答
```

因此，工具不是模型内部能力，而是 Python 程序开放给模型的外部能力。

---

## 二、三个核心组件分别负责什么

| 组件 | 类比 | 主要职责 |
|---|---|---|
| `ToolRegistry` | 工具仓库/通讯录 | 保存工具对象和元数据，提供工具 Schema，按名字调用工具 |
| `ToolExecutor` | 门卫/安检 | 在调用前执行 Hook，修改参数或阻止调用，捕获执行异常 |
| `ToolRuntime` | 调度中心 | 解析参数、查找工具、校验、超时、重试、结果标准化 |
| `Reasoner` | 使用工具的人 | 把工具说明交给 LLM，接收 tool call，调用 Runtime，再把结果交回 LLM |

完整关系如下：

```text
BeforeReasoningPhase
    │ get_schemas()
    ▼
ToolRegistry ───────────────→ LLM 看见工具说明
                                  │
                                  │ 返回 tool_call
                                  ▼
                              Reasoner
                                  │
                                  ▼
                             ToolRuntime
                         ┌────────┴────────┐
                         ▼                 ▼
                  ToolExecutor       ToolRegistry
                  执行 Hook           执行 Tool
                         └────────┬────────┘
                                  ▼
                           标准 JSON 结果
                                  │
                                  ▼
                                LLM
```

---

## 三、`Tool`：一个工具的数据结构

源码位置：

```text
agent/tools/base.py
```

定义如下：

```python
@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    timeout_s: float = 30.0
    idempotent: bool = True
    retry_count: int = 0
    output_schema: dict[str, Any] | None = None
```

字段作用：

| 字段 | 作用 |
|---|---|
| `name` | 工具唯一名称，也是 LLM 调用时使用的名称 |
| `description` | 告诉 LLM 这个工具适合解决什么问题 |
| `parameters` | 输入参数的 JSON Schema |
| `handler` | 真正执行工作的 Python 函数 |
| `timeout_s` | 单次调用超时时间，默认 30 秒 |
| `idempotent` | 是否可以安全重复执行 |
| `retry_count` | 显式配置的重试次数 |
| `output_schema` | 可选的输出结果 Schema，用于结果校验 |

### 一个最小 Tool 示例

```python
async def weather_handler(arguments, ctx):
    city = arguments["city"]
    return f"{city}：晴"

weather_tool = Tool(
    name="get_weather",
    description="查询城市天气",
    parameters={
        "type": "object",
        "properties": {
            "city": {"type": "string"}
        },
        "required": ["city"],
    },
    handler=weather_handler,
)
```

工具由两部分组成：

```text
给 LLM 看的部分
├── name
├── description
└── parameters

程序内部使用的部分
├── handler
├── timeout_s
├── idempotent
├── retry_count
└── output_schema
```

### `Tool.execute()`

作用：调用工具的 `handler`，同时兼容同步函数和异步函数。

```python
async def execute(self, arguments, ctx=None):
    result = self.handler(arguments, ctx)
    if inspect.isawaitable(result):
        result = await result
    ...
```

输入示例：

```python
arguments = {"city": "北京"}
ctx = current_reasoning_context
```

输出可能是：

```python
"北京：晴"
```

如果 handler 返回普通对象，`Tool.execute()` 会执行 `str(result)`；如果返回 `ToolResult`，则保留结构化结果。

### `Tool.to_schema()`

作用：把 Tool 转换成 OpenAI function calling 格式，供 LLM 阅读。

输入：当前 `Tool` 对象。

输出示例：

```json
{
  "type": "function",
  "function": {
    "name": "get_weather",
    "description": "查询城市天气",
    "parameters": {
      "type": "object",
      "properties": {
        "city": {"type": "string"}
      },
      "required": ["city"]
    }
  }
}
```

注意：`handler`、超时和风险等级不会发给 LLM。

---

## 四、`ToolResult`：工具的结构化返回值

```python
@dataclass
class ToolResult:
    text: str = ""
    data: Any = None
    content_blocks: list[dict[str, Any]] = field(default_factory=list)
```

它允许工具返回：

- `text`：适合给模型阅读的文字。
- `data`：结构化数据。
- `content_blocks`：图片等多模态内容块。

### `preview()`

它按以下顺序生成预览：

```text
有 text           → 返回 text
没有 text 有 data → 返回 str(data)
有 content_blocks →返回“[多模态结果 N blocks]”
全部没有          → 返回空字符串
```

输入示例：

```python
ToolResult(data={"temperature": 25})
```

输出：

```text
{'temperature': 25}
```

---

## 五、`ToolRegistry()`：创建工具注册表

源码位置：

```text
agent/tools/registry.py
```

`main.py`：

```python
tool_registry = ToolRegistry()
```

构造函数创建两个字典：

```python
self._tools: dict[str, Tool] = {}
self._metadata: dict[str, ToolMeta] = {}
```

刚创建时它是空的：

```text
ToolRegistry
├── _tools = {}
└── _metadata = {}
```

所以，这一行本身并没有注册任何工具。

---

## 六、`ToolMeta`：工具的运行元数据

```python
@dataclass
class ToolMeta:
    risk: str = "read-only"
    always_on: bool = False
    search_hint: str | None = None
    source_type: str = "builtin"
    source_name: str = ""
```

| 字段 | 作用 |
|---|---|
| `risk` | 风险级别，例如 `read-only`、`read-write` |
| `always_on` | 是否期望始终向模型提供 |
| `search_hint` | 工具发现时可使用的搜索提示 |
| `source_type` | 来源类型，例如 `builtin` 或 `plugin` |
| `source_name` | 来源名称，例如 `memory` 或插件名称 |

当前真实实现需要注意：

- `risk` 已用于决定是否允许自动重试。
- `source_type/source_name` 已被保存，可用于识别来源。
- `always_on/search_hint` 目前只被保存，尚未参与工具筛选。
- 当前 `BeforeReasoningPhase` 会把注册表中的全部工具 Schema 交给 LLM。

---

## 七、`ToolRegistry.register()`：注册工具

```python
registry.register(
    tool,
    risk="read-only",
    source_type="builtin",
    source_name="memory",
)
```

实现本质是：

```python
self._tools[tool.name] = tool
self._metadata[tool.name] = ToolMeta(...)
```

输入：

- 一个 `Tool` 对象。
- 风险、来源等元数据。

输出：`None`。

注册后：

```text
_tools
└── "recall_memory" → Tool(...)

_metadata
└── "recall_memory" → ToolMeta(risk="read-only", ...)
```

### 同名工具会怎样

字典以工具名为 key，因此后注册的同名工具会覆盖前一个：

```python
registry.register(builtin_echo)
registry.register(plugin_echo)
```

最终 `echo` 指向 `plugin_echo`。

这也解释了 `main.py` 为什么先注册内置工具，再加载插件：

```text
先注册内置工具
      ↓
再加载插件工具
      ↓
插件可以按名称覆盖内置工具
```

---

## 八、`ToolRegistry` 的查询和删除函数

### `unregister(name)`

作用：同时移除工具对象和元数据；不存在时不会报错。

```python
registry.unregister("echo")
```

输入：工具名。

输出：`None`。

PluginManager 卸载插件或插件初始化失败回滚时会使用它。

### `has_tool(name)`

```python
registry.has_tool("recall_memory")
```

输出示例：

```python
True
```

### `get_tool(name)`

```python
tool = registry.get_tool("recall_memory")
```

找到时返回 `Tool`，找不到返回 `None`。

### `get_metadata(name)`

返回对应 `ToolMeta`，找不到返回 `None`。

### `get_registered_names()`

返回所有已注册工具名组成的集合：

```python
{
    "recall_memory",
    "search_messages",
    "fetch_messages",
    "memorize",
}
```

### `get_schemas(names=None)`

作用：把工具转换成给 LLM 看的 Schema。

不传 `names`：

```python
registry.get_schemas()
```

返回全部工具 Schema。

传入集合：

```python
registry.get_schemas({"recall_memory", "fetch_messages"})
```

只返回指定工具的 Schema。

当前 `BeforeReasoningPhase.build_ctx()` 使用的是：

```python
tools = self.tool_registry.get_schemas()
```

因此当前每轮都会把注册表中全部工具交给模型。

### `ToolRegistry.execute(name, arguments, ctx)`

作用：按照工具名找到 Tool，然后调用 `Tool.execute()`。

输入示例：

```python
await registry.execute(
    "get_weather",
    {"city": "北京"},
    ctx,
)
```

输出示例：

```text
北京：晴
```

工具不存在时返回文字：

```text
工具 'get_weather' 不存在
```

重要区别：直接调用 `registry.execute()` 不会自动经过完整的参数校验、Hook、超时和重试。正常的 LLM 调用链通过 `ToolRuntime.execute_call()` 进入。

---

## 九、项目注册了哪些内置工具

`main.py` 中：

```python
register_memory_tools(tool_registry, memory_runtime.engine)
```

它把四个记忆工具注册进共享 Registry：

| 工具 | 风险 | 作用 |
|---|---|---|
| `recall_memory` | `read-only` | 从长期记忆中召回摘要和线索 |
| `search_messages` | `read-only` | 在原始历史消息里按关键词搜索 |
| `fetch_messages` | `read-only` | 根据 `source_ref` 取得原始消息和上下文 |
| `memorize` | `read-write` | 写入新的长期记忆 |

注册后大致是：

```text
ToolRegistry
├── recall_memory
├── search_messages
├── fetch_messages
└── memorize
```

这些工具的 Schema 定义在 `before_reasoning.py` 的 `_TOOLS`，真正 handler 在 `agent/tools/memory.py`。

### `register_memory_tools()` 输入输出

输入：

```python
registry       # 共享工具注册表
memory_engine  # DefaultMemoryEngine
```

处理：将 Schema 和 handler 组合成 `Tool`，逐个调用 `registry.register()`。

输出：`None`。

### 数据库关系

工具框架自身不直接操作数据库，但部分具体工具会通过 `memory_engine` 间接访问存储：

```text
memorize
   ↓
memory_engine.remember()
   ↓
写入长期记忆存储

recall_memory
   ↓
memory_engine.retrieve_explicit()
   ↓
查询长期记忆及向量索引

search_messages / fetch_messages
   ↓
memory_engine
   ↓
查询 Session 原始消息
```

本章节中的 `ToolRegistry`、`ToolExecutor` 和 `ToolRuntime` 不创建数据库表，也不直接执行 SQL。

---

## 十、LLM 是怎样看见工具的

`BeforeReasoningPhase.build_ctx()` 执行：

```python
tools = self.tool_registry.get_schemas()
```

然后放入：

```python
BeforeReasoningCtx(
    ...,
    tools=tools,
)
```

Reasoner 调用模型时：

```python
response = await self.client.chat.completions.create(
    model=self.model,
    messages=messages,
    tools=ctx.tools,
)
```

注意，交给模型的是工具说明，不是 Python handler：

```text
ToolRegistry
   │ to_schema()
   ▼
工具名称 + 描述 + 参数格式
   │
   ▼
LLM
   │ 只能选择工具名并生成参数
   ▼
Python 程序再执行真正 handler
```

模型并不能随意执行 Python，也拿不到 `handler` 对象。

---

## 十一、`ToolExecutor()`：创建工具执行门卫

源码位置：

```text
agent/tool_hooks/executor.py
```

`main.py`：

```python
tool_executor = ToolExecutor()
```

构造函数：

```python
def __init__(self, hooks=None):
    self._hooks = list(hooks or [])
```

刚创建时：

```text
ToolExecutor
└── _hooks = []
```

因此这行代码本身不会执行工具，也没有 Hook。

后面插件加载完毕后：

```python
tool_executor.add_hooks(plugin_manager.tool_hooks)
```

才把插件提供的调用前 Hook 加入 Executor。

---

## 十二、什么是 Tool Hook

Hook 是插入工具调用过程的拦截器。

当前主要用途：

- 检查工具是否允许调用。
- 修改模型传来的参数。
- 拒绝危险调用。
- 给结果增加额外提示和追踪记录。

```text
模型生成参数
   ↓
Hook 1：是否匹配？
   ↓
Hook 2：修改参数
   ↓
Hook 3：允许还是拒绝？
   ↓
真正调用工具
```

每个 Hook 继承 `ToolHook`，实现：

```python
def matches(self, ctx: HookContext) -> bool
async def run(self, ctx: HookContext) -> HookOutcome
```

### `matches()` 输入输出

输入是 `HookContext`，包含工具名、原参数、来源、Session 等信息。

输出：

```python
True   # 当前 Hook 要处理这个调用
False  # 跳过当前 Hook
```

### `run()` 输入输出

输出 `HookOutcome`：

```python
HookOutcome(
    decision="pass",                 # pass 或 deny
    updated_input={"x": 2},          # 可替换参数
    extra_message="参数已规范化",
    reason="",
)
```

拒绝示例：

```python
HookOutcome(
    decision="deny",
    reason="当前用户没有执行该工具的权限",
)
```

---

## 十三、`ToolExecutor.add_hooks()`

```python
tool_executor.add_hooks(plugin_manager.tool_hooks)
```

作用：把一组 Hook 追加到现有列表。

输入：`Sequence[ToolHook]`。

输出：`None`。

它是追加，不是替换；调用两次会继续累加，也可能造成同一个 Hook 重复执行。

---

## 十四、`ToolExecutor.execute()` 的完整流程

函数接收两个参数：

```python
await executor.execute(request, invoker)
```

其中：

- `request` 描述“谁要调用什么工具”。
- `invoker` 是最终真正执行工具的异步函数。

流程：

```text
复制原始参数
   ↓
依次运行 pre_tool_use Hooks
   ├── Hook 不匹配 → 继续
   ├── Hook 修改参数 → 后面的 Hook 使用新参数
   ├── Hook deny → 返回 denied，不调用工具
   └── Hook 抛异常 → 返回 error
   ↓
调用 invoker(tool_name, final_arguments)
   ├── 成功 → success
   └── 异常 → error
```

### 成功输入输出示例

输入：

```python
request = ToolExecutionRequest(
    call_id="call_1",
    tool_name="echo",
    arguments={"text": "hello"},
    source="passive",
)
```

输出：

```python
ToolExecutionResult(
    status="success",
    output="hello",
    final_arguments={"text": "hello"},
)
```

### Hook 修改参数示例

```text
原参数      {"x": 1}
   ↓ rewrite Hook
最终参数    {"x": 2}
   ↓
handler 实际收到 {"x": 2}
```

### Hook 拒绝示例

```python
ToolExecutionResult(
    status="denied",
    output="blocked",
    final_arguments={"x": 1},
)
```

工具 handler 完全不会执行。

### 当前实现的限制

`HookEvent` 类型预留了：

```text
pre_tool_use
post_tool_use
post_tool_error
```

但当前 `ToolExecutor.execute()` 实际只调用 `_run_pre_hooks()`，所以：

- `pre_tool_use` 已接通。
- `post_tool_use` 尚未执行。
- `post_tool_error` 尚未执行。
- `post_hook_trace` 当前通常为空。

阅读时要区分“类型已经预留”和“执行链已经实现”。

---

## 十五、`ToolExecutionRequest` 包含什么

| 字段 | 作用 |
|---|---|
| `call_id` | LLM 为本次工具调用生成的 ID |
| `tool_name` | 工具名 |
| `arguments` | 已解析的参数 |
| `source` | 来源：`passive`、`proactive` 或 `subagent` |
| `session_key` | 当前 Session 标识 |
| `channel` | 渠道，例如 `telegram` |
| `chat_id` | 聊天 ID |
| `request_text` | 用户原始问题 |
| `tool_batch` | 本轮模型同时产生的全部工具调用快照 |
| `tool_batch_index` | 当前工具在批次里的序号 |

这些信息让 Hook 不仅能看参数，还能根据用户问题、渠道和调用来源做决策。

---

## 十六、`ToolRuntime`：真正的调用调度中心

源码位置：

```text
agent/tools/runtime.py
```

Reasoner 初始化时：

```python
self._tool_runtime = ToolRuntime(
    registry=self._tool_registry,
    executor=self._tool_executor,
)
```

所以对象关系是：

```text
Reasoner
└── ToolRuntime
    ├── 同一个 ToolRegistry
    └── 同一个 ToolExecutor
```

`main.py` 先创建共享对象，再传给 Reasoner，保证后面注册工具、增加 Hook 时，Reasoner 能立即使用它们。

---

## 十七、`ToolRuntime.execute_call()` 完整链路

这是工具系统最重要的函数。

```text
1. 解析 arguments JSON
2. 按名字查找 Tool
3. 根据 parameters 校验输入
4. 读取 ToolMeta，计算重试策略
5. 创建 ToolExecutionRequest
6. 交给 ToolExecutor 运行 Hook
7. 通过 Registry 执行 Tool，并施加超时
8. 标准化工具输出
9. 根据 output_schema 校验输出
10. 返回 ToolRuntimeResult
```

输入示例：

```python
await runtime.execute_call(
    call_id="call_abc",
    tool_name="recall_memory",
    raw_arguments='{"query":"用户饮品偏好","memory_type":"preference"}',
    ctx=reasoning_ctx,
    source="passive",
    session_key="123:456",
    channel="telegram",
    chat_id="456",
    request_text="我喜欢喝什么？",
)
```

成功输出是 `ToolRuntimeResult`：

```python
ToolRuntimeResult(
    ok=True,
    status="success",
    tool_name="recall_memory",
    call_id="call_abc",
    data={...},
    final_arguments={...},
    duration_ms=12,
)
```

---

## 十八、参数解析和输入校验

### `_parse_arguments()`

输入可以是字典：

```python
{"query": "饮品偏好"}
```

也可以是 JSON 字符串：

```python
'{"query": "饮品偏好"}'
```

输出是字典。

如果 JSON 无效：

```json
{
  "ok": false,
  "error": {
    "code": "argument_parse",
    "message": "Tool arguments must be valid JSON object: ...",
    "retryable": true
  }
}
```

### `validate_json_schema()`

项目自己实现了一个轻量 Schema 校验器，检查：

- 基本类型。
- 必填字段 `required`。
- `enum`。
- 数字 `minimum/maximum`。
- 字符串 `minLength/maxLength`。
- 数组元素。
- 对象嵌套属性。

示例：Schema 要求 `x` 是 1 到 5 的整数：

```python
{"x": 9}
```

输出错误：

```text
x 须 <= 5
```

校验失败时 handler 不会运行。

这不是完整 JSON Schema 实现，只覆盖当前代码明确实现的规则。

---

## 十九、超时和重试

### 超时

`_invoke_with_timeout()` 使用：

```python
await asyncio.wait_for(..., timeout=tool.timeout_s)
```

默认超时 30 秒，最小会被保护为 0.001 秒。

超时结果的错误码：

```text
timeout
```

### 重试规则

默认配置：

```python
read_only_max_retries = 1
retry_backoff_s = 0.05
```

即只读且幂等的工具，在满足可重试错误条件时最多额外尝试一次：

```text
第一次调用失败
   ↓
等待 0.05 秒
   ↓
第二次调用
```

以下工具不会自动重试：

- `risk != "read-only"`。
- `idempotent == False`。

原因是写操作重复执行可能造成两次写入。

```text
recall_memory 读取失败 → 可以重试
memorize 写入失败      → 不自动重试
```

Hook 明确拒绝的调用也不会重试。

---

## 二十、工具输出怎样标准化

工具可能返回：

- `ToolResult`。
- JSON 字符串。
- 普通字符串。
- 字典或其他对象。

`normalize_tool_output()` 将它们整理成：

```python
(data, data_text)
```

例如 JSON 字符串：

```python
'{"temperature": 25}'
```

变成：

```python
({"temperature": 25}, "")
```

普通文字：

```python
"北京：晴"
```

变成：

```python
("北京：晴", "北京：晴")
```

最后 `ToolRuntimeResult.to_json()` 产生统一信封。

成功示例：

```json
{
  "ok": true,
  "status": "success",
  "data": {"temperature": 25},
  "error": null,
  "meta": {
    "tool_name": "get_weather",
    "call_id": "call_1",
    "duration_ms": 8,
    "retry_count": 0,
    "final_arguments": {"city": "北京"}
  }
}
```

失败示例：

```json
{
  "ok": false,
  "status": "error",
  "data": null,
  "error": {
    "code": "tool_lookup",
    "message": "Unknown tool: abc",
    "retryable": false
  },
  "meta": {
    "tool_name": "abc",
    "call_id": "call_2",
    "duration_ms": 0,
    "retry_count": 0,
    "final_arguments": {}
  }
}
```

主要错误码：

| 错误码 | 含义 |
|---|---|
| `argument_parse` | 参数不是合法 JSON 对象 |
| `tool_lookup` | 工具不存在 |
| `input_validation` | 输入不符合参数 Schema |
| `policy_check` | Hook 拒绝调用 |
| `tool_invoke` | handler 执行异常 |
| `timeout` | 工具超时 |
| `hook_error` | Hook 自己发生异常 |
| `output_validation` | 返回值不符合输出 Schema |

---

## 二十一、Reasoner 怎样处理一次 Tool Call

`Reasoner.run_turn()` 最多循环 4 次。

```text
第 1 次请求 LLM
   │
   ├── 没有 tool_calls → 得到最终回答
   │
   └── 有 tool_calls
          ↓
       逐个执行工具
          ↓
       把工具结果追加到 messages
          ↓
第 2 次请求 LLM
          ↓
       模型基于结果继续回答或再次调用工具
```

模型返回工具调用后，Reasoner 会先追加 assistant 消息：

```python
{
    "role": "assistant",
    "tool_calls": [...],
}
```

执行工具后再追加：

```python
{
    "role": "tool",
    "tool_call_id": tc.id,
    "content": result,
}
```

这里的 `tool_call_id` 用来告诉模型：这份结果对应哪一次工具调用。

如果模型一次返回多个工具调用，当前代码按顺序逐个执行，不是并发执行。

---

## 二十二、工具调用和 EventBus 的关系

Reasoner 在真正调用工具前发送观察事件：

```python
await event_bus.observe(BeforeToolCallCtx(...))
```

调用完成后发送：

```python
await event_bus.observe(AfterToolResultCtx(...))
```

所以一次调用同时存在两类扩展点：

```text
BeforeToolCallCtx TAP
    │ 用于观察、日志、遥测
    ▼
ToolRuntime
    │
    ├── ToolExecutor pre_tool_use Hook
    │      可修改参数或拒绝
    │
    └── ToolRegistry 执行 handler
    ▼
AfterToolResultCtx TAP
    用于观察最终状态和结果
```

EventBus TAP 与 Tool Hook 的区别：

| 机制 | 主要用途 | 能否阻止工具 |
|---|---|---|
| `BeforeToolCallCtx`/`AfterToolResultCtx` | 观察、日志、遥测 | TAP 返回值被忽略，不能阻止 |
| `pre_tool_use` Hook | 权限、策略、参数修改 | 可以 `deny` |

---

## 二十三、插件怎样增加工具

插件可以使用装饰器：

```python
@tool(name="echo_plugin", risk="read-only")
async def echo(self, event, text: str):
    """返回输入文字。"""
    return f"echo:{text}"
```

PluginManager 加载插件时会：

```text
读取 @tool 元数据
   ↓
根据函数签名生成 JSON Schema
   ↓
把插件方法包装成 Tool.handler
   ↓
registry.register(..., source_type="plugin")
```

函数参数类型会转换成 JSON Schema；没有默认值的参数进入 `required`。

PluginManager 还会过滤模型传来的参数，只把插件函数真正接受的参数传入。

### 插件覆盖内置工具

因为插件在内置工具之后注册，而且 Registry 以名称为 key，所以同名插件工具会覆盖内置工具。

这是有意支持的扩展方式，也意味着插件工具名称需要谨慎选择。

---

## 二十四、插件怎样增加 Tool Hook

插件可以使用：

```python
@on_tool_pre(tool_name="echo_plugin")
async def check_echo(self, event):
    ...
```

PluginManager 把它包装为 `_PluginToolHook`，放入：

```python
plugin_manager.tool_hooks
```

然后 `main.py` 执行：

```python
tool_executor.add_hooks(plugin_manager.tool_hooks)
```

完整链路：

```text
插件 @on_tool_pre
   ↓
PluginManager 收集 Hook
   ↓
ToolExecutor.add_hooks()
   ↓
每次工具调用前依次运行
```

---

## 二十五、`main.py` 两行代码的后续链路

```python
tool_registry = ToolRegistry()
tool_executor = ToolExecutor()
```

不能孤立理解，它们后面依次经历：

```text
1. 创建空 ToolRegistry
2. 创建没有 Hook 的 ToolExecutor
3. 两者传入 Reasoner
4. Reasoner 用它们创建 ToolRuntime
5. register_memory_tools() 注册四个内置工具
6. PluginManager 注册插件工具
7. 插件同名工具可以覆盖内置工具
8. 把插件 Hook 加到 ToolExecutor
9. BeforeReasoningPhase 从 Registry 获取全部 Schema
10. Reasoner 把 Schema 交给 LLM
11. LLM 返回 tool_call
12. ToolRuntime 执行完整安全链
13. 工具结果返回 LLM
14. LLM 继续推理并生成回答
```

对象共享关系：

```text
main.py
├── tool_registry ───────────────┐
│                                │
│   ├── register_memory_tools    │ 注册内置工具
│   ├── PluginManager            │ 注册插件工具
│   ├── BeforeReasoningPhase     │ 读取 Schema
│   └── Reasoner/ToolRuntime ◄───┘ 查找并执行
│
└── tool_executor ───────────────┐
                                 │
    ├── PluginManager hooks      │ 增加 Hook
    └── Reasoner/ToolRuntime ◄───┘ 调用前执行 Hook
```

共享同一个实例非常重要。如果 Reasoner 使用另一个 Registry，后面注册的工具就不会被它看见。

---

## 二十六、一次 `recall_memory` 的真实链路

```text
用户：“我喜欢喝什么？”
   ↓
BeforeReasoningPhase
   ├── registry.get_schemas()
   └── 把 recall_memory Schema 放进 ctx.tools
   ↓
Reasoner 调用 LLM
   ↓
LLM 返回：
recall_memory({query: "用户饮品偏好", memory_type: "preference"})
   ↓
Reasoner._execute_tool()
   ├── observe(BeforeToolCallCtx)
   └── ToolRuntime.execute_call()
          ├── 解析 JSON
          ├── Registry 查找 recall_memory
          ├── 校验 query 和 memory_type
          ├── Executor 运行 pre Hook
          ├── 设置 30 秒超时
          └── Registry.execute()
                 ↓
              Tool.execute()
                 ↓
              _recall_memory()
                 ↓
              memory_engine.retrieve_explicit()
   ↓
ToolRuntime 标准化为统一 JSON 信封
   ↓
Reasoner observe(AfterToolResultCtx)
   ↓
把 role=tool 的结果交回 LLM
   ↓
LLM 继续调用 fetch_messages 取证，或生成最终回答
```

---

## 二十七、Registry 和 Executor 最容易混淆的地方

错误理解：

```text
ToolExecutor 负责保存并找到工具
```

正确理解：

```text
ToolRegistry 保存并找到工具
ToolExecutor 包裹一次执行，运行调用前 Hook
ToolRuntime 负责整体编排
```

可以用餐厅类比：

```text
菜单                 = Tool Schema
菜谱和厨师           = Tool.handler
菜单与厨师登记册     = ToolRegistry
门口权限检查与改订单 = ToolExecutor + Hook
整个下单流程         = ToolRuntime
顾客决定点什么       = LLM
服务员来回传单       = Reasoner
```

---

## 二十八、当前实现值得注意的细节

### 1. Registry 不是单例

`ToolRegistry()` 每次都会创建独立注册表。项目通过 `main.py` 手动把同一个对象传给各模块。

### 2. Executor 不是单例

`ToolExecutor()` 也是普通对象。Hook 只对持有这个对象的调用链生效。

### 3. 同名注册是覆盖，不是报错

插件可以覆盖内置工具，但意外重名也会静默覆盖。

### 4. 直接调用 Registry 会绕过 Runtime

直接 `registry.execute()` 不具备 Runtime 的完整校验、Hook、超时和重试保护。

### 5. 当前所有工具 Schema 都发给 LLM

虽然元数据有 `always_on` 和 `search_hint`，当前还没有按请求动态挑选工具。

### 6. 当前只有调用前 Hook

`post_tool_use` 和 `post_tool_error` 只在类型中预留，执行器尚未运行它们。

### 7. 工具按顺序执行

模型一次请求多个工具时，Reasoner 使用 `for` 循环逐个 `await`，没有并发。

### 8. Tool handler 支持同步和异步

`Tool.execute()` 用 `inspect.isawaitable()` 判断是否需要 `await`。

### 9. 只读幂等工具才适合自动重试

写工具默认不重试，以免重复产生副作用。

---

## 二十九、数据库操作总结

这两行：

```python
tool_registry = ToolRegistry()
tool_executor = ToolExecutor()
```

不操作数据库，不创建表，也不执行查询。

数据库只可能出现在具体工具 handler 的下游：

```text
工具框架
   ↓ 调用
具体 Tool handler
   ↓ 调用
MemoryEngine / Store
   ↓
SQLite 表或向量索引
```

所以应当分层理解：

```text
工具框架负责“如何调用”
具体工具负责“调用后做什么”
Store/Engine 负责“如何读写数据”
```

---

## 三十、最小完整示例

```python
from agent.tools import Tool, ToolRegistry
from agent.tool_hooks import ToolExecutor
from agent.tools.runtime import ToolRuntime


def add_handler(arguments, ctx):
    return str(arguments["a"] + arguments["b"])


registry = ToolRegistry()
executor = ToolExecutor()

registry.register(
    Tool(
        name="add",
        description="计算两个整数之和",
        parameters={
            "type": "object",
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "integer"},
            },
            "required": ["a", "b"],
        },
        handler=add_handler,
    ),
    risk="read-only",
)

runtime = ToolRuntime(
    registry=registry,
    executor=executor,
)

result = await runtime.execute_call(
    call_id="call_add_1",
    tool_name="add",
    raw_arguments='{"a": 2, "b": 3}',
)

print(result.to_json())
```

结果大致为：

```json
{
  "ok": true,
  "status": "success",
  "data": 5,
  "error": null,
  "meta": {
    "tool_name": "add",
    "call_id": "call_add_1",
    "retry_count": 0,
    "final_arguments": {"a": 2, "b": 3}
  }
}
```

---

## 三十一、阅读时需要记住的关键点

- `Tool` 把名称、描述、参数 Schema 和 Python handler 包装在一起。
- `ToolRegistry` 管理“系统有哪些工具”。
- `ToolExecutor` 管理“执行前有哪些 Hook”。
- `ToolRuntime` 才是解析、校验、Hook、超时、重试和结果封装的总调度器。
- `BeforeReasoningPhase` 从 Registry 取得 Schema，让 LLM 知道可以调用哪些工具。
- LLM 只生成工具名和参数，不会直接执行 Python。
- Reasoner 收到 tool call 后调用 Runtime，再把结果作为 `role=tool` 消息交回模型。
- 内置记忆工具先注册，插件工具后注册；同名插件工具可以覆盖内置工具。
- `risk` 会影响重试策略：只读、幂等工具可以重试，写工具不自动重试。
- Tool Hook 可以修改参数或拒绝调用。
- EventBus 的工具事件用于观察；Tool Hook 才能真正拦截。
- 当前只有 `pre_tool_use` Hook 被执行，post Hook 尚未接通。
- Registry、Executor 和 Runtime 自身不操作数据库；具体工具可能通过 Engine/Store 访问数据库。
