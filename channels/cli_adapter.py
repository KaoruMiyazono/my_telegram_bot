"""CLI/TUI adapter using the same durable TurnRuntime and stream protocol."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from agent.core.envelope import MessageEnvelope
from agent.runtime.stream_events import StreamEventBroker
from channels.base import (
    ChannelIdentityStore,
    ChannelRequest,
    ChannelReceipt,
    RuntimeChannelAdapter,
)


class CliAdapter(RuntimeChannelAdapter):
    channel = "cli"

    def __init__(
        self,
        runtime,
        identities: ChannelIdentityStore,
        broker: StreamEventBroker,
        *,
        account_id: str = "local",
        output: Callable[[str], None] = print,
    ) -> None:
        super().__init__(runtime, identities)
        self.broker = broker
        self.account_id = account_id
        self.output = output
        self.user_id = identities.resolve(self.channel, account_id)
        self.runtime.bus.subscribe_outbound(self.channel, self.send_envelope)

    async def send_envelope(self, _envelope: MessageEnvelope) -> None:
        """The stream is the CLI delivery receipt; no second output is needed."""

    async def submit_text(
        self,
        content: str,
        *,
        chat_id: int = 1,
        thread_id: str = "main",
        client_message_id: str | None = None,
    ) -> ChannelReceipt:
        return await self.submit(
            ChannelRequest(
                channel=self.channel,
                account_id=self.account_id,
                chat_id=chat_id,
                thread_id=thread_id,
                content=content,
                user_id=self.user_id,
                client_message_id=client_message_id,
            )
        )

    async def render_turn(self, receipt: ChannelReceipt, *, after_seq: int = 0) -> None:
        subscription = await self.broker.subscribe(
            receipt.session_key, after_seq=after_seq
        )
        try:
            async for event in subscription:
                if event.turn_id != receipt.turn_id:
                    continue
                if event.event_type == "assistant.delta":
                    self.output(str(event.payload.get("delta") or ""))
                elif event.event_type.startswith("tool."):
                    self.output(f"[{event.event_type}] {event.payload.get('tool_name', '')}")
                if event.terminal:
                    break
        finally:
            await subscription.close()

    async def run_interactive(self) -> None:
        self.output("CLI Agent 已启动；输入 /stop 中断，/exit 退出。")
        while True:
            content = await asyncio.to_thread(input, "> ")
            if content.strip() == "/exit":
                return
            request = ChannelRequest(
                channel=self.channel,
                account_id=self.account_id,
                chat_id=1,
                thread_id="main",
                content=content,
                user_id=self.user_id,
            )
            receipt = (
                await self.cancel(request)
                if content.strip() == "/stop"
                else await self.submit(request)
            )
            if receipt.accepted and content.strip() != "/stop":
                await self.render_turn(receipt)
