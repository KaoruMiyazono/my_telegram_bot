import asyncio
import logging
from pathlib import Path

from agent.core.event_bus import EventBus
from agent.core.message_bus import MessageBus
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

    # 5. Initialize plugin runtime after built-ins, so plugin tools can override by name. 初始化插件管理
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

    # 9. Create asynchronous runtime and Telegram adapter.
    message_bus = MessageBus(RuntimeMessageStore())
    turn_runtime = TurnRuntime(bus=message_bus, pipeline=pipeline)
    adapter = TelegramAdapter(
        token=settings.TG_BOT_TOKEN,
        proxy=settings.HTTP_PROXY,
        runtime=turn_runtime,
    )
    message_bus.subscribe_outbound("telegram", adapter.send_envelope)
    try:
        await turn_runtime.start()

        # 10. Start bot
        logger.info("Starting Telegram bot...")
        await adapter.start()

        # Get bot info after starting
        me = await adapter.application.bot.get_me()
        logger.info(f"Bot started as @{me.username}")

        # Keep running
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await adapter.stop()
        await turn_runtime.stop()
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
