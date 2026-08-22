---
title: Reasoner：LLM推理、Function Calling与证据Guard循环
created: 2026-08-21
updated: 2026-08-21
tags: [telegram-bot, pipeline, reasoner, llm, function-calling, tool-runtime, guard]
description: 逐步讲解 Reasoner 在 PassiveTurnPipeline 中如何消费 BeforeReasoningCtx，通过 run_turn 最多四轮调用 LLM，判断文字或 tool_calls，执行 ToolRuntime、回填工具结果、运行记忆证据 Guard 与 Step 插件，最终返回 ReasonerResult。
source:
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/agent/pipeline/reasoner.py # 本文主要讲解对象
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/agent/pipeline/passive_turn.py # Reasoner 在总 Pipeline 中的调用位置
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/agent/core/types.py # BeforeReasoningCtx、BeforeStepCtx、AfterStepCtx 与 ReasonerResult
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/agent/tools/runtime.py # 单次工具调用的参数校验、Hook、超时、重试和结果信封
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/agent/tool_hooks/executor.py # 工具执行前后 Hook
  - /Users/zhengzhiyong/Desktop/work/telegram-bot/agent/core/event_bus.py # Step 与工具 TAP/GATE 生命周期
  - Cursor AI 对话，2026-08-21
---

# Reasoner：LLM推理、Function Calling与证据Guard循环

## `Reasoner`：大模型推理与工具调用循环

`main.py` 中创建 Reasoner：

```python
reasoner = Reasoner(
    tool_registry=tool_registry,
    tool_executor=tool_executor,
    event_bus=event_bus,
)
```

对应源码：

```text
agent/pipeline/reasoner.py
```

---

## 一、先说明：Reasoner 整套系统是干什么的

`Reasoner` 是 Pipeline 中真正调用大模型并组织“思考—使用工具—继续思考”的核心组件。

它不负责接收 Telegram 消息，也不负责最终发送消息。它接收已经准备好的 `BeforeReasoningCtx`，然后：

1. 把 System Prompt、历史消息、当前问题和工具 Schema 发给 DeepSeek。
2. 判断模型返回的是最终答案还是工具调用。
3. 如果模型请求工具，就调用工具系统。
4. 把工具结果作为 `role="tool"` 消息重新交给模型。
5. 必要时强制执行记忆检索、原文搜索和证据回源。
6. 最多进行 4 个推理 Step。
7. 最终返回 `ReasonerResult`。

```text
BeforeReasoningCtx
├── system prompt
├── 历史消息
├── 用户当前问题
├── 已召回记忆
└── 工具 Schema
        ↓
     Reasoner
        ↓
     调用 LLM
        │
        ├── 返回最终文字 ──────────────┐
        │                              │
        └── 返回 tool_calls            │
                ↓                      │
             执行工具                   │
                ↓                      │
             工具结果回填给 LLM          │
                └────→ 再次调用 LLM     │
                                       ↓
                                ReasonerResult
```

一句话理解：

> `Reasoner` 是大模型与本地工具系统之间的循环调度器。

---

## 二、Reasoner 在五阶段 Pipeline 中的位置

项目的被动对话 Pipeline：

```text
1. BeforeTurn
   获取 Session、召回初始记忆
        ↓
2. BeforeReasoning
   构建 Prompt、messages 和工具 Schema
        ↓
3. Reasoner
   调用 LLM、执行工具、生成最终答案
        ↓
4. AfterReasoning
   把答案包装成 OutboundMessage
        ↓
5. AfterTurn
   发布事件并发送 Telegram 消息
```

调用位置：

```python
result = await self.reasoner.run_turn(reasoning_ctx)
```

Reasoner 的输入由 `BeforeReasoningPhase` 准备，输出交给 `AfterReasoningPhase`。

---

## 三、输入 `BeforeReasoningCtx` 包含什么

```python
@dataclass
class BeforeReasoningCtx:
    session: Session
    memories: list[MemoryItem]
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    session_key: str = ""
    channel: str = "telegram"
    chat_id: str = ""
    content: str = ""
    timestamp: datetime = ...
    skill_names: list[str] = ...
    retrieved_memory_block: str = ""
    extra_hints: list[str] = ...
    abort: bool = False
    abort_reply: str = ""
    prompt_sections: list[Any] = ...
```

关键字段：

| 字段 | 作用 |
|---|---|
| `session` | 当前用户和聊天的 Session |
| `memories` | BeforeTurn 已经召回的记忆 |
| `messages` | 准备发送给 LLM 的完整消息列表 |
| `tools` | 允许 LLM 看见的工具 Schema |
| `content` | 当前用户问题 |
| `session_key` | 通常是 `user_id:chat_id` |
| `channel` | 当前渠道，默认 `telegram` |
| `retrieved_memory_block` | 已注入 Prompt 的记忆文本 |
| `prompt_sections` | System Prompt 的结构化区块 |

`messages` 通常长这样：

```python
[
    {"role": "system", "content": "你是一个有记忆能力的助手..."},
    {"role": "user", "content": "我以前说过喜欢喝什么？"},
    {"role": "assistant", "content": "...历史回复..."},
    {"role": "user", "content": "我现在喜欢喝什么？"},
]
```

`BeforeReasoningPhase` 已经完成 Prompt 构建，Reasoner 不再重新拼主 System Prompt。

---

## 四、输出 `ReasonerResult` 包含什么

```python
@dataclass
class ReasonerResult:
    content: str
    tool_calls: list[dict[str, Any]]
    finish_reason: str
```

示例：

```python
ReasonerResult(
    content="根据原始对话，你现在更喜欢喝茶。",
    tool_calls=[
        {
            "id": "call_1",
            "function": {
                "name": "recall_memory",
                "arguments": "{...}",
            },
            "result": "{...}",
            "status": "success",
        },
        {
            "id": "guard_fetch_2",
            "function": {
                "name": "fetch_messages",
                "arguments": "{...}",
            },
            "guard": "source_ref_requires_fetch",
            "result": "{...}",
            "status": "success",
        },
    ],
    finish_reason="stop",
)
```

`finish_reason` 常见值：

| 值 | 含义 |
|---|---|
| `stop` | 模型正常输出最终回答 |
| `early_stop` | BeforeStep 插件提前终止 |
| `max_iterations` | 4 个 Step 后仍未得到最终回答 |
| 其他模型值 | 使用 API 返回的 `choice.finish_reason` |

---

## 五、`Reasoner.__init__()`：初始化核心依赖

```python
def __init__(
    self,
    tool_registry=None,
    tool_executor=None,
    event_bus=None,
    before_step_modules=None,
    after_step_modules=None,
) -> None:
```

输入：

- `tool_registry`：保存可用 Tool。
- `tool_executor`：运行工具调用前 Hook。
- `event_bus`：发布 Step 和工具观察事件。
- `before_step_modules`：每次调用模型之前运行的插件模块。
- `after_step_modules`：每个推理 Step 结束后运行的插件模块。

输出：一个配置好的 Reasoner 对象。

### 1. 创建 OpenAI 兼容客户端

```python
self.client = AsyncOpenAI(
    api_key=settings.DEEPSEEK_API_KEY,
    base_url=settings.DEEPSEEK_BASE_URL,
    timeout=60.0,
)
```

虽然类名是 `AsyncOpenAI`，项目通过配置把它指向 DeepSeek 的 OpenAI 兼容接口：

