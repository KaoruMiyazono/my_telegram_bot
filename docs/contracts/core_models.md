# M0 核心运行合同

生产 Pipeline 的唯一核心合同定义在 `agent/core/types.py`。`agent/core/message_bus.py`、`agent/core/session.py` 和 `agent/core/events.py` 中仍存在早期 scaffold 类型，但不能用于当前被动 Pipeline；它们将在 M1 MessageBus 改造时迁移或删除。

## 三个统一标识

| 标识 | 生命周期 | 格式示例 | 用途 |
|---|---|---|---|
| `session_key` | 同一渠道、聊天和用户共享 | `telegram:1001:42` | 会话隔离与排队 |
| `turn_id` | 每条用户请求生成一次 | `turn:<uuid>` | 关联输入、输出、工具和提交事件 |
| `trace_id` | 一次内部执行链 | `trace:<uuid>` | 串联阶段日志与故障定位 |

`session_key` 的固定顺序为：

```text
channel:chat_id:user_id
```

测试/重放可以传入已有 `turn_id` 和 `trace_id`，真实请求缺省时自动生成。

## 六个核心模型

### `InboundMessage`

Channel 进入 Agent 的标准输入。必需字段为 `user_id`、`chat_id`、`content`；同时携带 `channel`、`turn_id`、`trace_id` 和非敏感 metadata。

### `OutboundMessage`

Agent 发往 Channel 的标准输出。包含目标 `chat_id`、正文、格式和同一轮的 `turn_id/trace_id`。

### `Session`

保存某个 `session_key` 下的原始消息历史与 consolidation 游标。Session 跨多个 Turn，不能用 `turn_id` 代替。

### `ReasonerResult`

Reasoner 的最终结果，包含回复正文、完整工具调用链、结束原因和同一轮标识。

### `ToolRuntimeResult`

单次工具执行的统一信封，包含 success/error、标准化数据、重试、耗时、最终参数和 `turn_id/trace_id`。错误不能通过抛出任意字符串跨越 Tool Runtime 边界。

### `TurnCommittedEvent`

AfterTurn 形成的提交事件，表示本轮输入、输出和新记忆已经形成。事件使用 Pipeline 入口生成的 `turn_id`，禁止在 AfterTurn 临时重新生成。

## Trace 安全边界

`TurnTrace` 可以记录：

- 五阶段名称；
- 可见工具名与实际调用工具名；
- 上下文 token 粗略估计；
- 检索模式和召回数量；
- finish reason、状态、错误类型和耗时。

它不能记录：

- Telegram Token、API Key 和 Authorization Header；
- 完整用户消息；
- 完整 Prompt；
- 工具参数正文或工具返回正文；
- 完整网页内容。

Golden Trace 会进一步把随机 ID 和耗时归一化，保证可以稳定进入 Git。
