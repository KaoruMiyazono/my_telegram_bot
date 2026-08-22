# telegram-bot 项目学习笔记

## `PluginManager`：本地插件发现、加载和接线系统

`main.py` 中的代码：

```python
plugin_manager = PluginManager(
    [Path.cwd() / "plugins"],
    event_bus=event_bus,
    tool_registry=tool_registry,
    workspace=Path.cwd(),
    memory_engine=memory_runtime.engine,
)
await plugin_manager.load_all()
tool_executor.add_hooks(plugin_manager.tool_hooks)
reasoner.set_step_modules(
    before_step=plugin_manager.before_step_modules,
    after_step=plugin_manager.after_step_modules,
)
```

对应核心目录：

```text
agent/plugins/
├── base.py
├── config.py
├── context.py
├── decorators.py
├── manager.py
└── registry.py
```

---

## 一、先说明：PluginManager 整套系统是干什么的

不修改 Agent 主流程，也能给 Agent 增加一整套新能力或改变它的行为。

`PluginManager` 让项目可以在不直接修改 Pipeline 核心代码的情况下增加功能。

插件可以提供：

- 新工具，例如天气查询、Echo、第三方 API。
- 生命周期处理器，例如在推理前增加提示。
- 工具调用前 Hook，例如检查权限或修改参数。
- PhaseModule，例如在指定 Pipeline Slot 之后插入处理步骤。
- 插件自己的配置和简单 KV 数据。
- 初始化和终止逻辑。

整体结构：

```text
plugins/*/plugin.py
        ↓
PluginManager 发现并动态 import
        ↓
创建 Plugin 实例并注入 PluginContext
        ↓
┌───────────────┬────────────────┬───────────────────┐
↓               ↓                ↓                   ↓
EventBus     ToolRegistry     ToolExecutor       PhaseModules
生命周期事件   新工具            pre-tool Hook     Pipeline 插入步骤
```

一句话理解：

> PluginManager 本身不处理用户对话，它负责把插件声明的能力接到项目现有的 EventBus、工具系统和 Pipeline 上。

---

## 二、这不是 MCP 系统

当前插件是本地 Python 插件：

```text
PluginManager
   ↓ importlib 动态导入
plugins/xxx/plugin.py
   ↓
直接调用 Python 方法
```

它没有：

```text
MCP Client
MCP Server
mcp_add
McpToolWrapper
tools/list
tools/call
```

本地插件和主程序运行在同一个 Python 进程，并直接共享对象。

---

## 三、插件目录必须长什么样

PluginManager 只发现这种结构：

```text
plugins/
├── 01_sample/
│   ├── plugin.py                必需
│   ├── manifest.yaml            可选
│   ├── _conf_schema.json        可选
│   ├── plugin_config.json       可选
│   └── .kv.json                 运行后可能生成
└── weather/
    └── plugin.py
```

发现规则：

- `plugins` 下必须是直接子目录。
- 子目录中必须存在 `plugin.py`。
- 普通文件会跳过。
- 没有 `plugin.py` 的目录会跳过。
- 子目录按照名称排序后加载。

当前本地仓库没有实际 `plugins/*/plugin.py`，因此当前启动环境通常会发现 0 个插件。`tests/test_plugins.py` 会在临时目录中创建 Sample 插件用于测试。

---

## 四、PluginManager 在 main.py 中的位置

初始化顺序：

```text
创建 EventBus
创建 ToolRegistry
创建 ToolExecutor
        ↓
创建 Reasoner
        ↓
注册内置记忆工具
        ↓
创建 PluginManager
        ↓
load_all() 加载插件
        ↓
插件工具注册进 ToolRegistry
        ↓
插件 Hook 交给 ToolExecutor
        ↓
插件 PhaseModule 交给各个 Phase/Reasoner
```

内置工具先注册、插件工具后注册，因此同名插件工具可以覆盖内置工具。

---

## 五、`Plugin` 基类

源码：

```text
agent/plugins/base.py
```

```python
class Plugin(ABC):
    name: str | None = None
    version: str | None = None
    desc: str | None = None
    author: str | None = None
    context: PluginContext
```

插件可以提供基本信息：

```python
class WeatherPlugin(Plugin):
    name = "weather"
    version = "1.0.0"
    desc = "天气查询插件"
    author = "example"
```

### `initialize()`

```python
async def initialize(self) -> None:
    pass
```

插件加载并完成接线后调用。适合：

