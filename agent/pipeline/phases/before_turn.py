from collections.abc import Sequence

from agent.core.event_bus import EventBus
from agent.core.types import BeforeTurnCtx, InboundMessage, MemoryItem, Session
from agent.lifecycle.phase import (
    PhaseFrame,
    PhaseModuleRunner,
    append_string_exports,
    collect_prefixed_slots,
)
from memory.engine import MemoryRetrieveRequest, MemoryScope
from memory.store import LONG_TERM_MEMORY_TYPES
from persistence.session_store import get_session_store

# 内存缓存（对应 akashic sm._cache），SessionStore 负责持久化
_sessions: dict[tuple[int, int], Session] = {}

# RRF 融合参数
_SESSION_SLOT = "session:session"
_CTX_SLOT = "session:ctx"
_EXTRA_HINT_PREFIX = "session:extra_hint:"
_ABORT_REPLY_SLOT = "session:abort_reply"


class BeforeTurnPhase:
    """检索阶段：加载会话 + RRF 融合检索 + 构建上下文"""

    def __init__(
        self,
        *,
        memory_engine: object,
        event_bus: EventBus | None = None,
        plugin_modules: Sequence[object] | None = None,
    ) -> None:
        self.event_bus = event_bus or EventBus.get_instance()
        self.plugin_modules = list(plugin_modules or [])
        self.memory_engine = memory_engine
        #  最新一轮检索到的记忆
        self.last_retrieved: list[MemoryItem] = []
        self.last_query_text = ""
        self.last_retrieved_memory_block = ""
        self.last_retrieval_trace: dict = {}

    async def acquire_session(self, message: InboundMessage) -> Session:
        key = (message.user_id, message.chat_id)
        session = _sessions.get(key)
        #  内存缓存里面有就直接返回，没有就去数据库里面查找，如果数据库里面也没有，就创建一个新的session对象，并且存到内存缓存里面
        if session is not None:
            return session
        session_store = get_session_store()
        session_state = session_store.load_state(message.user_id, message.chat_id)
        if session_state is None:
            saved_messages = []
            last_consolidated = 0
        else:
            saved_messages, last_consolidated = session_state
        session = Session(
            user_id=message.user_id,
            chat_id=message.chat_id,
            messages=saved_messages,
            last_consolidated=last_consolidated,
        )
        _sessions[key] = session
        return session
    #  输入是 session，也就是原始对话记录 query_text 是最近三条用户消息 + 当前消息，作为检索的 query_text userid是哪个用户
    async def prepare_context(
        self, session: Session, query_text: str, user_id: int
    ) -> list[MemoryItem]:
        """Retrieve passive memory context through the shared MemoryEngine."""
        result = await self.memory_engine.retrieve(  # type: ignore[attr-defined]
            MemoryRetrieveRequest(
                query=query_text,
                scope=MemoryScope(
                    user_id=user_id,
                    chat_id=session.chat_id,
                    session_key=f"{session.user_id}:{session.chat_id}",
                ),
                top_k=8,
                memory_types=LONG_TERM_MEMORY_TYPES,
            )
        )
        self.last_query_text = query_text
        self.last_retrieved = list(result.items)
        #  字符串格式的检索结果，方便后续的上下文构建和调试（和上面本质上一样的）
        self.last_retrieved_memory_block = result.text_block
        # 检索过程和统计信息，包括是否假想增加了检索，以及 生成的假想文本
        self.last_retrieval_trace = dict(result.trace or {})
        #  是否真的增加了结果
        self._last_hyde_used = bool(result.trace.get("hyde_used"))
        #  生成的假想文本
        self._last_hypothesis = str(result.trace.get("hypothesis") or "")
        return list(result.items)

    #  本质上这个before就是一个插件运行器，执行插件的before_turn阶段，插件可以在这个阶段对上下文进行修改或者添加额外的信息 额外信息就是用户的记忆
    async def build_ctx(self, inbound_message: InboundMessage) -> BeforeTurnCtx:
        #  把这个user的这个chat的session加载出来，如果没有就创建一个新的session对象，并且存到内存缓存里面 session指的是一个用户在一个聊天中的对话历史和状态（原文）
        session = await self.acquire_session(inbound_message)
        #  插件运行器，执行插件的before_turn阶段，插件可以在这个阶段对上下文进行修改或者添加额外的信息
        #  插件有向无环图调度器
        plugin_runner = PhaseModuleRunner(
            self.plugin_modules,
            phase_name="before_turn",
        )
        #  把当前的输入消息和session放到frame里，frame是一个上下文对象，包含了当前的输入、输出和一些中间状态 这也是告诉插件们，当前的输入消息和session已经准备好了，可以开始运行了
        frame = PhaseFrame(
            input=inbound_message,
            slots={
                _SESSION_SLOT: session,
                "before_turn.acquire_session": True,
            },
        )
        #  把依赖跑完，现在就是啥也没跑，因为还没有插件
        frame = await plugin_runner.run_ready(frame)
        early_ctx = frame.slots.get(_CTX_SLOT)
        if isinstance(early_ctx, BeforeTurnCtx):
            return early_ctx
        #  如果插件没有提前返回上下文，就继续执行后续的检索和上下文构建逻辑
        # early_abort 是一个标志，表示是否需要提前终止当前的对话流程，如果插件在运行过程中设置了这个标志，就会直接返回一个带有 abort=True 的上下文对象，表示当前的对话流程被阻断了
        early_abort = frame.slots.get(_ABORT_REPLY_SLOT)
        if isinstance(early_abort, str) and early_abort:
            return BeforeTurnCtx(
                inbound_message=inbound_message,
                session=session,
                retrieved_memories=[],
                session_key=f"{inbound_message.user_id}:{inbound_message.chat_id}",
                channel=str(inbound_message.metadata.get("channel") or "telegram"),
                chat_id=str(inbound_message.chat_id),
                content=inbound_message.content,
                history_messages=tuple(session.messages),
                abort=True,
                abort_reply=early_abort,
            )
        # 最近三条用户消息 + 当前消息，作为检索的 query_text
        user_messages = [
            msg["content"]
            for msg in session.messages[-3:]
            if msg.get("role") == "user"
        ]
        user_messages.append(inbound_message.content)
        #  query_text 是一个字符串，包含了最近三条用户消息和当前消息，用于检索相关的记忆信息
        query_text = " ".join(user_messages) if user_messages else inbound_message.content
        #  去记忆中检索相关的记忆信息，返回一个列表，里面包含了检索到的记忆项
        retrieved_memories = await self.prepare_context(
            session=session, query_text=query_text, user_id=inbound_message.user_id,
        )
        retrieved_memory_block = self.last_retrieved_memory_block
        frame.slots["session:retrieved_memories"] = retrieved_memories
        frame.slots["session:retrieved_memory_block"] = retrieved_memory_block
        frame.slots["session:retrieval_trace_raw"] = dict(self.last_retrieval_trace)
        frame.slots["before_turn.prepare_context"] = True
        frame = await plugin_runner.run_ready(frame)
        ctx = BeforeTurnCtx(
            inbound_message=inbound_message,
            session=session,
            retrieved_memories=retrieved_memories,
            session_key=f"{inbound_message.user_id}:{inbound_message.chat_id}",
            channel=str(inbound_message.metadata.get("channel") or "telegram"),
            chat_id=str(inbound_message.chat_id),
            content=inbound_message.content,
            retrieved_memory_block=retrieved_memory_block,
            retrieval_trace_raw=dict(self.last_retrieval_trace),
            history_messages=tuple(session.messages),
        )
        frame.slots[_CTX_SLOT] = ctx
        frame.slots["before_turn.build_ctx"] = True
        frame = await plugin_runner.run_ready(frame)
        ctx = frame.slots.get(_CTX_SLOT, ctx)
        emitted = await self.event_bus.emit(ctx)
        if emitted is None:
            ctx.abort = True
            if not ctx.abort_reply:
                ctx.abort_reply = "请求已被生命周期处理器阻断。"
            return ctx
        ctx = emitted
        frame.slots[_CTX_SLOT] = ctx
        frame.slots["before_turn.emit"] = True
        frame = await plugin_runner.run_ready(frame)
        ctx = frame.slots.get(_CTX_SLOT, ctx)
        append_string_exports(
            ctx.extra_hints,
            collect_prefixed_slots(frame.slots, _EXTRA_HINT_PREFIX),
        )
        frame.slots["before_turn.collect_exports"] = True
        abort_reply = frame.slots.get(_ABORT_REPLY_SLOT)
        if isinstance(abort_reply, str) and abort_reply:
            ctx.abort = True
            ctx.abort_reply = abort_reply
        frame.slots["before_turn.return"] = True
        plugin_runner.warn_unresolved()
        return ctx
