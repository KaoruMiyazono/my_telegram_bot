import asyncio
import logging
from pathlib import Path

from agent.core.event_bus import EventBus
from agent.core.message_bus import MessageBus
from agent.core.ids import build_session_key
from agent.core.types import OutboundMessage
from agent.mcp import McpManager, register_mcp_management_tools
from agent.pipeline.passive_turn import PassiveTurnPipeline
from agent.pipeline.phases.after_reasoning import AfterReasoningPhase
from agent.pipeline.phases.after_turn import AfterTurnPhase
from agent.pipeline.phases.before_reasoning import BeforeReasoningPhase
from agent.pipeline.phases.before_turn import BeforeTurnPhase
from agent.pipeline.reasoner import Reasoner
from agent.plugins import PluginManager
from agent.tool_hooks import ToolExecutor
from agent.tools import ToolRegistry, register_tool_search
from agent.tools.memory import register_memory_tools
from agent.tools.web import register_web_tools
from channels.telegram.adapter import TelegramAdapter
from config.settings import settings
from evaluation.conversation_logger import ConversationLogger
from memory.embedder import Embedder
from memory.bootstrap import build_memory_runtime
from memory.store import MemoryStore
from persistence.database import init_db
from persistence.session_store import get_session_store
from persistence.runtime_message_store import RuntimeMessageStore
from agent.runtime.turn_runtime import TurnRuntime
from agent.runtime.idle_tasks import IdleTaskResult, IdleTaskRuntime, IdleTaskStore
from agent.runtime.mode_coordinator import ModeCoordinator
from agent.runtime.stream_events import StreamEventBroker, StreamEventStore
from channels.base import ChannelIdentityStore
from channels.cli_adapter import CliAdapter
from channels.web_gateway import WebGateway, WebGatewayServer
from agent.tools.message_push import MessagePushTool
from proactive_v2.agent_tick import AgentTick
from proactive_v2.contracts import ProactivePolicy
from proactive_v2.loop import ProactiveLoop
from proactive_v2.interests import MemoryInterestReader, OpenAIInterestJudge
from proactive_v2.mcp_sources import (
    McpManagerSourceCaller,
    McpProactiveGateway,
    ProactiveSourceRegistry,
    register_proactive_source_management_tools,
)
from proactive_v2.state import ProactiveStateStore

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
#  调整 httpx 和 httpcore 的日志级别为 WARNING，避免过多的调试信息干扰日志输出
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def main() -> None:
    """Main entry point."""
    logger.info("Bot starting...")

    # 1. Initialize database 主要是利用sqlite这个库创建表格，以及打开sqlvec的权限
    logger.info("Initializing database...")
    init_db()

    # 2. Initialize core components
    embedder = Embedder()
    #  初始化memory_store类，其实就是一个sqlite数据库的封装类，主要是用来存储记忆的。它使用了embedder来对记忆进行向量化处理，以便后续的检索和相似度计算。 包括检索，插入，删除，更新等操作。它还提供了一些高级功能，比如批量插入，批量删除，批量更新，以及记忆的替代关系处理。
    #  管理长期记忆
    memory_store = MemoryStore(embedder)
    #  管理短期对话历史
    session_store = get_session_store()
    memory_runtime = build_memory_runtime(
        embedder=embedder,
        memory_store=memory_store,
        session_store=session_store,
    )
    #  保证是单例的 并且初始化执行对象和注册对象
    event_bus = EventBus.get_instance()
    tool_registry = ToolRegistry(
        initial_max_tools=settings.TOOL_INITIAL_MAX_SCHEMAS,
        initial_schema_char_budget=settings.TOOL_INITIAL_SCHEMA_CHAR_BUDGET,
        session_lru_size=settings.TOOL_SESSION_LRU_SIZE,
    )
    tool_executor = ToolExecutor()

    # 3. Initialize conversation logger (for evaluation)
    #  这个conversion就是把所有的对话写到一个文件里，方便测试
    conversation_logger = ConversationLogger()
    await conversation_logger.start()
    logger.info("Conversation logger started")

    # 4. Initialize reasoner + built-in tools before plugin discovery.
    reasoner = Reasoner(
        tool_registry=tool_registry,
        tool_executor=tool_executor,
        event_bus=event_bus,
    )
    #  注册 记忆相关的工具
    register_memory_tools(tool_registry, memory_runtime.engine)
    # 注册只读联网搜索和网页抓取工具
    register_web_tools(tool_registry)
    # Meta工具始终可见；其搜索结果只在当前Turn解锁目标Schema。
    register_tool_search(tool_registry)

    # M4: preconfigured MCP servers can be registered/unregistered at runtime.
    # Remote tools still enter the same ToolRegistry/ToolRuntime as built-ins,
    # so Reasoner and the five-phase pipeline remain protocol-agnostic.
    mcp_manager = McpManager.from_config(
        registry=tool_registry,
        config_path=settings.MCP_CONFIG_PATH,
        allowed_commands={
            item.strip()
            for item in settings.MCP_STDIO_COMMAND_ALLOWLIST.split(",")
            if item.strip()
        },
        allow_loopback_http=settings.MCP_ALLOW_LOOPBACK_HTTP,
        connect_timeout=settings.MCP_CONNECT_TIMEOUT,
        drain_timeout=settings.MCP_DRAIN_TIMEOUT,
    )
    register_mcp_management_tools(tool_registry, mcp_manager)
    await mcp_manager.start()
    proactive_sources = ProactiveSourceRegistry.from_config(
        settings.PROACTIVE_SOURCE_CONFIG_PATH
    )
    proactive_sources.validate_servers(set(mcp_manager.specs))
    register_proactive_source_management_tools(tool_registry, proactive_sources)

    # 5. Initialize plugin runtime after built-ins, so plugin tools can override by name. 初始化插件管理
    plugin_manager = PluginManager(
        [Path.cwd() / "plugins"],
        event_bus=event_bus,
        tool_registry=tool_registry,
        workspace=Path.cwd(),
        memory_engine=memory_runtime.engine,
    )
    await plugin_manager.load_all()
    plugin_manager.attach_tool_executor(tool_executor)
    reasoner.set_step_modules(
        before_step=plugin_manager.before_step_modules,
        after_step=plugin_manager.after_step_modules,
    )

    # 6. Initialize pipeline phases
    before_turn = BeforeTurnPhase(
        event_bus=event_bus,
        plugin_modules=plugin_manager.before_turn_modules,
        memory_engine=memory_runtime.engine,
    )
    before_reasoning = BeforeReasoningPhase(
        tool_registry=tool_registry,
        event_bus=event_bus,
        plugin_modules=plugin_manager.before_reasoning_modules,
        prompt_render_modules=plugin_manager.prompt_render_modules,
        self_model_reader=memory_runtime.markdown.store.read_self,
        long_term_memory_reader=memory_runtime.markdown.store.read_long_term,
        recent_context_reader=memory_runtime.markdown.store.read_recent_context,
    )
    await before_reasoning.preheat()
    after_reasoning = AfterReasoningPhase(
        memory_store,
        event_bus=event_bus,
        plugin_modules=plugin_manager.after_reasoning_modules,
    )
    after_turn = AfterTurnPhase(
        event_bus,
        None,
        plugin_modules=plugin_manager.after_turn_modules,
        deferred_dispatch=True,
    )  # TurnRuntime publishes the final reply through MessageBus outbound.

    # 7. Consolidation worker（窗口期 LLM 提取长期记忆）
    from agent.pipeline.consolidation_worker import ConsolidationWorker
    from agent.pipeline.invalidation_worker import InvalidationWorker
    consolidation = ConsolidationWorker(
        keep_count=10,
        min_new_messages=6,
        markdown_store=memory_runtime.markdown.store,
    )
    invalidation = InvalidationWorker(memory_store, embedder)
    from memory.markdown_vector_sync import MarkdownVectorSync
    from memory.optimizer import MemoryOptimizer, MemoryOptimizerLoop, OpenAITextProvider
    memory_optimizer_loop = MemoryOptimizerLoop(
        MemoryOptimizer(
            memory_runtime.markdown.store,
            OpenAITextProvider(),
            settings.LLM_MODEL,
            vector_sync=MarkdownVectorSync(memory_store),
        ),
        memory_runtime.markdown.store,
        interval=settings.MEMORY_OPTIMIZER_INTERVAL_SECONDS,
    )
    mode_coordinator = ModeCoordinator()
    idle_store = IdleTaskStore()
    idle_runtime = IdleTaskRuntime(
        idle_store,
        mode_coordinator,
        poll_seconds=settings.IDLE_TASK_POLL_SECONDS,
    )

    async def optimize_memory_idle(_context: object) -> IdleTaskResult:
        await memory_optimizer_loop.run_once()
        return IdleTaskResult(
            repeat_after_seconds=settings.MEMORY_OPTIMIZER_INTERVAL_SECONDS
        )

    idle_runtime.register(
        "memory_optimizer",
        optimize_memory_idle,
        permission="local_maintenance",
    )
    mode_coordinator.attach_idle(idle_runtime)

    # 8. Create pipeline
    pipeline = PassiveTurnPipeline(
        before_turn=before_turn,
        before_reasoning=before_reasoning,
        reasoner=reasoner,
        after_reasoning=after_reasoning,
        after_turn=after_turn,
        store=memory_store,
        consolidation_worker=consolidation,
        invalidation_worker=invalidation,
        memory_runtime=memory_runtime,
    )

    # 9. Create one asynchronous runtime shared by all channel adapters.
    message_bus = MessageBus(RuntimeMessageStore())
    stream_store = StreamEventStore()
    stream_broker = StreamEventBroker(
        stream_store,
        subscriber_queue_size=settings.STREAM_SUBSCRIBER_QUEUE_SIZE,
    )
    channel_identities = ChannelIdentityStore()
    turn_runtime = TurnRuntime(
        bus=message_bus,
        pipeline=pipeline,
        mode_coordinator=mode_coordinator,
        stream_broker=stream_broker,
        event_bus=event_bus,
    )
    adapter = TelegramAdapter(
        token=settings.TG_BOT_TOKEN,
        proxy=settings.HTTP_PROXY,
        runtime=turn_runtime,
        identities=channel_identities,
    )
    message_bus.subscribe_outbound("telegram", adapter.send_envelope)
    web_server: WebGatewayServer | None = None
    if settings.CHANNEL_WEB_ENABLED:
        web_gateway = WebGateway(
            turn_runtime,
            channel_identities,
            stream_broker,
            api_token=settings.CHANNEL_API_TOKEN,
        )
        web_server = WebGatewayServer(
            web_gateway,
            host=settings.CHANNEL_WEB_HOST,
            port=settings.CHANNEL_WEB_PORT,
        )
    cli_adapter: CliAdapter | None = None
    cli_task: asyncio.Task[None] | None = None
    if settings.CHANNEL_CLI_ENABLED:
        cli_adapter = CliAdapter(
            turn_runtime,
            channel_identities,
            stream_broker,
            account_id=settings.CHANNEL_CLI_ACCOUNT_ID,
        )
    proactive_loop: ProactiveLoop | None = None
    proactive_state: ProactiveStateStore | None = None
    if settings.PROACTIVE_ENABLED and settings.PROACTIVE_CHAT_ID.strip():
        proactive_user_id = (
            settings.PROACTIVE_USER_ID.strip() or settings.PROACTIVE_CHAT_ID.strip()
        )
        proactive_session_key = build_session_key(
            channel=settings.PROACTIVE_CHANNEL,
            chat_id=settings.PROACTIVE_CHAT_ID,
            user_id=proactive_user_id,
        )
        push_tool = MessagePushTool()

        async def send_proactive_text(chat_id: str, content: str) -> None:
            sent = await adapter.send(
                OutboundMessage(chat_id=int(chat_id), content=content)
            )
            if not sent:
                raise RuntimeError("Telegram delivery failed")

        push_tool.register_channel("telegram", text=send_proactive_text)
        proactive_state = ProactiveStateStore()
        proactive_gateway = McpProactiveGateway(
            McpManagerSourceCaller(mcp_manager),
            proactive_sources,
        )
        policy = ProactivePolicy(
            threshold=settings.PROACTIVE_THRESHOLD,
            cooldown_seconds=settings.PROACTIVE_COOLDOWN_SECONDS,
            daily_limit=settings.PROACTIVE_DAILY_LIMIT,
            quiet_start_hour=settings.PROACTIVE_QUIET_START_HOUR,
            quiet_end_hour=settings.PROACTIVE_QUIET_END_HOUR,
            timezone=settings.PROACTIVE_TIMEZONE,
            urgent_bypass_busy=settings.PROACTIVE_URGENT_BYPASS_BUSY,
            urgent_bypass_cooldown=settings.PROACTIVE_URGENT_BYPASS_COOLDOWN,
            urgent_bypass_quiet=settings.PROACTIVE_URGENT_BYPASS_QUIET,
            urgent_bypass_daily_limit=settings.PROACTIVE_URGENT_BYPASS_DAILY_LIMIT,
            normal_interval_seconds=settings.PROACTIVE_INTERVAL_SECONDS,
            blocked_interval_seconds=settings.PROACTIVE_BLOCKED_INTERVAL_SECONDS,
            empty_interval_seconds=settings.PROACTIVE_EMPTY_INTERVAL_SECONDS,
            cold_start_threshold=settings.PROACTIVE_COLD_START_THRESHOLD,
            content_dedupe_hours=settings.PROACTIVE_CONTENT_DEDUPE_HOURS,
            semantic_dedupe_hours=settings.PROACTIVE_SEMANTIC_DEDUPE_HOURS,
            semantic_similarity_threshold=(
                settings.PROACTIVE_SEMANTIC_SIMILARITY_THRESHOLD
            ),
            empty_backoff_multiplier=settings.PROACTIVE_EMPTY_BACKOFF_MULTIPLIER,
            empty_backoff_max_seconds=settings.PROACTIVE_EMPTY_BACKOFF_MAX_SECONDS,
            error_backoff_base_seconds=settings.PROACTIVE_ERROR_BACKOFF_BASE_SECONDS,
            error_backoff_max_seconds=settings.PROACTIVE_ERROR_BACKOFF_MAX_SECONDS,
            alert_interval_seconds=settings.PROACTIVE_ALERT_INTERVAL_SECONDS,
            schedule_jitter_ratio=settings.PROACTIVE_SCHEDULE_JITTER_RATIO,
        )
        interest_reader = MemoryInterestReader(
            memory_store,
            memory_runtime.markdown.store,
            max_items=settings.PROACTIVE_INTEREST_MAX_ITEMS,
            max_chars=settings.PROACTIVE_INTEREST_MAX_CHARS,
        )
        ambiguous_interest_judge = (
            OpenAIInterestJudge(OpenAITextProvider(), settings.LLM_MODEL)
            if settings.PROACTIVE_LLM_JUDGE_ENABLED
            else None
        )
        proactive_loop = ProactiveLoop(
            AgentTick(
                gateway=proactive_gateway,
                push_tool=push_tool,
                default_channel=settings.PROACTIVE_CHANNEL,
                default_chat_id=settings.PROACTIVE_CHAT_ID,
                user_id=proactive_user_id,
                session_key=proactive_session_key,
                mode="live" if settings.PROACTIVE_MODE.lower() == "live" else "shadow",
                policy=policy,
                state_store=proactive_state,
                passive_busy_fn=mode_coordinator.is_passive_active,
                interest_reader=interest_reader.read,
                ambiguous_interest_judge=ambiguous_interest_judge,
                ack_handlers=proactive_gateway.ack_handlers(),
                ack_max_attempts=settings.PROACTIVE_ACK_MAX_ATTEMPTS,
                ack_retry_base_seconds=settings.PROACTIVE_ACK_RETRY_BASE_SECONDS,
                ack_retry_max_seconds=settings.PROACTIVE_ACK_RETRY_MAX_SECONDS,
                mode_coordinator=mode_coordinator,
            ),
            interval_seconds=settings.PROACTIVE_INTERVAL_SECONDS,
            ack_interval_seconds=settings.PROACTIVE_ACK_WORKER_INTERVAL_SECONDS,
        )
        mode_coordinator.attach_proactive_waker(proactive_loop.wake)
    try:
        await turn_runtime.start()
        if settings.IDLE_TASKS_ENABLED:
            await idle_runtime.start()
            has_optimizer_task = any(
                task.task_type == "memory_optimizer"
                and task.status in {"queued", "running", "paused"}
                for task in idle_store.list()
            )
            if settings.MEMORY_OPTIMIZER_ENABLED and not has_optimizer_task:
                from datetime import datetime, timedelta, timezone

                idle_runtime.enqueue(
                    "memory_optimizer",
                    not_before=datetime.now(timezone.utc)
                    + timedelta(seconds=settings.MEMORY_OPTIMIZER_INTERVAL_SECONDS),
                    trace_id="idle:memory_optimizer",
                )
        if web_server is not None:
            await web_server.start()
            logger.info(
                "HTTP/SSE + WebSocket gateway started on %s:%s",
                settings.CHANNEL_WEB_HOST,
                settings.CHANNEL_WEB_PORT,
            )
        if cli_adapter is not None:
            cli_task = asyncio.create_task(
                cli_adapter.run_interactive(), name="cli-channel"
            )

        # 10. Start bot
        logger.info("Starting Telegram bot...")
        await adapter.start()

        # Get bot info after starting
        me = await adapter.application.bot.get_me()
        logger.info(f"Bot started as @{me.username}")
        if proactive_loop is not None:
            proactive_loop.start()
            logger.info("Proactive runtime started mode=%s", settings.PROACTIVE_MODE)

        # Keep running
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        if proactive_loop is not None:
            await proactive_loop.close()
        if proactive_state is not None:
            proactive_state.close()
        if cli_task is not None:
            cli_task.cancel()
            await asyncio.gather(cli_task, return_exceptions=True)
        if web_server is not None:
            await web_server.stop()
        await idle_runtime.close()
        await adapter.stop()
        await turn_runtime.stop()
        channel_identities.close()
        stream_store.close()
        await mcp_manager.close()
        # Stop conversation logger
        await plugin_manager.terminate_all()
        await conversation_logger.stop()
        logger.info("Conversation logger stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
