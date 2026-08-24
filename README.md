# My Telegram Bot

一个支持工具调用、插件生命周期、Session 原文检索和向量长期记忆的 Telegram AI Agent。

## 功能特性

- 💾 **持久化记忆**：使用向量数据库存储和检索对话
- 🧠 **智能检索**：基于语义相似度和关键词混合检索
- 🔄 **对话管理**：支持多轮对话，记忆会话历史
- 🤖 **工具调用**：通过 ToolRegistry / ToolExecutor 统一调度内置记忆工具和插件工具
- 🌐 **联网检索**：内置 `web_search` 和安全版 `web_fetch`，支持时效性搜索、来源核实和 URL 引用
- 🧩 **插件生命周期**：支持 Akashic 风格 PhaseModule、slot export、prompt_render 插入点
- 📡 **主动 Agent**：五阶段主动链读取用户长期兴趣，支持三类 MCP Source、个性化内容判断、自适应调度、五层去重和可恢复精确 ACK
- 💤 **三模式协调**：Passive > Proactive > Idle；用户消息可暂停并恢复持久化后台维护任务
- 📱 **四类终端**：Telegram、CLI、WebSocket、HTTP/SSE 共用 TurnRuntime，支持有序事件、取消、ACK 与断线续传

## 技术栈

- **语言**：Python 3.11+
- **LLM**：DeepSeek API
- **向量**：阿里云 DashScope
- **数据库**：SQLite + sqlite-vec
- **Bot 框架**：python-telegram-bot 20.7+
- **依赖管理**：Poetry

## 快速开始

### 本地运行

1. 安装依赖：
   ```bash
   poetry install
   ```

2. 配置环境变量（复制 `.env.example` 为 `.env` 并填入密钥）

3. 启动：
   ```bash
   poetry run python main.py
   ```

### 开发检查

安装开发工具并运行统一质量入口：

```bash
poetry run pip install -r requirements-dev.txt
poetry run pytest
poetry run pyright
poetry run python eval/rag_layer_smoke.py
```

默认 pytest 包含离线单元测试、Pipeline 集成测试和五类 Golden Trace，不会运行需要真实 API 的手工 E2E 脚本。M0 的启动依赖与数据合同见 `docs/architecture/main_startup_dependency.md` 和 `docs/contracts/core_models.md`。

生产 Telegram 消息现在会先进入持久化 `MessageBus`，再按
`channel:chat_id:user_id` 进入 Session Lane。同一个 Session 严格串行，不同
Session 并发执行；`/stop` 可以取消当前 LLM/工具任务，新消息也可以抢占旧的
长任务。消息状态记录在 `runtime_messages`，Telegram `update_id` 作为幂等键，
避免进程重启后重复消费已经提交的消息。

如果当前网络不能直接访问 Telegram，可在 `.env` 中配置 HTTP 或 SOCKS 代理：

```dotenv
HTTP_PROXY=socks5://127.0.0.1:7897
```

联网搜索默认使用 Exa 的公开 MCP HTTP 端点，无需密钥即可试用。生产环境可配置 `SEARCH_API_KEY` 提高额度；联网工具代理与 Telegram 代理分开设置：

```dotenv
SEARCH_API_KEY=
WEB_PROXY=socks5://127.0.0.1:7897
WEB_SEARCH_MAX_RESULTS=5
WEB_FETCH_TIMEOUT=20
WEB_FETCH_MAX_CHARS=15000
```

`web_fetch` 只允许访问公开 HTTP/HTTPS 地址，会阻止本机、内网、保留地址、云元数据地址及指向这些地址的重定向。

主动 Agent 默认关闭。M9 使用 `config/proactive_sources.toml` 声明
alert/content/context Source；Source 可独立动态启停，一个 MCP Server 可以提供
多个 Source。建议先使用 Shadow Mode：

```dotenv
PROACTIVE_ENABLED=true
PROACTIVE_MODE=shadow
PROACTIVE_CHAT_ID=
PROACTIVE_USER_ID=
PROACTIVE_SOURCE_CONFIG_PATH=./config/proactive_sources.toml
```

本地 `proactive_demo` Server 和三个示例 Source 默认均为 disabled，不会自动发送测试数据。

M10 会按 `PROACTIVE_USER_ID` 读取该用户的 active
`preference/profile/procedure` 长期记忆。明确负向偏好优先于 Provider 热度；没有兴趣记忆时进入严格冷启动，只允许标记为 `interesting` 且达到
`PROACTIVE_COLD_START_THRESHOLD` 的高置信内容。模糊内容默认安全跳过；需要 LLM 辅助判断时显式开启：

```dotenv
PROACTIVE_LLM_JUDGE_ENABLED=true
PROACTIVE_COLD_START_THRESHOLD=0.9
```