- 检查配置。
- 创建客户端。
- 预热资源。
- 初始化插件状态。

输入：无。

输出：`None`。

### `terminate()`

```python
async def terminate(self) -> None:
    pass
```

PluginManager 终止插件时调用。适合关闭客户端和释放资源。

### `__init_subclass__()` 的关键作用

当 Python 执行：

```python
class Sample(Plugin):
    ...
```

会自动调用：

```python
plugin_registry.register_class(cls)
```

因此插件类是在模块 import 过程中自动登记的：

```text
动态 import plugin.py
   ↓
执行 class Sample(Plugin)
   ↓
Plugin.__init_subclass__()
   ↓
PluginRegistry._classes[module_name] = Sample
```

一个模块如果定义多个 Plugin 子类，当前 `_classes` 以模块名为 key，后定义的类会覆盖先定义的类。

---

## 六、`PluginRegistry` 与 `PluginManager` 的区别

这两个名字容易混淆。

```text
PluginRegistry
└── 全局内存登记册
    保存插件类、实例和装饰器元数据

PluginManager
└── 生命周期管理器
    负责发现文件、import、实例化、接线、初始化和终止
```

全局对象：

```python
plugin_registry = PluginRegistry()
```

内部保存：

```text
_classes
└── import module name → Plugin class

_instances
└── import module name → Plugin instance

_handlers
└── PluginHandlerMetadata 列表
```

---

## 七、三类插件元数据

```python
class MetadataKind(Enum):
    LIFECYCLE = auto()
    TOOL = auto()
    TOOL_HOOK = auto()
```

### `LIFECYCLE`

表示生命周期处理器，例如：

```python
@on_before_reasoning(priority=10)
async def add_hint(self, event):
    ...
```

最终接到 EventBus。

### `TOOL`

表示插件向 LLM 提供的新工具：

```python
@tool(name="echo")
async def echo(self, event, text: str):
    ...
```

最终接到 ToolRegistry。

### `TOOL_HOOK`

表示工具执行前拦截器：

```python
@on_tool_pre(tool_name="echo")
async def rewrite(self, event):
    ...
```

最终接到 ToolExecutor。

---

## 八、`PluginHandlerMetadata` 保存什么

主要字段：

| 字段 | 作用 |
|---|---|
| `kind` | 生命周期、工具或工具 Hook |
| `event_type` | 生命周期事件类型 |
| `handler_type` | GATE 或 TAP |
| `handler` | 原始插件函数 |
| `handler_name` | 函数名 |
| `plugin_module_path` | 函数所属动态模块名 |
| `tool_name` | 工具暴露名称 |
| `tool_schema` | 自动生成的参数 Schema |
| `tool_risk` | 工具风险等级 |
| `tool_always_on` | 工具元数据 |
| `tool_search_hint` | 工具搜索提示元数据 |
| `hook_tool_name` | Hook 只匹配哪个工具 |
| `priority` | 生命周期处理器优先级 |
| `active` | 活跃标记，当前执行链没有使用它筛选 |

装饰器不是马上接入 EventBus 或 ToolRegistry，而是先创建这些元数据。

---

## 九、装饰器何时运行

例如插件代码：

```python
class Sample(Plugin):
    @tool(name="echo")
    async def echo(self, event, text: str):
        return text
```

Python import 模块时执行顺序：

```text
读到 @tool(...)
   ↓
创建装饰器 deco
   ↓
把 echo 函数传给 deco
   ↓
生成 PluginHandlerMetadata
   ↓
追加进全局 plugin_registry._handlers
   ↓
类创建完成
   ↓
__init_subclass__ 注册 Plugin class
```

此时只是“登记声明”，PluginManager 后面才把它绑定到真实 Plugin 实例并接到系统。

---

## 十、生命周期装饰器

当前公开装饰器：

```text
@on_before_turn          GATE
@on_before_reasoning     GATE
@on_prompt_render        GATE
@on_before_step          GATE
@on_after_step           TAP
@on_after_reasoning      GATE
@on_after_turn           TAP
```

其中：

```text
GATE
├── 可以修改并返回 Context
└── 返回 None 可以阻断对应生命周期

TAP
├── 主要用于观察、日志、遥测
└── 返回值被忽略
```

装饰器可接收 `priority`：

```python
@on_before_reasoning(priority=10)
```

priority 越高，越先执行。

---

## 十一、`@tool` 如何声明插件工具

示例：