```text
AsyncOpenAI SDK
   ↓ base_url
DeepSeek API
```

网络请求超时配置为 60 秒。

### 2. 读取模型名

```python
self.model = settings.LLM_MODEL
```

默认配置：

```text
deepseek-chat
```

### 3. 保存工具注册表

```python
self._tool_registry = tool_registry
```

Reasoner 不自己注册工具，只使用 `main.py` 传入的共享 Registry。

### 4. 准备 ToolExecutor

```python
self._tool_executor = tool_executor or ToolExecutor()
```

传入现有 Executor 就使用它；没有传入则创建新的空 Executor。

### 5. 创建 ToolRuntime

```python
self._tool_runtime = ToolRuntime(
    registry=self._tool_registry,
    executor=self._tool_executor,
)
```

ToolRuntime 负责参数解析、输入校验、Hook、超时、重试和统一结果封装。

### 6. 使用 EventBus

```python
self._event_bus = event_bus or EventBus.get_instance()
```

`main.py` 传入了共享 EventBus，因此 Reasoner 与其他 Phase、插件和日志组件使用同一事件总线。

### 7. 保存 Step 插件模块

```python
self.set_step_modules(...)
```

初始化完成后的结构：

```text
Reasoner
├── client: AsyncOpenAI → DeepSeek
├── model: deepseek-chat
├── ToolRegistry
├── ToolExecutor
├── ToolRuntime
├── EventBus
├── before_step_modules
└── after_step_modules
```

---

## 六、`main.py` 中为什么先创建 Reasoner，再注册工具

`main.py` 顺序：

```python
reasoner = Reasoner(
    tool_registry=tool_registry,
    tool_executor=tool_executor,
    event_bus=event_bus,
)

register_memory_tools(tool_registry, memory_runtime.engine)
```

这里不会导致 Reasoner 看不到后注册的工具，因为 Reasoner 保存的是同一个 Registry 对象的引用：

```text
main.tool_registry ───────────────┐
                                 ├── 同一个对象
Reasoner._tool_registry ──────────┘
```

向这个对象注册新工具后，Reasoner 立即可以查到。

---

## 七、`set_step_modules()`：设置每个 Step 的插件模块

```python
def set_step_modules(
    self,
    *,
    before_step=None,
    after_step=None,
) -> None:
```

输入：两个模块序列。

输出：`None`。

实现只是复制成列表：

```python
self._before_step_modules = list(before_step or [])
self._after_step_modules = list(after_step or [])
```

`main.py` 在插件加载后执行：

```python
reasoner.set_step_modules(
    before_step=plugin_manager.before_step_modules,
    after_step=plugin_manager.after_step_modules,
)
```

因此插件可以在每次 LLM 调用前后插入行为。

---

## 八、`add_tool_hooks()`：追加工具 Hook

```python
def add_tool_hooks(self, hooks) -> None:
    self._tool_executor.add_hooks(hooks)
```

输入：ToolHook 列表。

输出：`None`。

它只是转交给共享 `ToolExecutor`。当前 `main.py` 直接执行：

```python
tool_executor.add_hooks(plugin_manager.tool_hooks)
```

所以这个包装函数当前不是主启动链必须经过的路径。

---

## 九、`close()`：关闭 LLM 客户端

```python
async def close(self) -> None:
    await self.client.close()
```

输入：无。

输出：`None`。

作用：关闭 AsyncOpenAI 客户端持有的网络资源。

当前 `main.py` 的 finally 中没有显式调用 `reasoner.close()`；如果在脚本或测试中独立管理 Reasoner 生命周期，应该在结束时调用它。

---

## 十、最核心函数 `run_turn()`

```python
async def run_turn(
    self,
    ctx: BeforeReasoningCtx,
) -> ReasonerResult:
```

输入：准备完毕的 `BeforeReasoningCtx`。

输出：`ReasonerResult`。

函数首先复制消息列表并创建工具调用记录：

```python
messages = ctx.messages.copy()
tool_calls = []
```

`copy()` 是浅复制：Reasoner 追加消息时不会改变 `ctx.messages` 这个列表本身，但列表中原有的字典对象仍然共享引用。

然后进入最多 4 次循环：

```python
for iteration in range(_MAX_LLM_ITERATIONS):
```

其中：

```python
_MAX_LLM_ITERATIONS = 4
```

iteration 的值依次是：

```text
0 → 1 → 2 → 3
```

---

## 十一、为什么需要多轮推理循环

一个需要记忆证据的问题通常不能一次完成。

```text
第 0 个 Step
LLM：我要调用 recall_memory
        ↓
程序执行并返回记忆摘要

第 1 个 Step
LLM：我拿到线索了，现在根据原文回答
        ↓
输出最终答案
```

更完整的证据链可能是：

```text
Step 0：recall_memory
   ↓
Guard：search_messages
   ↓
Guard：fetch_messages
   ↓
Step 1：LLM 基于原文生成最终回答
```

模型每次工具调用后都需要再次看到工具结果，所以 Reasoner 必须循环调用 LLM。

---

## 十二、每轮开始：`_run_before_step()`

每次请求 LLM 前：

```python
step_ctx = await self._run_before_step(
    ctx,
    iteration,
    messages,
)
```

它创建：

```python
BeforeStepCtx(
    session_key=...,
    channel=...,
    chat_id=...,
    iteration=iteration,
    input_tokens_estimate=...,
    visible_tool_names=...,
)
```

字段作用：

| 字段 | 作用 |
|---|---|
| `iteration` | 当前是第几个推理 Step |
| `input_tokens_estimate` | 根据消息字符数粗略估计 Token |
| `visible_tool_names` | 当前 LLM 能看到的工具名 |
| `extra_hints` | 插件为当前 Step 增加的 System Hint |
| `early_stop` | 是否在调用 LLM 前提前结束 |
| `early_stop_reply` | 提前结束时直接返回的文字 |

它会：

```text
创建 BeforeStepCtx
   ↓
运行 before_step PhaseModule
   ↓
EventBus.emit(step_ctx) 执行 GATE handler
   ↓
再次运行依赖新 slots 的插件模块
   ↓
收集 step:extra_hint:* 导出
   ↓
读取 step:abort_reply
```

输入示例：

```python
iteration = 0
messages = [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "你好"},
]
```

输出示例：

```python
BeforeStepCtx(
    iteration=0,
    input_tokens_estimate=35,
    visible_tool_names=frozenset({"recall_memory", "memorize"}),
    extra_hints=[],
    early_stop=False,
)
```

---

## 十三、BeforeStep 如何提前停止

如果插件设置：

```python
step_ctx.early_stop = True
step_ctx.early_stop_reply = "当前请求已被停止"
```

或者通过 Slot 导出：

```python
frame.slots["step:abort_reply"] = "stopped-by-step"
```

`run_turn()` 直接返回：

```python
ReasonerResult(
    content="stopped-by-step",
    tool_calls=[],
    finish_reason="early_stop",
)
```

LLM API 不会被调用。

完整分支：

```text
BeforeStep
   ↓
early_stop?
   ├── 是 → 直接 ReasonerResult(early_stop)
   └── 否 → 继续调用 LLM
```

---

## 十四、Step Hint 怎样注入消息

如果 `step_ctx.extra_hints` 不为空：

