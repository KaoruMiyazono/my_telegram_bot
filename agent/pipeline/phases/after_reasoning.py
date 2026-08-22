from collections.abc import Sequence

from agent.core.types import (
    AfterReasoningCtx,
    MemoryItem,
    OutboundMessage,
    ReasonerResult,
    Session,
)
from agent.core.event_bus import EventBus
from agent.lifecycle.phase import (
    PhaseFrame,
    PhaseModuleRunner,
    collect_prefixed_slots,
)
from memory.store import MemoryStore
from uuid import uuid4
from datetime import datetime
# `AfterReasoningPhase` 位于 Reasoner 之后、消息真正发送之前。它把模型的 `ReasonerResult` 包装成 Telegram 可发送的 `OutboundMessage`，同时给 EventBus 和插件一次修改回复、添加 metadata 或 media 的机会。
# AfterReasoningPhase：把回答加工成“系统准备发送什么”

class AfterReasoningPhase:
    def __init__(
        self,
        store: MemoryStore,
        event_bus: EventBus | None = None,
        plugin_modules: Sequence[object] | None = None,
    ) -> None:
        self.store = store
        self.event_bus = event_bus or EventBus.get_instance()
        self.plugin_modules = list(plugin_modules or [])

    async def build_ctx(
        self,
        result: ReasonerResult,
        session: Session,
        chat_id: int,
        user_id: int,
    ) -> AfterReasoningCtx:
        """Build AfterReasoningCtx from ReasonerResult."""
        # Extract content and create OutboundMessage
        content = result.content
        outbound_msg = OutboundMessage(
            chat_id=chat_id,
            content=content,
            format="text",
        )

        # Persist user message and assistant message as memories
        # For now, we'll store them as "event" type memories
        # The actual message content would be passed in, but for now we use what's in session

        ctx = AfterReasoningCtx(
            reasoner_result=result,
            outbound_message=outbound_msg,
            session_key=f"{user_id}:{chat_id}",
            channel="telegram",
            chat_id=str(chat_id),
            reply=content,
            tools_used=tuple(
                call.get("function", {}).get("name", "")
                for call in result.tool_calls
                if call.get("function", {}).get("name")
            ),
            tool_chain=tuple(result.tool_calls),
        )
        plugin_runner = PhaseModuleRunner(
            self.plugin_modules,
            phase_name="after_reasoning",
        )
        frame = PhaseFrame(
            input=result,
            slots={
                "reasoning:ctx": ctx,
                "after_reasoning.build_ctx": True,
            },
        )
        frame = await plugin_runner.run_ready(frame)
        ctx = frame.slots.get("reasoning:ctx", ctx)
        emitted = await self.event_bus.emit(ctx)
        if emitted is not None:
            ctx = emitted
        frame.slots["reasoning:ctx"] = ctx
        frame.slots["after_reasoning.emit"] = True
        frame.slots["after_reasoning.persist_user"] = True
        frame.slots["after_reasoning.persist_assistant"] = True
        frame = await plugin_runner.run_ready(frame)
        ctx = frame.slots.get("reasoning:ctx", ctx)
        ctx.outbound_metadata.update(
            collect_prefixed_slots(frame.slots, "outbound:metadata:")
        )
        # media_exports 是从插件槽位中收集准备发送的图片、音频、视频等附件；但在当前项目中只是收集了，还没有真正发送出去。 但是其实还没实现
        media_exports = collect_prefixed_slots(frame.slots, "outbound:media:")
        for value in media_exports.values():
            if isinstance(value, str) and value.strip():
                ctx.media.append(value)
            elif isinstance(value, list):
                ctx.media.extend(str(item) for item in value if str(item).strip())
        if ctx.reply != ctx.outbound_message.content:
            ctx.outbound_message = OutboundMessage(
                chat_id=chat_id,
                content=ctx.reply,
                format=ctx.outbound_message.format,
            )
            ctx.reasoner_result.content = ctx.reply
        frame.slots["after_reasoning.collect_exports"] = True
        frame.slots["after_reasoning.return"] = True
        plugin_runner.warn_unresolved()
        return ctx

    async def persist_messages(
        self,
        session: Session,
        user_message: str,
        assistant_message: str,
        user_id: int,
        chat_id: int,
    ) -> list[MemoryItem]:
        """Raw turns are persisted by SessionStore, not the long-term vector pool."""
        return []