```python
@tool(
    name="echo_plugin",
    risk="read-only",
    search_hint="echo",
)
async def echo_plugin(
    self,
    event,
    text: str,
) -> str:
    """Echo text.

    Args:
        text: Text to echo.
    """
    return f"echo:{text}"
```

装饰器要求前两个参数必须是：

```text
self, event
```

否则 import 插件时直接抛 `TypeError`。

`name` 是暴露给 LLM 的工具名。

`risk` 默认是 `read-write`，插件工具如果确定只读应显式写 `read-only`。

`always_on` 和 `search_hint` 会保存到 ToolMeta，但当前项目还没有用它们动态筛选工具。

---

## 十二、插件工具 Schema 如何自动生成

`_derive_params_schema()` 使用函数签名：

```python
async def search(
    self,
    event,
    query: str,
    limit: int = 5,
):
```

生成：

```json
{
  "type": "object",
  "properties": {
    "query": {"type": "string"},
    "limit": {"type": "integer"}
  },
  "required": ["query"]
}
```

规则：

- 跳过 `self` 和 `event`。
- 没有默认值的参数进入 `required`。
- 有默认值的参数不是必填。
- 从 `Args:` Docstring 中读取参数说明。

类型映射：

```text
str   → string
int   → integer
float → number
bool  → boolean
dict  → object
list/tuple/set → array
未知类型 → string
```

当前 Schema 推导比较轻量：数组不会自动生成完整 `items` Schema，也不会生成 enum、范围等高级约束。

---

## 十三、`@on_tool_pre` 如何声明工具 Hook

```python
@on_tool_pre(tool_name="echo_plugin")
async def rewrite_echo(self, event):
    return dict(
        event.arguments,
        text=event.arguments.get("text", "") + ":hooked",
    )
```

`tool_name` 指定只拦截某个工具。

如果不传：

```python
@on_tool_pre()
```

则匹配所有工具调用。

PluginManager 会把它包装成项目标准 `ToolHook`。

---

## 十四、`PluginManager.__init__()`

```python
def __init__(
    self,
    plugin_dirs,
    *,
    event_bus,
    tool_registry=None,
    workspace=None,
    memory_engine=None,
) -> None:
```

输入：

- 插件根目录列表。
- 共享 EventBus。
- 共享 ToolRegistry。
- 项目工作目录。
- 共享 MemoryEngine。

输出：PluginManager 对象。

内部创建：

```text
_loaded                 已成功加载的动态模块名集合
_tool_hooks              插件 ToolHook 列表
_before_turn_modules     各阶段 PhaseModule 列表
_before_reasoning_modules
_prompt_render_modules
_before_step_modules
_after_step_modules
_after_reasoning_modules
_after_turn_modules
```

刚创建时所有集合和列表都是空的，不会自动扫描插件。

---

## 十五、属性为什么返回副本

例如：

```python
@property
def tool_hooks(self):
    return list(self._tool_hooks)
```

以及：

```python
manager.before_turn_modules
manager.after_turn_modules
```

都返回新 list。

调用者修改返回值：

```python
hooks = manager.tool_hooks
hooks.clear()
```

不会直接清空 Manager 内部 `_tool_hooks`。

`loaded_count` 返回：

```python
len(self._loaded)
```

---

## 十六、`discover()`：发现插件目录

输入：无，使用初始化传入的 `_dirs`。

输出：插件描述字典列表。

假设：

```text
plugins/
├── 01_sample/plugin.py
└── weather/plugin.py
```

输出类似：

```python
[
    {
        "name": "01_sample",
        "module_path": "/project/plugins/01_sample/plugin.py",
        "import_path": "telegram_bot_plugin_plugins_01_sample",
    },
    {
        "name": "weather",
        "module_path": "/project/plugins/weather/plugin.py",
        "import_path": "telegram_bot_plugin_plugins_weather",
    },
]
```

这里要区分：

```text
module_path
└── 文件系统路径

import_path
└── Python 动态模块名，也是 Registry 中使用的 key
```

如果配置多个插件根目录，两个目录中有相同子目录名，后发现的重复插件会被跳过并记录 Warning。

---

## 十七、`load_all()`：顺序加载所有插件

```python
async def load_all(self) -> None:
    for mod in self.discover():
        await self._load_one(mod)
```

输入：无。

输出：`None`。

插件是顺序加载，不是并发加载。

```text
发现 plugin A
   ↓ await 完整加载
发现 plugin B
   ↓ await 完整加载
```