```python
messages.append({
    "role": "system",
    "content": "# Step Hints\n" + "\n".join(step_ctx.extra_hints),
})
```

例如插件增加：

```python
["这一轮只能回答一句话", "优先使用原文证据"]
```

追加的消息：

```python
{
    "role": "system",
    "content": (
        "# Step Hints\n"
        "这一轮只能回答一句话\n"
        "优先使用原文证据"
    ),
}
```

注意：每次 Step 新增的 Hint 会追加到本地 `messages`，后续 Step 仍然会看到它。

---

## 十五、调用 DeepSeek API

核心代码：

```python
response = await self.client.chat.completions.create(
    model=self.model,
    messages=messages,
    tools=ctx.tools,
)
```

输入包括：

- `model`：模型名称。
- `messages`：完整上下文。
- `tools`：OpenAI function calling 格式的工具 Schema。

模型可能返回两类结果：

```text
choice.message
├── content：最终文字
└── tool_calls：工具调用请求
```

Reasoner 只读取：

```python
choice = response.choices[0]
message = choice.message
```

即使用第一个 choice。

---

## 十六、LLM API 异常如何重试

```python
except Exception:
    if iteration == 0:
        await asyncio.sleep(0.5)
        continue
    raise
```

行为：

```text
第 0 个 iteration API 失败
   ↓
等待 0.5 秒
   ↓
进入 iteration 1 再试

iteration 1/2/3 API 失败
   ↓
异常继续向上抛出
```

它只给第一次 API 调用一次容错机会，而且失败的 iteration 也占用 4 次循环预算。

这与 ToolRuntime 的工具重试是两套不同机制：

```text
LLM API 重试
└── Reasoner.run_turn() 负责

工具调用重试
└── ToolRuntime 负责
```

---

## 十七、分支 A：模型直接返回最终回答

如果：

```python
message.tool_calls == []
```

并不一定马上返回。Reasoner 会先运行三种 Memory Guard：

```text
1. Explicit Recall Guard
2. Raw Search Guard
3. Evidence Fetch Guard
```

如果 Guard 补执行了工具：

```text
模型本来想直接回答
   ↓
代码发现缺少必要证据
   ↓
自动执行工具
   ↓
把工具结果追加进 messages
   ↓
continue，进入下一次 LLM 调用
```

如果没有 Guard 需要执行：

1. 调用 `_run_after_step(..., has_more=False)`。
2. 调用 `_apply_final_answer_guard()`。
3. 返回 `ReasonerResult`。

简单输入输出例子：

```text
用户：你好
LLM：你好！有什么可以帮你？
```

返回：

```python
ReasonerResult(
    content="你好！有什么可以帮你？",
    tool_calls=[],
    finish_reason="stop",
)
```

---

## 十八、分支 B：模型返回 `tool_calls`

模型可能返回：

```json
{
  "id": "call_1",
  "type": "function",
  "function": {
    "name": "recall_memory",
    "arguments": "{\"query\":\"用户的职业\",\"memory_type\":\"profile\"}"
  }
}
```

Reasoner 先把调用保存进 `tool_calls` 历史：

```python
tool_calls.extend([...])
```

然后必须把模型这条 assistant tool-call 消息放进 `messages`：

```python
messages.append({
    "role": "assistant",
    "content": message.content or "",
    "tool_calls": [...],
})
```

接着逐个执行工具：

```python
for idx, tc in enumerate(message.tool_calls):
    result = await self._execute_tool(...)
```

最后追加工具结果消息：

```python
messages.append({
    "role": "tool",
    "tool_call_id": tc.id,
    "content": result,
})
```

消息演化：

```text
初始 messages
├── system
└── user

LLM 请求工具后
├── system
├── user
└── assistant(tool_calls=[call_1])

工具执行后
├── system
├── user
├── assistant(tool_calls=[call_1])
└── tool(tool_call_id=call_1, content=结果)
```

下一次 LLM 调用就能看到工具结果。

---

## 十九、多个 Tool Call 如何执行

模型一次可能返回多个工具调用：

```text
tool_calls
├── call_1: memorize preference
└── call_2: memorize profile
```

当前代码使用：

```python
for idx, tc in enumerate(message.tool_calls):
    result = await self._execute_tool(...)
```

因此是顺序执行：

```text
执行 call_1
   ↓ 等完成
执行 call_2
```

不是 `asyncio.gather()` 并发执行。

顺序执行能减少多个写工具同时修改数据时的竞争，但延迟是各工具耗时相加。

---

## 二十、`_tool_call_batch_snapshot()` 做什么

输入：模型当前一次返回的 Tool Call 对象列表。

输出：不可变 tuple，每项只保留名称和已解析参数。

输入示例：

```text
call_1 → memorize({"summary": "喜欢茶"})
call_2 → memorize({"summary": "住北京"})
```

输出：

```python
(
    {"name": "memorize", "arguments": {"summary": "喜欢茶"}},
    {"name": "memorize", "arguments": {"summary": "住北京"}},
)
```

这个快照连同 `tool_batch_index` 传给 ToolRuntime，方便 Tool Hook 理解当前调用在整个批次中的位置。

---

## 二十一、`_execute_tool()`：连接 Reasoner 和 ToolRuntime

函数签名：

```python
async def _execute_tool(
    tool_name,
    arguments,
    ctx,
    *,
    call_id="",
    tool_batch=(),
    tool_batch_index=0,
) -> str:
```

输入：

- 工具名。
- 字典或 JSON 字符串参数。
- 当前 `BeforeReasoningCtx`。
- 调用 ID 和批次信息。

输出：ToolRuntime 统一结果信封的 JSON 字符串。

完整流程：

```text
提取 session_key/channel/chat_id
   ↓
EventBus.observe(BeforeToolCallCtx)
   ↓
ToolRuntime.execute_call()
   ├── 参数解析
   ├── Schema 校验
   ├── Tool Hook
   ├── 超时与重试
   └── Tool handler
   ↓
ToolRuntimeResult.to_json()
   ↓
EventBus.observe(AfterToolResultCtx)
   ↓
返回 JSON 字符串
```

输入示例：

```python
await reasoner._execute_tool(
    "recall_memory",
    '{"query":"用户职业","memory_type":"profile"}',
    ctx,
    call_id="call_1",
)
```

输出示例：

```json
{
  "ok": true,
  "status": "success",
  "data": {
    "count": 1,
    "items": [{"summary": "用户是程序员"}]
  },
  "error": null,
  "meta": {
    "tool_name": "recall_memory",
    "call_id": "call_1"
  }
}
```

---

## 二十二、工具前后 EventBus 事件

工具调用前：

```python
await event_bus.observe(BeforeToolCallCtx(...))
```

工具调用后：

```python
await event_bus.observe(AfterToolResultCtx(...))
```

这两个是 TAP 观察事件，适合：

- 日志。
- 遥测。
- 统计工具次数。
- 调试工具输入输出。

```text
BeforeToolCallCtx TAP
   ↓
真正执行工具
   ↓
AfterToolResultCtx TAP
```

TAP handler 的返回值不会修改工具调用。真正能修改参数或拒绝调用的是 ToolExecutor 的 `pre_tool_use` Hook。

---

## 二十三、`_annotate_tool_call_from_runtime()` 做什么

ToolRuntime 返回结果后，Reasoner 不只保存原始 JSON，还提取关键运行信息放进调用记录。

