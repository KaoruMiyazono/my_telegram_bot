# Development Guide

本文保存项目内部的开发、验证、架构和高级配置说明。README 只作为面向使用者的项目首页。

## 开发环境与质量检查

```bash
poetry install
poetry run pip install -r requirements-dev.txt
poetry run pytest
poetry run pyright
poetry run python eval/rag_layer_smoke.py
```

pytest 默认包含离线单元测试、Pipeline 集成测试、Golden Trace、并发和故障恢复测试，不运行依赖真实外部服务的 Nightly 场景。

测试归档的生成、验证和下载格式见 [docs/testing/M13_TEST_ARCHIVE.md](docs/testing/M13_TEST_ARCHIVE.md)。启动依赖与核心数据合同见：

- [docs/architecture/main_startup_dependency.md](docs/architecture/main_startup_dependency.md)
- [docs/contracts/core_models.md](docs/contracts/core_models.md)
- [docs/architecture/m1_async_runtime.md](docs/architecture/m1_async_runtime.md)

## 异步运行时

生产消息先进入持久化 MessageBus，再进入 Session Lane。同一 Session 严格串行，不同 Session 并发执行；`/stop` 可以取消当前 LLM/工具任务，新消息也可以抢占旧的长任务。

消息状态记录在 `runtime_messages`。Telegram `update_id` 作为幂等键，避免进程重启后重复消费已经提交的消息。

Session Key 使用：

```text
<channel>:<account_id>:<chat_id>:<thread_id>
```

不同终端默认拥有独立身份和 Session。只有经过 `/v1/identities/bind` 显式绑定后才共享长期记忆；当前对话原文仍按终端隔离。

## 联网工具

联网搜索可使用 Exa MCP；生产环境可以配置自己的搜索凭证和独立代理：

```dotenv
SEARCH_API_KEY=
WEB_PROXY=socks5://127.0.0.1:7897
WEB_SEARCH_MAX_RESULTS=5
WEB_FETCH_TIMEOUT=20
WEB_FETCH_MAX_CHARS=15000
```

`web_fetch` 只允许公开 HTTP/HTTPS 地址，会阻止本机、内网、保留地址、云元数据地址及指向它们的重定向。

## 主动 Agent

主动 Agent 默认关闭。Source 由 `config/proactive_sources.toml` 声明，每个 alert/content/context Source 可以独立启停，一个 MCP Server 可以提供多个 Source。

```dotenv
PROACTIVE_ENABLED=true
PROACTIVE_MODE=shadow
PROACTIVE_CHAT_ID=
PROACTIVE_USER_ID=
PROACTIVE_SOURCE_CONFIG_PATH=./config/proactive_sources.toml
PROACTIVE_LLM_JUDGE_ENABLED=false
PROACTIVE_COLD_START_THRESHOLD=0.9
```

主动决策读取目标用户 active 的 `preference/profile/procedure` 长期记忆。明确负向偏好优先于 Provider 热度；没有兴趣记忆时进入严格冷启动。发送成功后写入 ACK Outbox，独立 Worker 只重试 Provider ACK，不重复发送 Telegram。

## 三模式协调

优先级为：

```text
Passive > Proactive > Idle
```

用户消息会暂停可恢复的 Idle Task，并阻止同一 Session 的普通主动投递。回复真正投递完成后再恢复 Idle Task，并唤醒主动调度器。

```dotenv
IDLE_TASKS_ENABLED=true
IDLE_TASK_POLL_SECONDS=5
```

Idle Task 第一版仅允许 `local_read` 和 `local_maintenance`，不能发送消息或修改外部服务。

## 多终端与流式事件

四类终端共用 TurnRuntime，统一事件协议为：

```text
turn.started → assistant.delta / tool.* → turn.completed | turn.cancelled
```

事件先持久化到 `runtime_stream_events`，再广播给客户端。客户端可通过 `seq` 和 `runtime_stream_acks` 断线续传，也可以读取 `/v1/result/{turn_id}`。

当前 LLM Provider 仍使用非流式接口，因此 `assistant.delta` 是最终回答的分块投影；工具开始和结束事件是实时事件。

```dotenv
CHANNEL_WEB_ENABLED=true
CHANNEL_WEB_HOST=127.0.0.1
CHANNEL_WEB_PORT=8080
CHANNEL_API_TOKEN=replace-with-a-secret
CHANNEL_CLI_ENABLED=false
CHANNEL_CLI_ACCOUNT_ID=local
```

Web Gateway 接口：

```text
POST /v1/chat
POST /v1/chat/cancel
GET  /v1/events?session_key=...&after_seq=0
POST /v1/events/ack
GET  /v1/result/{turn_id}
WS   /v1/ws
```

非回环地址启动 Gateway 时必须配置 `CHANNEL_API_TOKEN`。

## 真实长期记忆评测

真实基准必须走被动 Pipeline，不使用 mock：

```bash
python eval/replay_runner.py \
  --set green \
  --fresh \
  --trace \
  --limit 3 \
  --output data/evaluation/results/replay_live_smoke.json
```

执行 `--fresh` 会清理本次评测数据库；不要指向生产数据。

## 数据安全

以下内容不能提交：

- `.env` 与 API Token；
- SQLite 数据库和 WAL 文件；
- 用户消息、Conversation Log 和评测运行产物；
- PID、临时文件与本地代理配置。