一个插件导入或初始化失败只记录 Warning 并跳过，不会阻止后面的插件继续加载。

这不是延迟加载：所有发现的插件都在启动阶段加载。

---

## 十八、`_load_one()` 的完整链路

这是 PluginManager 最重要的函数。

```text
检查是否已经加载
   ↓
动态 import plugin.py
   ↓
从全局 Registry 找 Plugin class
   ↓
创建插件实例
   ↓
应用 manifest.yaml
   ↓
创建并注入 PluginContext
   ↓
登记 Plugin instance
   ↓
绑定生命周期 handler
   ↓
注册插件工具
   ↓
包装并收集 Tool Hook
   ↓
收集 PhaseModules
   ↓
await instance.initialize()
   ├── 失败 → 回滚
   └── 成功 → 加入 _loaded
```

如果 `_loaded` 已包含该 `import_path`，函数直接返回，避免同一个 Manager 重复加载同一插件。

---

## 十九、`_import_plugin()`：动态执行 plugin.py

```python
spec = importlib.util.spec_from_file_location(...)
module = importlib.util.module_from_spec(spec)
sys.modules[module_name] = module
spec.loader.exec_module(module)
```

输入：动态模块名和 `plugin.py` 路径。

输出：`None`；副作用是执行整个插件模块。

```text
plugin.py 顶层 import
plugin.py 顶层语句
装饰器
class 定义
```

都会在 `exec_module()` 时执行。

因此本地插件不是安全配置文件，而是拥有当前 Python 进程权限的可执行代码。

---

## 二十、manifest.yaml 如何加载

可选文件：

```yaml
name: weather
version: 1.0.0
desc: 天气插件
author: alice
```

`_apply_manifest()` 只识别：

```text
name
version
desc
author
```

并覆盖实例属性。

当前实现不是完整 YAML 解析器，只按行寻找第一个 `:`，因此适合简单扁平键值，不支持复杂 YAML 嵌套、数组和多行文本。

文件不存在或读取失败时不会阻止插件加载，只记录 Warning 或直接返回。

---

## 二十一、PluginContext：插件拿到哪些能力

Manager 为每个实例注入：

```python
PluginContext(
    event_bus=...,
    tool_registry=...,
    plugin_id=...,
    plugin_dir=...,
    kv_store=...,
    config=...,
    workspace=...,
    memory_engine=...,
)
```

字段作用：

| 字段 | 作用 |
|---|---|
| `event_bus` | 访问共享事件总线 |
| `tool_registry` | 查询或注册工具 |
| `plugin_id` | 插件标识，优先使用 manifest/class name |
| `plugin_dir` | 插件自己的目录 |
| `kv_store` | 插件私有 JSON KV 存储 |
| `config` | 合并后的插件配置 |
| `workspace` | 项目工作目录 |
| `memory_engine` | 访问共享记忆能力 |

插件内部可以使用：

```python
self.context.memory_engine
self.context.kv_store
self.context.config
```

能力很强，也意味着插件必须被视为受信任代码。

---

## 二十二、插件配置怎样合并

`_load_plugin_config()` 读取两个文件。

### `_conf_schema.json`

例如：

```json
{
  "api_url": {
    "type": "string",
    "default": "https://example.com"
  },
  "timeout": {
    "type": "integer",
    "default": 10
  }
}
```

它只提取每个字段的 `default`。

### `plugin_config.json`

```json
{
  "timeout": 30
}
```

覆盖默认值。

结果：

```python
PluginConfig({
    "api_url": "https://example.com",
    "timeout": 30,
})
```

优先级：

```text
schema default
   ↓ 被覆盖
plugin_config.json
```

当前没有根据 `_conf_schema.json` 真正验证 override 的类型、必填项和范围。

---

## 二十三、`PluginConfig` 的使用

### `get(key, default)`

```python
self.context.config.get("timeout", 10)
```

输出示例：`30`。

### `as_dict()`

返回配置副本：

```python
{"api_url": "...", "timeout": 30}
```

### 属性访问

```python
self.context.config.timeout
```

存在时返回值，不存在时抛 `AttributeError`。

---

## 二十四、PluginKVStore：插件自己的简单持久化

每个插件默认使用：

```text
插件目录/.kv.json
```

例如：

```json
{
  "turns": 12,
  "initialized": true
}
```

### `get()`

```python
value = kv_store.get("turns", 0)
```

返回值或默认值。

### `set()`

```python
kv_store.set("initialized", True)
```