主动检查间隔会根据空 Tick、Source 错误、告警新鲜度和被动会话忙碌状态自动调整。Telegram 发送成功后写入 ACK Outbox；独立 ACK Worker 按指数退避重试，超过最大次数进入 `dead`，不会重新发送 Telegram 消息。

M11 使用 `ModeCoordinator` 统一协调三种工作模式。Passive 用户消息优先级最高，
会暂停正在运行的 Idle Task，并阻止同一 Session 的普通 Proactive 投递；本轮回复真正
发送完成后，系统恢复可恢复的 Idle Task，并唤醒主动调度器重新计算下一次 Tick。
Idle Task 状态和断点保存在 `idle_tasks` 表中，进程重启后 `running` 会恢复为
`paused`。第一版只允许 `local_read` 和 `local_maintenance` 权限，不能借 Idle
模式发送消息或修改外部服务。

```dotenv
IDLE_TASKS_ENABLED=true
IDLE_TASK_POLL_SECONDS=5
```

M12 将四类终端统一到同一个 `TurnRuntime`。终端只做输入标准化、身份映射、
事件渲染、最终投递和客户端 ACK；Agent Pipeline 不依赖 Telegram SDK。统一事件包括：

```text
turn.started → assistant.delta / tool.* → turn.completed | turn.cancelled
```

事件先持久化到 `runtime_stream_events`，再非阻塞广播。客户端可使用 `seq` 和
`runtime_stream_acks` 断线续传，也可以通过 `/v1/result/{turn_id}` 直接获取最终结果。
当前 LLM 调用仍是非流式接口，因此 `assistant.delta` 是最终答案的分块投影；工具开始和
结束事件则会在执行时实时产生。

启用本地 HTTP/SSE + WebSocket Gateway：

```dotenv
CHANNEL_WEB_ENABLED=true
CHANNEL_WEB_HOST=127.0.0.1
CHANNEL_WEB_PORT=8080
CHANNEL_API_TOKEN=replace-with-a-secret
```

主要接口：

```text
POST /v1/chat
POST /v1/chat/cancel
GET  /v1/events?session_key=...&after_seq=0
POST /v1/events/ack
GET  /v1/result/{turn_id}
WS   /v1/ws
```

启用同进程 CLI：

```dotenv
CHANNEL_CLI_ENABLED=true
CHANNEL_CLI_ACCOUNT_ID=local
```

Session Key 使用 `<channel>:<account_id>:<chat_id>:<thread_id>`。不同终端默认分配
不同用户身份和独立 Session；只有通过受保护的 `/v1/identities/bind` 显式绑定后才共享
长期记忆，即使绑定后，各终端的当前对话原文仍然隔离。非回环地址启动 Web Gateway 时
必须配置 `CHANNEL_API_TOKEN`。

请勿提交 `.env`、数据库、运行日志或用户对话数据；这些内容已由 `.gitignore` 排除。

### Docker 部署

```bash
docker-compose up -d
```

详细指南见 [DOCKER.md](DOCKER.md)

## 项目结构

```
telegram-bot-mvp/
├── agent/               # Agent 核心逻辑
│   ├── core/            # 类型定义、EventBus、PromptBlock
│   ├── lifecycle/       # PhaseFrame / PhaseModule / slot export
│   ├── pipeline/        # 被动回复流水线与各阶段
│   ├── plugins/         # 插件管理器、上下文、装饰器
│   ├── prompting/       # Prompt 渲染与 section 组装
│   ├── tool_hooks/      # 工具调用前置 hook 链
│   └── tools/           # ToolRegistry、ToolExecutor、记忆与 Web 内置工具
├── channels/           # 统一 Adapter、Telegram、CLI、WebSocket、HTTP/SSE
├── memory/             # 记忆管理
│   ├── embedder.py      # 向量生成
│   ├── hyde_enhancer.py # HyDE 检索增强
│   └── store.py         # 记忆存储和检索
├── persistence/         # 数据持久化
│   ├── database.py      # SQLite + sqlite-vec
│   └── session_store.py # 原始消息与会话游标
├── proactive_v2/        # 五阶段主动Agent、MCP Source、去重、投递与ACK
├── config/             # 配置管理
│   └── settings.py      # Pydantic 设置
├── tests/              # 单元测试
├── main.py             # 入口文件
├── Dockerfile          # Docker 镜像构建
└── docker-compose.yml  # Docker 编排
```

## 获取 API 密钥

| 服务 | 地址 |
|------|------|
| DeepSeek | https://platform.deepseek.com/ |
| 阿里云 DashScope | https://dashscope.aliyuncs.com/ |

## 许可证

本项目采用 [MIT License](LICENSE)。

## 致谢

本项目从 [yusaRe/telegram-bot](https://github.com/yusaRe/telegram-bot) 的早期实现演进而来，现作为独立仓库继续开发和维护。