输入：

```python
call = {
    "function": {"name": "echo", "arguments": "{...}"}
}
```

以及 Runtime JSON：

```json
{
  "status": "error",
  "error": {"code": "argument_parse"},
  "meta": {
    "final_arguments": {},
    "retry_count": 0
  }
}
```

处理后：

```python
call = {
    ...,
    "status": "error",
    "error_code": "argument_parse",
    "final_arguments": {},
    "retry_count": 0,
}
```

这样 `ReasonerResult.tool_calls` 不仅能用于回放，还能看到最终参数、错误码和重试次数。

---

## 二十四、为什么还要三类 Memory Guard

模型看到工具 Schema，并不保证它一定按规则调用工具。

例如系统已经把一条记忆摘要注入 Prompt，模型可能直接根据摘要回答，跳过原文取证。

Reasoner 增加代码级 Guard：

```text
LLM 自主决策
   ↓
Reasoner 检查证据链是否完整
   │
   ├── 缺显式长期记忆检索 → recall guard
   ├── 更新/变化问题缺原始搜索 → raw search guard
   └── 有 source_ref 但没取原文 → fetch guard
```

这是一种“模型决策 + 确定性代码校验”的混合方式。

---

## 二十五、Explicit Recall Guard

执行函数：

```python
_run_explicit_recall_guard(...)
```

触发条件由 `_should_force_explicit_recall()` 判断：

```text
recall_memory 对当前模型可见
   AND
本轮还没有调用 recall_memory
   AND
ctx 中已经存在记忆上下文
   AND
用户问题包含记忆相关关键词
```

关键词包括：

```text
我、我的、推荐、建议、喜欢、偏好、记得、以前、现在、做过……
```

它解决的问题：

```text
Prompt 已注入一段被动召回记忆
   ↓
LLM 想直接根据摘要回答
   ↓
Guard 强制调用 recall_memory
   ↓
最终答案以显式检索结果为准
```

Guard 创建的调用记录会包含：

```python
"guard": "passive_memory_requires_explicit_recall"
```

输入：`ctx`、可变 `messages`、可变 `tool_calls`。

输出：

```python
["recall_memory"]  # 执行了
[]                  # 没执行
```

---

## 二十六、Recall Guard 怎样生成参数

`_recall_guard_args()` 默认生成：

```python
{
    "query": "用户当前问题相关的长期记忆：...",
    "limit": 5,
}
```

如果当前已召回记忆全是同一类型：

```python
{"memory_type": "preference"}
```

如果用户问“以前、后来、变化、更新、现在”：

```python
{"include_superseded": True}
```

例如用户问：

```text
我以前和现在分别喜欢喝什么？
```

可能生成：

```python
{
    "query": "用户当前问题相关的长期记忆：我以前和现在分别喜欢喝什么？...",
    "limit": 5,
    "memory_type": "preference",
    "include_superseded": True,
}
```

---

## 二十七、Raw Search Guard

执行函数：

```python
_run_raw_search_guard(...)
```

触发条件：

```text
search_messages 对当前模型可见
   AND
本轮已经调用 recall_memory
   AND
还没有调用 search_messages
   AND
用户问题包含更新、历史、技术栈、项目等关键词
```

关键词包括：

```text
现在、以前、变化、更新、后来、不再、戒、改、换、项目、技术栈……
```

作用：记忆摘要可能不完整或有更新冲突，所以再到原始 Session 消息中搜索线索。

生成调用：

```python
search_messages({
    "query": "咖啡 茶 喜欢 现在 以前",
    "role": "user",
    "limit": 10,
})
```

调用记录标记：

```python
"guard": "recall_requires_raw_search"
```

输出同样是：

```python
["search_messages"]
```

或者：

```python
[]
```

---

## 二十八、搜索关键词怎样生成

`_search_guard_query()` 会组合：

- 用户当前问题。
- `ctx.memories` 的摘要。
- `recall_memory` 返回的 items 摘要。

然后从中提取：

- 预定义领域词，例如咖啡、城市、Python、Rust、音乐。
- 动作词，例如喜欢、常用、改喝、推荐。
- 英文/数字 Token。

最多取前 10 个去重词。

输入示例：

```text
我以前用 Python，现在项目改用 Rust 了吗？
```

输出可能是：

```text
Python Rust 技术栈 项目 现在 以前
```

如果没有提取到词，则退回用户原问题或前 80 个字符。

---

## 二十九、Evidence Fetch Guard

执行函数：

```python
_run_evidence_fetch_guard(...)
```

它检查 `recall_memory` 和 `search_messages` 的结果：

```text
结果里出现 source_ref
   ↓
本轮是否已经 fetch 过这个 ref？
   ├── 是 → 不重复读取
   └── 否 → 强制 fetch_messages
```

调用参数：

```python
{
    "source_refs": source_refs[:5],
    "context": 2,
    "limit": 20,
}
```

最多一次回源前 5 个 source_ref，并取前后各 2 条上下文。

调用记录标记：

```python
"guard": "source_ref_requires_fetch"
```

然后追加 System 消息，要求模型：

- 基于原文证据回答。
- 证据不足时明确说明。
- 当前状态优先使用 active 记忆。
- 只有询问历史时才把 superseded 当历史结论。

---

## 三十、source_ref 去重和已取证判断

`_pending_evidence_source_refs()` 会：

1. 从 recall/search 结果提取证据 ref。
2. 从 fetch_messages 参数和结果提取已读取 ref。
3. 去重。
4. 删除已经读取过的 ref。

`_source_refs_from_payload()` 能从以下位置提取：

```text
payload.source_ref
payload.source_refs[]
payload.items[].source_ref
payload.messages[].source_ref
```

`_is_ref_fetched()` 还处理 Session 父级 ref：

```text
已 fetch：session:1:2
待检查：session:1:2#msg:5
```

它认为子消息已经包含在 Session 级 fetch 中，不再重复读取。

---

## 三十一、Guard 如何伪装成标准工具对话

Guard 不是只在 Python 内部偷偷调用工具，它会同时向 `messages` 追加标准协议消息：

```text
assistant(tool_calls=[guard call])
   ↓
tool(tool_call_id=guard call id, content=result)
   ↓
system(说明为何强制补充证据)
```

这样下一轮 LLM 能正确理解：

- 哪个工具被调用。
- 调用参数是什么。
- 返回结果是什么。
- 为什么代码强制执行了它。

Guard 的 call ID 格式：

```text
guard_recall_N
guard_search_N
guard_fetch_N
```

---

## 三十二、`_run_after_step()`：记录每个 Step 的结果

每个推理 Step 结束时创建：

```python
AfterStepCtx(
    session_key=...,
    channel=...,
    chat_id=...,
    iteration=iteration,
    tools_called=...,
    partial_reply=...,
    tools_used_so_far=...,
    tool_chain_partial=...,
    partial_thinking=None,
    has_more=...,
)
```

关键字段：

| 字段 | 作用 |
|---|---|
| `tools_called` | 当前 Step 执行了哪些工具 |
| `tools_used_so_far` | 从开始到当前累计使用的工具 |
| `tool_chain_partial` | 截至当前完整工具调用记录 |
| `partial_reply` | 当前模型返回的部分文字 |
| `has_more` | 是否还要继续下一个推理 Step |
| `extra_metadata` | 插件收集的遥测信息 |