读取完整 JSON、修改一个 key，再重写完整文件。

### `increment()`

```python
new_value = kv_store.increment("turns")
```

原来是 12，输出并保存 13。

KVStore 使用 JSON 文件，不是 SQLite 数据库。当前没有文件锁、原子替换和多进程并发控制。

---

## 二十五、`_bind_handlers()`：接入 EventBus

PluginManager 遍历当前插件的 LIFECYCLE 元数据：

```python
bound = functools.partial(md.handler, instance)
```

原始未绑定函数：

```python
Sample.add_hint(self, event)
```

绑定后：

```python
bound(event)
```

其中 `instance` 已经固定，不需要 EventBus 传 self。

随后根据 HandlerType：

```python
event_bus.on(ctx_type, bound, priority=...)
```

或者：

```python
event_bus.observe(ctx_type, bound, priority=...)
```

完整关系：

```text
@on_before_reasoning
   ↓ Metadata
PluginManager._bind_handlers
   ↓
EventBus.on(BeforeReasoningCtx, bound_handler)
```

---

## 二十六、事件类型映射

Manager 使用 `_EVENT_TYPE_MAP`：

```text
BEFORE_TURN        → BeforeTurnCtx
BEFORE_REASONING   → BeforeReasoningCtx
PROMPT_RENDER      → PromptRenderCtx
BEFORE_STEP        → BeforeStepCtx
AFTER_STEP         → AfterStepCtx
AFTER_REASONING    → AfterReasoningCtx
AFTER_TURN         → AfterTurnCtx
BEFORE_TOOL_CALL   → BeforeToolCallCtx
AFTER_TOOL_RESULT  → AfterToolResultCtx
```

公开 decorators 当前没有直接导出 `on_before_tool_call` 和 `on_after_tool_result`，虽然类型映射已经预留。

---

## 二十七、`_register_tools()`：接入 ToolRegistry

对每个 `@tool` 元数据，Manager：

1. 把插件实例和 `event=None` 绑定到原始方法。
2. 取得插件函数接受的业务参数名。
3. 创建统一 Tool handler。
4. 构造 `Tool`。
5. 注册到共享 ToolRegistry。

```text
插件方法
async def echo(self, event, text)
        ↓ partial(instance, None)
bound_handler(text)
        ↓ 包装
handler(arguments, ctx)
        ↓
Tool(name, description, parameters, handler)
        ↓
ToolRegistry.register(... source_type="plugin")
```

当前插件工具的 `event` 参数实际收到 `None`；ToolRuntime 传入包装 handler 的 `ctx` 没有继续传给插件原方法。插件需要共享能力时通常使用 `self.context`。

---

## 二十八、插件工具参数过滤

`_accepted_tool_params()` 检查绑定后的函数签名。

插件声明：

```python
async def echo(self, event, text: str):
```

允许参数：

```python
frozenset({"text"})
```

模型传入：

```python
{
    "text": "hello",
    "unknown": 123,
}
```

包装 handler 过滤成：

```python
{"text": "hello"}
```

多余字段被静默丢弃。

插件方法支持同步或异步返回；包装器使用 `inspect.isawaitable()` 判断。最终统一执行 `str(result)`，因此插件工具当前主要返回文本，不会原样保留 `ToolResult` 结构。

---

## 二十九、插件工具覆盖内置工具

ToolRegistry 以名字为字典 key：

```python
self._tools[tool.name] = tool
```

因此：

```text
先注册 builtin: recall_memory
   ↓
插件再注册同名 recall_memory
   ↓
Registry 中变成插件版本
```

这符合 `main.py` 注释中“插件可按名称覆盖内置工具”的设计。

但当前没有保存被覆盖工具的栈。插件终止或初始化失败时调用 `unregister(name)`，不会自动恢复原来的内置同名工具。

---

## 三十、`_bind_tool_hooks()`：包装工具 Hook

PluginManager 将每个 `@on_tool_pre` 包装成 `_PluginToolHook`：

```python
_PluginToolHook(
    name="plugin:sample:rewrite_echo",
    handler=bound,
    tool_name_filter="echo_plugin",
)
```

这些对象先存入：

```python
plugin_manager._tool_hooks
```

然后 `main.py` 执行：

```python
tool_executor.add_hooks(plugin_manager.tool_hooks)
```

所以 PluginManager 只收集 Hook，真正每次调用工具时执行 Hook 的是 ToolExecutor。

---

