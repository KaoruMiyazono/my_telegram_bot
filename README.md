# My Telegram Bot

一个支持长期记忆、工具调用、联网检索、插件扩展和主动推送的 Telegram AI Agent。

## 核心能力

- **长期记忆**：Session 原文、可读 Markdown 记忆和向量记忆协同工作。
- **智能检索**：融合语义搜索、关键词搜索、HyDE 与原始证据回溯。
- **工具系统**：通过统一 ToolRegistry / ToolExecutor 调度内置、插件和 MCP 工具。
- **联网能力**：内置安全的 `web_search` 与 `web_fetch`。
- **MCP Runtime**：支持 MCP Server 动态注册、卸载和远端工具接入。
- **生命周期插件**：七阶段 Pipeline、Slot 依赖和 EventBus 扩展点。
- **主动 Agent**：结合用户兴趣完成内容判断、调度、去重、推送和 ACK。
- **多模式协调**：统一被动问答、主动推送与空闲任务，用户请求优先。
- **多终端接入**：Telegram、CLI、WebSocket、HTTP/SSE 共用 TurnRuntime。
- **可靠运行**：Session 隔离、用户中断、持久化消息、断线续传和五级上下文降级。

## 架构概览

```text
Telegram / CLI / WebSocket / HTTP
                 ↓
      MessageBus + Session Lane
                 ↓
            TurnRuntime
                 ↓
BeforeTurn → BeforeReasoning → Reasoner → AfterReasoning → AfterTurn
                 ↓
 Tool Runtime / MCP / Memory / Proactive Runtime
```

## 技术栈

- Python 3.11+
- Poetry
- python-telegram-bot
- OpenAI 兼容 LLM API
- DashScope Embedding
- SQLite + sqlite-vec
- FastAPI / WebSocket / SSE

## 快速开始

1. 安装依赖：

   ```bash
   poetry install
   ```

2. 复制配置模板并填写 Bot、LLM 与 Embedding 凭证：

   ```bash
   cp .env.example .env
   ```

3. 启动：

   ```bash
   poetry run python main.py
   ```

如果当前网络不能直接访问 Telegram，可以配置代理：

```dotenv
HTTP_PROXY=socks5://127.0.0.1:7897
```

## 可选能力

启用主动 Agent 的 Shadow Mode：

```dotenv
PROACTIVE_ENABLED=true
PROACTIVE_MODE=shadow
PROACTIVE_CHAT_ID=
PROACTIVE_USER_ID=
```

启用 HTTP/SSE 与 WebSocket Gateway：

```dotenv
CHANNEL_WEB_ENABLED=true
CHANNEL_WEB_HOST=127.0.0.1
CHANNEL_WEB_PORT=8080
CHANNEL_API_TOKEN=replace-with-a-secret
```

启用同进程 CLI：

```dotenv
CHANNEL_CLI_ENABLED=true
CHANNEL_CLI_ACCOUNT_ID=local
```

更多环境变量、架构细节、测试方式和质量归档说明见 [DEVELOPMENT.md](DEVELOPMENT.md)。

## Docker 部署

```bash
docker-compose up -d
```

详细指南见 [DOCKER.md](DOCKER.md)。

## 项目结构

```text
agent/          Agent 核心、Pipeline、Runtime、工具和插件
channels/       Telegram、CLI、WebSocket、HTTP/SSE Adapter
memory/         可读记忆、向量记忆、检索与优化
persistence/    SQLite、Session、Message 与 Stream 持久化
proactive_v2/   主动决策、MCP Source、调度、去重与 ACK
config/         应用配置、MCP 与质量场景目录
evaluation/     指标、数据集和测试归档
eval/           长期记忆评测与 RAG smoke
tests/          自动化测试
main.py         应用入口
```

请勿提交 `.env`、数据库、运行日志或用户对话数据；这些内容已由 `.gitignore` 排除。

## 许可证

本项目采用 [MIT License](LICENSE)。

## 致谢

本项目从 [yusaRe/telegram-bot](https://github.com/yusaRe/telegram-bot) 的早期实现演进而来，现作为独立仓库继续开发和维护。