它会运行 after_step PhaseModule，并执行：

```python
await self._event_bus.observe(step_ctx)
```

这是 TAP 事件，只用于观察和遥测，不修改主流程返回值。

输入示例：

```python
iteration=0
tools_called=("recall_memory", "fetch_messages")
partial_reply=""
has_more=True
```

输出：填充后的 `AfterStepCtx`。

---

## 三十三、`has_more` 什么时候为 True

```text
模型调用了工具
└── has_more=True

模型没调用工具，但 Guard 补了工具
└── has_more=True

模型直接给最终答案，Guard 也不需要运行
└── has_more=False
```

它主要用于插件和遥测判断当前推理链是否已经结束。

---

## 三十四、Final Answer Guard

模型最终输出后，Reasoner 调用：

```python
content = _apply_final_answer_guard(
    ctx,
    message.content or "",
    tool_calls,
)
```

它只在以下情况介入：

```text
回答非空
   AND
本轮调用过 recall_memory 或 fetch_messages
   AND
用户问题包含“推荐”或“建议”
```

一般作用是确保回答明确说明依据来自用户偏好或技术栈。

例如模型回答：

```text
可以使用 FastAPI。
```

可能改成：

```text
根据你擅长的技术栈，可以使用 FastAPI。
```

### 特殊项目建议规则

如果用户问项目/技术栈，并且 fetch 的原文中同时出现：

- `Python`
- `Django` 或 `FastAPI`

代码会直接构造：

```text
根据你擅长的技术栈，你可以用 Python 的 Django 或 FastAPI 框架来做后端。
```

这是一段确定性的业务规则，不是模型本身生成的内容。

---

## 三十五、达到最大循环次数会怎样

如果 4 个 iteration 都没有得到最终文字，例如模型每次都继续调用工具：

```python
return ReasonerResult(
    content="抱歉，处理请求时遇到问题。",
    tool_calls=tool_calls,
    finish_reason="max_iterations",
)
```

流程：

```text
iteration 0 → tool call
iteration 1 → tool call
iteration 2 → tool call
iteration 3 → tool call
循环结束
   ↓
返回兜底错误文案
```

这个上限避免模型和工具陷入无限循环。

---

## 三十六、几个重要辅助函数

### `_tool_context(ctx)`

输入：`BeforeReasoningCtx`。

输出：

```python
(session_key, channel, chat_id)
```

缺省时会回退到：

```python
session_key = f"{user_id}:{chat_id}"
channel = "telegram"
chat_id = str(ctx.session.chat_id)
```

### `_tool_is_visible(ctx, tool_name)`

检查工具 Schema 是否存在于 `ctx.tools`。

```python
_tool_is_visible(ctx, "recall_memory")
```

输出：`True` 或 `False`。

Guard 不会强制调用模型根本看不到的工具。

### `_last_user_content(ctx)`

从 `ctx.messages` 末尾向前找最后一条 `role="user"`；找不到时回退到 `ctx.content`。

输入：ctx。

输出示例：

```text
我现在喜欢喝什么？
```

### `_dominant_memory_type(ctx)`

如果所有已召回记忆类型相同，返回该类型；混合类型或没有记忆时返回空字符串。

```text
[preference, preference] → "preference"
[profile, preference]    → ""
[]                       → ""
```

### `_asks_for_history_or_updates(content)`

如果问题包含以前、后来、变化、更新、现在等词，返回 `True`。

### `_estimate_message_tokens(messages)`

它不使用真正 tokenizer，而是：

```python
总字符数 // 3
```

最小返回 1。

例如所有消息总计 300 个字符：

```python
input_tokens_estimate = 100
```

这只是粗略估算，不是模型真实计费 Token 数。

---

## 三十七、JSON 辅助函数

### `_load_raw_json_object(raw)`

输入 JSON 字符串，解析成功且顶层是对象时返回字典，否则返回 `{}`。

```text
'{"a":1}' → {"a": 1}
'[1,2]'    → {}
'{bad'     → {}
```

### `_load_json_object(raw)`

先解析 JSON，再调用 `unwrap_tool_envelope()`。

如果输入是 ToolRuntime 信封：

```json
{"ok":true,"data":{"items":[]}}
```

返回内部业务数据：

```python
{"items": []}
```

### `_append_ref()`

把非空 source_ref 转成字符串后追加到列表。

### `_dedupe_refs()`

按第一次出现顺序去重。

```python
["a", "b", "a"] → ["a", "b"]
```

---

## 三十八、一次普通问候的完整链路

用户：

```text
你好
```

链路：

```text
BeforeReasoningCtx
   ↓
run_turn iteration=0
   ↓
BeforeStep，没有 early_stop
   ↓
调用 DeepSeek
   ↓
message.content="你好！有什么可以帮你？"
message.tool_calls=[]
   ↓
三种 Guard 均不触发
   ↓
AfterStep(has_more=False)
   ↓
Final Answer Guard 不修改
   ↓
ReasonerResult(
  content="你好！有什么可以帮你？",
  tool_calls=[],
  finish_reason="stop"
)
```

---

## 三十九、一次记忆问答的完整链路

用户：

```text
我是什么职业？
```

可能链路：

```text
iteration 0
   ↓
LLM 返回 recall_memory tool call
   ↓
BeforeToolCallCtx TAP
   ↓
ToolRuntime 执行 recall_memory
   ↓
AfterToolResultCtx TAP
   ↓
结果包含 source_ref=session:1:1#msg:0
   ↓
Evidence Fetch Guard 检测到未回源
   ↓
自动执行 fetch_messages
   ↓
把 recall 和 fetch 结果写入 messages
   ↓
AfterStep(has_more=True)
   ↓
continue

iteration 1
   ↓
LLM 看见原文证据
   ↓
返回“根据原文，你是程序员。”
   ↓
没有新 Guard
   ↓
AfterStep(has_more=False)
   ↓
返回 ReasonerResult
```

工具记录：

```text
tool_calls
├── recall_memory（模型主动）
└── fetch_messages（Guard 自动补充）
```

---

## 四十、一次模型漏调工具的链路

假设 Prompt 已经注入“用户喜欢拿铁”，模型准备直接回答：

```text
你喜欢拿铁。
```

但用户问的是记忆相关问题，代码发现没有显式 recall：

```text
LLM 返回直接答案
   ↓
Explicit Recall Guard 触发
   ↓
自动调用 recall_memory
   ↓
若结果有 source_ref
   ↓
Evidence Fetch Guard 自动调用 fetch_messages
   ↓
把证据加入 messages
   ↓
不采用模型刚才的直接答案
   ↓
进入下一次 LLM 推理
```

这能降低模型只凭 Prompt 摘要直接下结论的风险。

---

## 四十一、Reasoner 是否操作数据库

Reasoner 本身：

- 不连接 SQLite。
- 不执行 SQL。
- 不创建数据库表。
- 不直接保存 Session。
- 不直接插入长期记忆。

它只负责调用工具：

```text
Reasoner
   ↓ ToolRuntime
记忆 Tool handler
   ↓ MemoryEngine
Store / SessionStore
   ↓
SQLite 和向量索引
```

例如：

```text
recall_memory → 间接查询长期记忆
search_messages → 间接查询原始 Session 消息
fetch_messages → 间接读取原始消息
memorize → 间接写入长期记忆
```