## 三十一、`_PluginToolHook.matches()`

输入：`HookContext`。

输出：`bool`。

有过滤名称：

```text
当前工具名 == tool_name_filter → True
否则 → False
```

没有过滤名称：所有工具都返回 True。

---

## 三十二、`_PluginToolHook.run()`

它把 ToolExecutor 的 `HookContext` 转换成插件易用的 `PreToolCtx`：

```python
PreToolCtx(
    session_key=...,
    channel=...,
    chat_id=...,
    tool_name=...,
    arguments=...,
    call_id=...,
    source=...,
    request_text=...,
    tool_batch=...,
    tool_batch_index=...,
)
```

然后调用插件方法。

插件返回值规则：

```text
None
└── HookOutcome()，直接放行

HookOutcome
└── 原样使用，可 pass/deny/改参数/增加消息

dict
└── 当作 updated_input，替换工具参数

其他类型
└── deny，原因是不支持的返回类型
```

示例：

```python
return {"text": "hello:hooked"}
```

转成：

```python
HookOutcome(
    updated_input={"text": "hello:hooked"}
)
```

---

## 三十三、PhaseModule 是什么

除 EventBus handler 外，插件还可以提供更细粒度的阶段模块：

```python
class ReasoningHintModule:
    slot = "sample.before_reasoning_hint"
    requires = ("before_reasoning.emit",)

    async def run(self, frame):
        frame.slots["reasoning:extra_hint:sample"] = "slot-hint"
        return frame
```

`slot`：当前模块完成的唯一标记。

`requires`：哪些 Slot 已存在后才能运行。

`produces`：可选，声明产生哪些额外 Slot。

```text
Phase 建立内置锚点 slot
   ↓
PhaseModuleRunner 检查 requires
   ↓
条件满足就运行插件模块
   ↓
模块产生新 slot
   ↓
可能解锁下一个模块
```

这比单纯 EventBus 更适合表达“必须在某个具体内置步骤完成后运行”。

---

## 三十四、`_collect_phase_modules()`

PluginManager 检查插件是否提供以下方法：

```text
before_turn_modules()
before_reasoning_modules()
prompt_render_modules()
before_step_modules()
after_step_modules()
after_reasoning_modules()
after_turn_modules()
```

每个方法应该返回 list。

输入示例：

```python
def before_step_modules(self):
    return [BeforeStepStopModule()]
```

输出：模块被追加到 Manager 对应列表。

如果方法不存在或返回 `None`：当作空列表。

如果属性不可调用、调用抛异常或返回的不是 list：记录 Warning 并忽略。

---

## 三十五、PhaseModule 怎样传给 Pipeline

`main.py` 分别注入：

```text
BeforeTurnPhase
└── manager.before_turn_modules

BeforeReasoningPhase
├── manager.before_reasoning_modules
└── manager.prompt_render_modules

Reasoner
├── manager.before_step_modules
└── manager.after_step_modules

AfterReasoningPhase
└── manager.after_reasoning_modules

AfterTurnPhase
└── manager.after_turn_modules
```

因此 PluginManager 不执行这些模块，只负责收集和分发。

---

## 三十六、插件 initialize 失败如何回滚

在调用 initialize 前，Manager 记录：

```python
tool_names
hook_count_before
module_counts_before
```

如果：

```python
await instance.initialize()
```

抛异常，会：

```text
从 PluginRegistry 删除插件
   ↓
unregister 本插件工具
   ↓
删除本次新增 ToolHook
   ↓
把各 PhaseModule list 截回原长度
   ↓
不加入 _loaded
```

后面的插件仍继续加载。

### 当前回滚不完整的地方

生命周期 handler 在 initialize 之前已经通过 `_bind_handlers()` 注册到 EventBus。

EventBus 当前没有 `unsubscribe()`，所以 initialize 失败后虽然 Registry 元数据被删除，已经绑定进 EventBus 的 handler 仍然存在。

```text
bind EventBus handler
   ↓
initialize 失败
   ↓
其他部分回滚
   ↓
EventBus handler 无法移除
```

---

## 三十七、`terminate_all()`：终止全部插件

输入：无。

输出：`None`。

对每个 `_loaded` 模块：

```text
找到实例
   ↓
await instance.terminate()
   ↓
unregister 插件 Tool
   ↓
从全局 PluginRegistry 删除类、实例、元数据
```

最后清空：

```text
_loaded
_tool_hooks
所有 PhaseModule 列表
```

