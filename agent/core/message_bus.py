"""
消息总线：agent 与各 channel 之间的异步消息传递
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Self

logger = logging.getLogger(__name__)


@dataclass
class InboundMessage:
    """入站消息：从 Channel 到 Agent"""
    channel: str
    user_id: str
    chat_id: str
    content: str
    message_id: int | None = None
    username: str | None = None
    metadata: dict = None

    def __post_init__(self) -> None:
        if self.metadata is None:
            self.metadata = {}


@dataclass
class OutboundMessage:
    """出站消息：从 Agent 到 Channel"""
    channel: str
    chat_id: str
    content: str
    parse_mode: str | None = None  # "Markdown", "HTML", or None
    reply_to_message_id: int | None = None


class MessageBus:
    """agent 与各 channel 之间的异步消息总线"""

    def __init__(self) -> None:
        self._inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()
        self._subscribers: dict[
            str, list[Callable[[OutboundMessage], Awaitable[None]]]
        ] = {}
        self._running = False
        self._dispatch_task: asyncio.Task[None] | None = None

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """channel → agent"""
        await self._inbound.put(msg)

    async def consume_inbound(self) -> InboundMessage:
        """阻塞直到有消息可消费"""
        return await self._inbound.get()

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """agent → channel"""
        await self._outbound.put(msg)

    def subscribe_outbound(
        self,
        channel: str,
        callback: Callable[[OutboundMessage], Awaitable[None]],
    ) -> None:
        """订阅某 channel 的出站消息"""
        self._subscribers.setdefault(channel, []).append(callback)

    async def dispatch_outbound(self) -> None:
        """后台任务：将出站消息分发给对应 channel 的订阅者"""
        self._running = True
        while self._running:
            try:
                msg = await asyncio.wait_for(self._outbound.get(), timeout=1.0)
                for cb in self._subscribers.get(msg.channel, []):
                    try:
                        await cb(msg)
                    except Exception as first_err:
                        logger.warning(
                            f"分发消息到 {msg.channel} 首次失败，2s 后重试: {first_err}"
                        )
                        await asyncio.sleep(2)
                        try:
                            await cb(msg)
                        except Exception as second_err:
                            logger.error(
                                f"分发消息到 {msg.channel} 重试仍失败: {second_err}"
                            )
            except asyncio.TimeoutError:
                continue

    def start_dispatch(self) -> asyncio.Task[None]:
        """启动出站消息分发任务"""
        if self._dispatch_task is None or self._dispatch_task.done():
            self._dispatch_task = asyncio.create_task(
                self.dispatch_outbound(),
                name="message_bus_dispatch",
            )
        return self._dispatch_task

    async def stop(self) -> None:
        """停止消息总线"""
        self._running = False
        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass

    @property
    def inbound_size(self) -> int:
        return self._inbound.qsize()

    @property
    def outbound_size(self) -> int:
        return self._outbound.qsize()