数据库操作属于具体 Tool 的下游，不属于 Reasoner 类本身。

Reasoner 会直接进行网络操作：通过 `AsyncOpenAI` 调用 DeepSeek API。

---

## 四十二、当前实现需要注意的细节

### 1. 最多 4 个 Step

它防止无限工具循环，但复杂任务可能在取证完成前耗尽预算。

### 2. 多工具顺序执行

一次返回多个 tool_calls 时不并发。

### 3. 第一次 LLM 错误只重试一次

只有 `iteration == 0` 的 API 异常会等待 0.5 秒后重试；后续异常直接抛出。

### 4. API 失败会消耗 iteration

第一次失败后从 iteration 1 继续，而不是重新执行 iteration 0。

### 5. Guard 是项目特定规则

关键词和 Final Answer Guard 明显针对记忆评估、偏好更新、技术栈建议等场景，并非通用 Agent 框架必然需要的规则。

### 6. Token 数只是字符估算

`字符数 // 3` 不等同于 DeepSeek tokenizer 的真实 Token。

### 7. `ctx.messages.copy()` 是浅复制

Reasoner 主要追加新项，不修改原项；若未来修改内部字典，仍会影响共享对象。

### 8. `_observe_tool_result()` 当前未被调用

文件中保留了这个辅助方法，但当前所有正式工具调用通过 `_execute_tool()` 自己发送 AfterToolResultCtx。

### 9. `close()` 当前主流程没有显式调用

独立使用 Reasoner 时需要注意客户端资源释放。

### 10. Final Answer Guard 可能改写模型答案

最终 `ReasonerResult.content` 不一定与 `message.content` 完全相同。

### 11. Guard 调用也计入 tool_calls

所以 `ReasonerResult.tool_calls` 包含模型主动调用和代码自动补充调用，要通过 `guard` 字段区分。

---

## 四十三、主要函数输入输出速查表

| 函数 | 输入 | 输出 |
|---|---|---|
| `__init__()` | Registry、Executor、EventBus、插件模块 | Reasoner 对象 |
| `set_step_modules()` | before/after step 模块 | `None` |
| `add_tool_hooks()` | Hook 列表 | `None` |
| `close()` | 无 | `None`，关闭客户端 |
| `run_turn(ctx)` | `BeforeReasoningCtx` | `ReasonerResult` |
| `_execute_tool(...)` | 工具名、参数、ctx | Runtime JSON 字符串 |
| `_observe_tool_result(...)` | 工具结果信息 | `None`，发送 TAP |
| `_run_before_step(...)` | ctx、iteration、messages | `BeforeStepCtx` |
| `_run_after_step(...)` | Step 结果信息 | `AfterStepCtx` |
| `_run_explicit_recall_guard(...)` | ctx/messages/tool_calls | 执行的工具名列表 |
| `_run_raw_search_guard(...)` | ctx/messages/tool_calls | 执行的工具名列表 |
| `_run_evidence_fetch_guard(...)` | ctx/messages/tool_calls | 执行的工具名列表 |
| `_tool_call_batch_snapshot()` | API tool_calls | 工具批次 tuple |
| `_annotate_tool_call_from_runtime()` | 调用记录、Runtime JSON | `None`，原地补字段 |
| `_tool_context()` | ctx | `(session_key, channel, chat_id)` |
| `_tool_is_visible()` | ctx、工具名 | `bool` |
| `_last_user_content()` | ctx | 最后一条用户文本 |
| `_estimate_message_tokens()` | messages | 粗略 Token 数 |

---

## 四十四、阅读 Reasoner 时最容易混淆的关系

### Reasoner 与 LLM

```text
LLM 负责根据上下文做语言和工具选择
Reasoner 负责反复调用 LLM 并执行选择结果
```

### Reasoner 与 ToolRuntime

```text
Reasoner 决定何时执行工具并维护消息链
ToolRuntime 负责一次工具调用的安全执行
```

### Reasoner 与 BeforeReasoningPhase

```text
BeforeReasoningPhase 构建 messages 和 tools
Reasoner 消费它们进行推理
```

### Reasoner 与 AfterReasoningPhase

```text
Reasoner 返回 ReasonerResult
AfterReasoningPhase 把结果包装成 OutboundMessage
```

### Reasoner 与 EventBus

```text
BeforeStep 用 GATE，可修改或提前停止
AfterStep 用 TAP，主要观察和遥测
Before/After Tool 用 TAP，观察工具输入输出
```

---

## 四十五、最终总链路

```text
BeforeReasoningPhase.build_ctx()
   ├── 构建 system prompt
   ├── 加入 Session 历史
   ├── 加入用户当前问题
   └── 加入 Tool Schema
           ↓
Reasoner.run_turn(ctx)
           ↓
┌──────────────── iteration 0..3 ────────────────┐
│                                                │
│  _run_before_step()                            │
│      ├── 插件模块                               │
│      ├── EventBus GATE                         │
│      └── early_stop / extra_hints              │
│             ↓                                  │
│  DeepSeek chat.completions.create()            │
│             ↓                                  │
│      ┌──────┴────────┐                         │
│      │               │                         │
│  tool_calls       final text                   │
│      │               │                         │
│  _execute_tool()     ├── Recall Guard          │
│      ├── TAP before  ├── Search Guard          │
│      ├── Runtime     └── Fetch Guard           │
│      └── TAP after          │                   │
│      │                      │                   │
│  append role=tool      Guard 执行工具？          │
│      │                 ├── 是 → continue       │
│      │                 └── 否 → 最终回答        │
│      ↓                                          │
│  _run_after_step(has_more=True/False)           │
│      ↓                                          │
│  continue 或 return                             │
└────────────────────────────────────────────────┘
           ↓
ReasonerResult
├── content
├── tool_calls
└── finish_reason
           ↓
AfterReasoningPhase
```

---

## 四十六、阅读时需要记住的关键点

- Reasoner 是 LLM 与工具系统之间的循环调度器。
- 它的输入是 `BeforeReasoningCtx`，输出是 `ReasonerResult`。
- BeforeReasoningPhase 已经准备好 Prompt、历史和工具 Schema。
- 每个 Step 先执行 BeforeStep 扩展，再调用 DeepSeek。
- 模型返回 tool_calls 时，Reasoner 逐个执行工具并把结果作为 `role="tool"` 加回消息链。
- 工具调用通过 ToolRuntime，具备校验、Hook、超时、重试和结果封装。
- BeforeToolCallCtx 与 AfterToolResultCtx 是 EventBus TAP 观察事件。
- 模型直接回答时，Reasoner 仍会检查记忆证据链是否完整。
- Recall Guard 保证显式检索，Search Guard 补原始搜索，Fetch Guard 强制回源取证。
- Guard 工具调用也会记录在 `ReasonerResult.tool_calls` 中。
- 每个 Step 后产生 AfterStepCtx，供插件和遥测观察。
- 最终答案可能被 Final Answer Guard 调整。
- 循环最多 4 次，超限返回 `max_iterations` 兜底结果。
- Reasoner 本身不操作数据库，但具体工具可以通过 MemoryEngine 间接读写数据库。
- Reasoner 会直接通过 AsyncOpenAI 客户端调用 DeepSeek 网络 API。

---

## 四十七、重新聚焦：`run_turn()` 就是一台异步状态机

前面的章节已经拆解了所有函数。把它们重新按 `run_turn()` 的真实执行顺序组合起来，可以把这个函数理解成一台状态机：