插件的 `terminate()` 抛异常时只记录 Warning，仍继续清理其他内容。

### 当前终止限制

- 不会从 EventBus 移除生命周期 handler。
- 已经复制到 ToolExecutor 的 Hook 不会因为 Manager 清空 `_tool_hooks` 自动消失。
- 已经复制到各 Phase 对象的 Module list 也不会自动更新。
- `_loaded` 是 set，终止顺序不是明确的加载逆序。

正常 `main.py` 在整个程序即将退出时调用 terminate，因此残留对象通常随进程一起结束；如果要支持运行中热卸载，这些问题就很重要。

---

## 三十八、Sample 插件完整例子

```python
from agent.plugins import (
    Plugin,
    on_after_turn,
    on_before_reasoning,
    on_tool_pre,
    tool,
)


class Sample(Plugin):
    name = "sample"

    async def initialize(self):
        self.context.kv_store.set("initialized", True)

    async def terminate(self):
        pass

    @tool(
        name="echo_plugin",
        risk="read-only",
        search_hint="echo",
    )
    async def echo_plugin(self, event, text: str) -> str:
        """Echo text.

        Args:
            text: Text to echo.
        """
        return f"echo:{text}"

    @on_tool_pre(tool_name="echo_plugin")
    async def rewrite_echo(self, event):
        return {
            **event.arguments,
            "text": event.arguments.get("text", "") + ":hooked",
        }

    @on_before_reasoning(priority=10)
    async def add_hint(self, event):
        event.extra_hints.append("plugin-hint")
        return event

    @on_after_turn()
    async def count_turn(self, event):
        self.context.kv_store.increment("turns")
```

执行 echo：

```text
模型调用 echo_plugin(text="hello")
   ↓
rewrite_echo Hook
   ↓
参数变成 text="hello:hooked"
   ↓
echo_plugin
   ↓
返回 "echo:hello:hooked"
```

---

## 三十九、完整启动链路

```text
main.py
   ↓
PluginManager([Path.cwd()/"plugins"], ...)
   ↓
load_all()
   ↓
discover()
   ↓
plugins/<name>/plugin.py
   ↓
_import_plugin()
   ├── 装饰器登记 metadata
   └── Plugin class 自动注册
   ↓
_load_one()
   ├── 创建 instance
   ├── manifest
   ├── config
   ├── kv_store
   └── PluginContext
   ↓
┌─────────────────────────────────────────────────┐
│ _bind_handlers()       → EventBus               │
│ _register_tools()      → ToolRegistry           │
│ _bind_tool_hooks()     → manager.tool_hooks     │
│ _collect_phase_modules → manager.*_modules      │
└─────────────────────────────────────────────────┘
   ↓
initialize()
   ↓
main.py 分发 hooks/modules
   ↓
Bot 开始处理对话
```

---

## 四十、文件和数据库操作

PluginManager 自身不连接 SQLite，不执行 SQL，也不创建数据库表。

它会进行文件操作：

| 文件操作 | 作用 |
|---|---|
| 遍历插件目录 | 发现 `plugin.py` |
| 读取并执行 `plugin.py` | 动态加载代码 |
| 读取 `manifest.yaml` | 插件元信息 |
| 读取 `_conf_schema.json` | 配置默认值 |
| 读取 `plugin_config.json` | 配置覆盖值 |
| 读写 `.kv.json` | 插件私有简单状态 |

插件可以通过：

```python
self.context.memory_engine
```

间接访问记忆数据库，但那是插件自己的业务行为，不是 PluginManager 内部的数据库操作。

---

## 四十一、安全边界

本地插件与主程序同进程运行，没有沙箱：

```text
插件代码
├── 可以 import Python 模块
├── 可以访问文件系统权限范围内的文件
├── 可以访问共享 EventBus
├── 可以访问 ToolRegistry
├── 可以访问 MemoryEngine
└── 异常或阻塞可能影响 Bot 进程
```

因此只应该加载可信插件。

`manifest.yaml`、配置 Schema 和 `risk` 元数据并不会限制插件 Python 代码本身的权限。

---

## 四十二、当前实现需要注意的细节

### 1. 当前仓库没有实际插件

测试有 Sample 插件，但项目根目录当前没有可发现的 `plugins/*/plugin.py`。

### 2. 启动时全部加载

没有延迟加载和按请求加载。

### 3. 没有热重载

文件变化后不会自动 reload。

### 4. EventBus handler 无法卸载

