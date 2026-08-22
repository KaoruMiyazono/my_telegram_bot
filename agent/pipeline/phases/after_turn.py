import inspect
from collections.abc import Sequence
from typing import Any

from agent.core.types import (
    AfterReasoningCtx,
    AfterTurnCtx,
    OutboundMessage,
    TurnCommittedEvent,
)
from agent.core.event_bus import EventBus
from agent.lifecycle.phase import (
    PhaseFrame,
    PhaseModuleRunner,
    collect_prefixed_slots,
)
from channels.telegram.adapter import TelegramAdapter
from uuid import UUID


# `AfterTurnPhase` 是用户收到回复之前的最后一个 Pipeline Phase：它广播“这一轮已经形成结果”的事件，运行回合后插件和遥测观察者，然后调用 TelegramAdapter 发送 `OutboundMessage`。
class AfterTurnPhase:
    def __init__(
        self,
        event_bus: EventBus,
        telegram_adapter: TelegramAdapter,
        plugin_modules: Sequence[Any] | None = None,
    ) -> None:
        self.event_bus = event_bus
        self.telegram_adapter = telegram_adapter
        self.plugin_modules = list(plugin_modules or [])

    async def execute(
        self,
        ctx: AfterReasoningCtx,
        user_id: int,
        new_memory_ids: list[UUID],
        inbound_content: str = "",
    ) -> None:
        """Execute post-turn operations."""
        # Create and emit TurnCommittedEvent
        event = TurnCommittedEvent(
            turn_id=ctx.turn_id,
            user_id=user_id,
            inbound_content=inbound_content,
            outbound_message=ctx.outbound_message,
            new_memory_ids=new_memory_ids,
            session_key=ctx.session_key,
            trace_id=ctx.trace_id,
        )

        turn_ctx = AfterTurnCtx(
            session_key=ctx.session_key or f"{user_id}:{ctx.outbound_message.chat_id}",
            channel=ctx.channel or "telegram",
            chat_id=ctx.chat_id or str(ctx.outbound_message.chat_id),
            reply=ctx.outbound_message.content,
            tools_used=ctx.tools_used,
            thinking=ctx.thinking,
            will_dispatch=self.telegram_adapter is not None,
            extra_metadata=dict(ctx.outbound_metadata),
            turn_id=ctx.turn_id,
            trace_id=ctx.trace_id,
        )
        plugin_runner = PhaseModuleRunner(
            self.plugin_modules,
            phase_name="after_turn",
        )
        frame = PhaseFrame(
            input=ctx,
            slots={
                "turn:ctx": turn_ctx,
                "turn:committed": event,
                "after_turn.build_work": True,
            },
        )
        frame = await plugin_runner.run_ready(frame)
        #  让ConversationLogger 去记录对话，主要是把对话写到一个文件里，方便测试
        await self.event_bus.emit("turn_committed", event=event)
        frame.slots["after_turn.fanout_committed"] = True
        frame = await plugin_runner.run_ready(frame)
        turn_ctx = frame.slots.get("turn:ctx", turn_ctx)
        turn_ctx.extra_metadata.update(
            collect_prefixed_slots(frame.slots, "turn:telemetry:")
        )
        frame.slots["after_turn.collect_telemetry"] = True
        observed = self.event_bus.observe(turn_ctx)
        if inspect.isawaitable(observed):
            await observed

        # Send message via Telegram (skip if no adapter, e.g. during testing)
        if self.telegram_adapter is not None:
            await self.telegram_adapter.send(ctx.outbound_message)
        frame.slots["after_turn.dispatch"] = True
        frame.slots["after_turn.return"] = True
        plugin_runner.warn_unresolved()