```text
START
  │
  ▼
复制 ctx.messages，创建 tool_calls=[]
  │
  ▼
┌──────────── iteration = 0、1、2、3 ────────────┐
│                                                │
│  BEFORE_STEP                                   │
│  ├── PhaseModuleRunner                         │
│  ├── EventBus GATE                             │
│  ├── early_stop? ──是──→ RETURN                │
│  └── extra_hints → 追加 role=system            │
│           │                                    │
│           ▼                                    │
│  CALL_LLM                                      │
│  chat.completions.create(messages, tools)      │
│           │                                    │
│           ▼                                    │
│  DECIDE                                        │
│  message.tool_calls 是否为空？                  │
│       │                                        │
│       ├── 非空：TOOL_BRANCH                     │
│       │     ├──保存 assistant tool_calls       │
│       │     ├──逐个调用 ToolRuntime            │
│       │     ├──追加 role=tool                  │
│       │     ├──执行 Search/Fetch Guard         │
│       │     ├──AfterStep(has_more=True)        │
│       │     └──continue                        │
│       │                                        │
│       └── 空：TEXT_BRANCH                       │
│             ├──执行 Recall/Search/Fetch Guard │
│             ├──Guard 调了工具？                │
│             │     ├──是：AfterStep + continue │
│             │     └──否：最终答案分支          │
│             ├──AfterStep(has_more=False)       │
│             ├──Final Answer Guard              │
│             └──RETURN ReasonerResult           │
└────────────────────────────────────────────────┘
  │
  ▼
四轮仍未完成：RETURN max_iterations
```

这里的“状态”没有定义成 Enum，而是由这些变量共同表达：

| 状态变量 | 含义 |
|---|---|
| `iteration` | 当前第几个 LLM Step |
| `messages` | 截至当前模型已经能看到的完整对话和工具结果 |
| `tool_calls` | 本轮累计执行过的所有工具记录 |
| `message.tool_calls` | 本次 LLM 是否主动请求工具 |
| `guard_tools` | 本次是否由代码 Guard 强制补充工具 |
| `has_more` | 本次 Step 后是否还要继续调用 LLM |

### 与总 Pipeline 的衔接

```text
BeforeReasoningPhase.build_ctx()
        │
        │ BeforeReasoningCtx
        │ ├── messages
        │ ├── tools
        │ ├── memories
        │ └── prompt_sections
        ▼
Reasoner.run_turn(ctx)
        │
        │ ReasonerResult
        │ ├── content
        │ ├── tool_calls
        │ └── finish_reason
        ▼
AfterReasoningPhase.build_ctx()
        │
        ▼
OutboundMessage
```

所以 `run_turn()` 只负责从“模型输入”推进到“模型最终结果”，不负责组装主 Prompt，也不负责发送 Telegram。

---

## 四十八、`run_turn()` 的等价伪代码

去掉项目特定 Guard 和插件细节后，核心结构可以缩写为：

```python
async def run_turn(ctx):
    messages = ctx.messages.copy()
    all_tool_calls = []

    for iteration in range(4):
        step = await before_step(ctx, iteration, messages)

        if step.early_stop:
            return ReasonerResult(
                content=step.early_stop_reply,
                tool_calls=all_tool_calls,
                finish_reason="early_stop",
            )

        if step.extra_hints:
            messages.append({
                "role": "system",
                "content": format_hints(step.extra_hints),
            })

        response = await llm(
            model=self.model,
            messages=messages,
            tools=ctx.tools,
        )

        message = response.choices[0].message

        if message.tool_calls:
            messages.append(
                assistant_tool_call_message(message)
            )

            for call in message.tool_calls:
                result = await execute_tool(call)
                all_tool_calls.append(
                    tool_call_record(call, result)
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result,
                })

            await after_step(has_more=True)
            continue

        guard_tools = await run_memory_guards(...)
        if guard_tools:
            await after_step(has_more=True)
            continue

        await after_step(has_more=False)
        return ReasonerResult(
            content=final_answer_guard(message.content),
            tool_calls=all_tool_calls,
            finish_reason=response.choices[0].finish_reason,
        )

    return ReasonerResult(
        content="抱歉，处理请求时遇到问题。",
        tool_calls=all_tool_calls,
        finish_reason="max_iterations",
    )
```

项目真实代码比它多出的部分，主要是：

- BeforeStep/AfterStep 插件 slot；
- EventBus GATE/TAP；
- 工具批次快照；
- ToolRuntime 结果注解；
- Recall、Raw Search、Evidence Fetch 三个确定性 Guard；
- Final Answer Guard。

---

## 四十九、一次 Function Call 的变量变化全过程

假设当前用户问：

```text
我现在喜欢喝什么？
```

### 49.1 进入 `run_turn()`

`BeforeReasoningPhase` 已经准备：

```python
ctx.messages = [
    {
        "role": "system",
        "content": "你是一个友好的 AI 助手……",
    },
    {
        "role": "user",
        "content": "我以前喜欢喝咖啡。",
    },
    {
        "role": "assistant",
        "content": "记住了。",
    },
    {
        "role": "user",
        "content": "我现在喜欢喝什么？",
    },
]

ctx.tools = [
    {"type": "function", "function": {"name": "recall_memory", ...}},
    {"type": "function", "function": {"name": "search_messages", ...}},
    {"type": "function", "function": {"name": "fetch_messages", ...}},
]
```

函数初始化：

```python
messages = ctx.messages.copy()
tool_calls = []
```

此时：

```text
messages 长度 = 4
tool_calls 长度 = 0
iteration = 0
```

### 49.2 Step 0：BeforeStep

创建：

```python
BeforeStepCtx(
    session_key="1001:2002",
    channel="telegram",
    chat_id="2002",
    iteration=0,
    input_tokens_estimate=120,
    visible_tool_names=frozenset({
        "recall_memory",
        "search_messages",
        "fetch_messages",
    }),
)
```

如果没有插件阻断，也没有 Step Hint，就继续调用 LLM。

### 49.3 第一次 LLM 返回 Function Call

```python
message.content
# None

message.tool_calls
# [
#   ToolCall(
#       id="call_001",
#       function.name="recall_memory",
#       function.arguments=(
#           '{"query":"用户当前饮品偏好",'
#           '"memory_type":"preference",'
#           '"include_superseded":true}'
#       ),
#   )
# ]
```

真正决定走工具分支的是：

```python
if message.tool_calls:
```

而不是只依赖：

```python
choice.finish_reason == "tool_calls"
```

### 49.4 保存 Assistant 的工具请求

累计调用记录变成：

```python
tool_calls = [
    {
        "id": "call_001",
        "type": "function",
        "function": {
            "name": "recall_memory",
            "arguments": (
                '{"query":"用户当前饮品偏好",'
                '"memory_type":"preference",'
                '"include_superseded":true}'
            ),
        },
    }
]
```

同时向 LLM 消息协议追加：

```python
messages.append({
    "role": "assistant",
    "content": "",
    "tool_calls": [
        {
            "id": "call_001",
            "type": "function",
            "function": {
                "name": "recall_memory",
                "arguments": "...",
            },
        }
    ],
})
```

这里必须先保留 Assistant 的请求，后面的 `role="tool"` 才能通过 `tool_call_id` 与它配对。