影响初始化失败回滚和运行中 terminate。

### 5. ToolExecutor 接收的是 Hook 列表副本

`main.py` 在 load_all 后执行一次 `add_hooks()`；之后新加载插件不会自动进入现有 Executor。

### 6. Phase 对象也接收 Module 列表副本

后续动态改变 Manager 列表不会自动改变已创建 Phase。

### 7. 同名工具覆盖没有恢复栈

卸载覆盖工具后，原工具不会自动恢复。

### 8. Plugin 工具 event 当前为 None

原始工具方法拿不到真实 Tool Context，通常依赖 `self.context`。

### 9. Plugin 工具输出强制转字符串

结构化 `ToolResult` 不会原样穿过包装器。

### 10. 配置 Schema 只取默认值

没有执行完整类型和约束校验。

### 11. manifest 不是完整 YAML 解析

只支持四个简单单行字段。

### 12. KVStore 不适合多进程并发

它是整文件读改写，没有锁。

### 13. 插件 import 会执行顶层代码

即使后续 initialize 失败，import 阶段产生的外部副作用也无法自动回滚。

---

## 四十三、主要函数输入输出速查表

| 函数 | 输入 | 输出/副作用 |
|---|---|---|
| `PluginManager.__init__()` | 目录和共享依赖 | 空 Manager |
| `discover()` | Manager 中的目录 | 插件描述列表 |
| `load_all()` | 无 | 顺序加载全部插件 |
| `_load_one(mod)` | 一个插件描述 | 完成接线或回滚 |
| `_import_plugin()` | 模块名、文件路径 | 执行 plugin.py |
| `_bind_handlers()` | 实例、模块名 | 注册 EventBus handler |
| `_register_tools()` | 实例、模块名 | 注册 Tool，返回工具名列表 |
| `_bind_tool_hooks()` | 实例、模块名 | 收集 ToolHook |
| `_collect_phase_modules()` | 实例 | 收集 PhaseModule |
| `_module_counts()` | 无 | 各模块列表长度字典 |
| `_rollback_phase_modules()` | 旧长度字典 | 截断新增模块 |
| `terminate_all()` | 无 | terminate 并清理登记 |
| `_accepted_tool_params()` | 绑定工具函数 | 允许参数名集合 |
| `_load_module_list()` | 实例、provider 名 | Module list 或空列表 |
| `_load_plugin_config()` | 插件目录 | `PluginConfig` |
| `_apply_manifest()` | 实例、插件目录 | 覆盖四个元信息字段 |
| `_PluginToolHook.matches()` | HookContext | 是否匹配当前工具 |
| `_PluginToolHook.run()` | HookContext | HookOutcome |

---

## 四十四、PluginManager 与其他系统的边界

```text
PluginManager
负责发现、加载和接线

PluginRegistry
负责暂存类、实例和元数据

EventBus
负责真正执行生命周期 handler

ToolRegistry
负责保存和调用插件 Tool

ToolExecutor
负责真正执行插件 Tool Hook

PhaseModuleRunner
负责按照 slot 依赖执行 PhaseModule

PluginKVStore
负责插件自己的简单 JSON 状态
```

PluginManager 是“组装者”，不是这些能力的最终执行者。

---

## 四十五、阅读时需要记住的关键点

- PluginManager 是本地 Python 插件加载器，不是 MCP。
- 它扫描 `plugins/*/plugin.py`，当前仓库实际没有该目录下的插件。
- import 插件时，装饰器登记 metadata，Plugin 子类自动登记 class。
- Manager 创建实例并注入 EventBus、ToolRegistry、KVStore、配置、工作区和 MemoryEngine。
- 生命周期装饰器最终接入 EventBus。
- `@tool` 最终包装成 Tool 并接入 ToolRegistry。
- `@on_tool_pre` 最终包装成 ToolHook，再由 main 交给 ToolExecutor。
- PhaseModule 通过 slot/requires 插入 Pipeline 的精确位置。
- 插件按目录名顺序、逐个加载，不是延迟或并发加载。
- initialize 失败会回滚 Tool、Hook 和 PhaseModule，但无法移除已注册的 EventBus handler。
- terminate 能移除插件工具和 Registry 记录，但当前不是真正完整的热卸载。
- 同名插件工具可以覆盖内置工具，卸载后不会自动恢复原工具。
- PluginKVStore 使用 `.kv.json`，不是数据库。
- 插件代码与主程序同进程、没有沙箱，只能加载可信代码。
