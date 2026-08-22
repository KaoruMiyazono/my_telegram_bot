# My Telegram Bot

一个支持工具调用、插件生命周期、Session 原文检索和向量长期记忆的 Telegram AI Agent。

## 功能特性

- 💾 **持久化记忆**：使用向量数据库存储和检索对话
- 🧠 **智能检索**：基于语义相似度和关键词混合检索
- 🔄 **对话管理**：支持多轮对话，记忆会话历史
- 🤖 **工具调用**：通过 ToolRegistry / ToolExecutor 统一调度内置记忆工具和插件工具
- 🌐 **联网检索**：内置 `web_search` 和安全版 `web_fetch`，支持时效性搜索、来源核实和 URL 引用
- 🧩 **插件生命周期**：支持 Akashic 风格 PhaseModule、slot export、prompt_render 插入点

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
├── channels/           # 消息通道
│   └── telegram/        # Telegram 集成
├── memory/             # 记忆管理
│   ├── embedder.py      # 向量生成
│   ├── hyde_enhancer.py # HyDE 检索增强
│   └── store.py         # 记忆存储和检索
├── persistence/         # 数据持久化
│   ├── database.py      # SQLite + sqlite-vec
│   └── session_store.py # 原始消息与会话游标
├── proactive_v2/        # 主动推送链路 scaffold
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