### 49.5 ToolRuntime 执行工具

```python
result = await self._execute_tool(
    "recall_memory",
    '{"query":"用户当前饮品偏好",...}',
    ctx,
    call_id="call_001",
)
```

执行链：

```text
BeforeToolCallCtx TAP
        ↓
ToolRuntime.parse_arguments
        ↓
ToolRegistry.get_tool
        ↓
输入 JSON Schema 校验
        ↓
ToolExecutor pre hooks / policy
        ↓
Tool.execute
        ↓
ToolExecutor post hooks
        ↓
输出 Schema 校验与 normalize_tool_output
        ↓
ToolRuntimeResult.to_json
        ↓
AfterToolResultCtx TAP
```

工具结果可能是：

```json
{
  "ok": true,
  "status": "success",
  "data": {
    "items": [
      {
        "summary": "用户现在更喜欢喝茶",
        "status": "active",
        "source_ref": "session:1001:2002#msg:18"
      },
      {
        "summary": "用户以前喜欢咖啡",
        "status": "superseded",
        "source_ref": "session:1001:2002#msg:3"
      }
    ]
  },
  "error": null,
  "meta": {
    "tool_name": "recall_memory",
    "call_id": "call_001",
    "retry_count": 0
  }
}
```

### 49.6 把工具结果放回消息链

```python
messages.append({
    "role": "tool",
    "tool_call_id": "call_001",
    "content": result,
})
```

此时消息链尾部是：

```text
assistant(tool_calls=[call_001])
        ↓ 对应
tool(tool_call_id=call_001, content=Runtime JSON)
```

同时 `tool_calls[0]` 被补充：

```python
{
    ...,
    "result": result,
    "status": "success",
    "final_arguments": {...},
    "retry_count": 0,
}
```

### 49.7 Evidence Fetch Guard 自动回源

因为 `recall_memory` 结果含有尚未读取的 `source_ref`，代码自动执行：

```python
fetch_messages({
    "source_refs": [
        "session:1001:2002#msg:18",
        "session:1001:2002#msg:3",
    ],
    "context": 2,
    "limit": 20,
})
```

它也会向 `messages` 追加标准三件套：

```text
assistant(tool_calls=[guard_fetch_2])
tool(tool_call_id=guard_fetch_2, content=原文结果)
system("# Evidence Guard ...")
```

累计工具记录：

```python
tool_calls = [
    {"function": {"name": "recall_memory"}, ...},
    {
        "function": {"name": "fetch_messages"},
        "guard": "source_ref_requires_fetch",
        ...,
    },
]
```

然后执行：

```python
await self._run_after_step(
    iteration=0,
    tools_called=("recall_memory", "fetch_messages"),
    partial_reply="",
    has_more=True,
)
```

最后：

```python
continue
```

进入 `iteration=1`。

### 49.8 Step 1：模型返回最终文字

第二次调用 LLM 时，它能看到：

```text
原 System Prompt
Session 历史
当前用户问题
recall_memory 请求与结果
fetch_messages 请求与原文结果
Evidence Guard 规则
```

模型返回：

```python
message.content
# "你现在更喜欢喝茶；咖啡是你以前的偏好。"

message.tool_calls
# None

choice.finish_reason
# "stop"
```

代码进入文字分支。三个 Guard 都不再需要执行，于是：

```python
await self._run_after_step(
    iteration=1,
    tools_called=(),
    partial_reply="你现在更喜欢喝茶；咖啡是你以前的偏好。",
    has_more=False,
)
```

最后返回：

```python
ReasonerResult(
    content="你现在更喜欢喝茶；咖啡是你以前的偏好。",
    tool_calls=[
        {"function": {"name": "recall_memory"}, ...},
        {
            "function": {"name": "fetch_messages"},
            "guard": "source_ref_requires_fetch",
            ...,
        },
    ],
    finish_reason="stop",
)
```

---

## 五十、怎样判断这次是文字回复还是 Function Call

模型的一次返回对象中，两类信息都挂在 `choice.message` 上：

```python
choice = response.choices[0]
message = choice.message
```

### 模型请求工具

```python
if message.tool_calls:
```

例如：

```python
message.content = None
message.tool_calls = [ToolCall(...)]
```

项目不会把这个 `content` 当最终用户回复，而是执行工具并 `continue`。

### 模型返回文字

```python
else:
```

例如：

```python
message.content = "这是最终回答"
message.tool_calls = None
```

但是这时仍不一定立即返回，因为代码还会运行三个 Guard。只有满足：

```text
message.tool_calls 为空
        AND
Recall/Search/Fetch Guard 都没有补调用工具
```

才把 `message.content` 当成最终候选回答。

所以项目的准确判断逻辑是：

```text
message.tool_calls 非空
    → 模型主动 Function Call

message.tool_calls 为空，但 guard_tools 非空
    → 模型想输出文字，代码强制补 Function Call

message.tool_calls 为空，guard_tools 也为空
    → 最终文字回答
```

`choice.finish_reason` 只是最终写入 `ReasonerResult.finish_reason` 的辅助信息，不是代码选择工具分支的主要依据。

---

## 五十一、`run_turn()` 各出口汇总

| 出口 | 触发条件 | `finish_reason` | `content` |
|---|---|---|---|
| BeforeStep 提前终止 | `step_ctx.early_stop=True` | `early_stop` | `early_stop_reply` |
| 正常文字回答 | 无 tool_calls，且 Guard 不补工具 | API 的 finish_reason，通常 `stop` | 模型文字经过 Final Answer Guard |
| 最大轮数耗尽 | 四个 iteration 都继续 | `max_iterations` | `抱歉，处理请求时遇到问题。` |
| LLM API 异常 | iteration 0 失败会重试；后续失败 | 不返回 ReasonerResult | 异常向上抛出 |

工具执行出错通常不会直接让 `run_turn()` 抛异常。ToolRuntime 会把错误包装成 JSON 信封作为 `role="tool"` 返回给模型，例如：

```json
{
  "ok": false,
  "status": "error",
  "data": null,
  "error": {
    "code": "input_validation",
    "message": "缺少参数 query",
    "retryable": true
  }
}
```

模型下一轮可以看到错误并尝试修正参数。

---

## 五十二、这一段 Pipeline 最终要记住什么

```text
BeforeReasoningCtx.messages
        │
        ▼
run_turn 复制为局部 messages
        │
        ▼
每轮 BeforeStep
        │
        ▼
调用 LLM(messages, tools)
        │
        ├── tool_calls：执行工具、回填 role=tool、继续
        │
        └── content：检查 Guard，无缺口才结束
        ▼
ReasonerResult
        │
        ▼
AfterReasoningPhase
```

三个最关键的变量：

```python
messages
# 模型在下一轮能看到的全部上下文，随着工具调用持续增长

tool_calls
# 本轮实际执行工具的审计记录，包含模型主动调用和代码 Guard 调用

message
# 当前这一轮 LLM 返回的 Assistant 消息，用 tool_calls 决定分支
```

数据库边界：Reasoner 自己不执行 SQL；它只通过 ToolRuntime 调用具体工具。具体工具可能继续进入 MemoryEngine、MemoryStore 或 SessionStore，间接读写 `memory_items`、`vec_items` 和 `conversation_sessions`，详见 [[learning_tool_system]]、[[learning_default_memory_engine]] 与 [[learning_table]]。
