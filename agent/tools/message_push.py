from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from agent.tools.base import Tool

logger = logging.getLogger(__name__)


class MessagePushTool:
    """Akashic-style outbound push tool.

    The proactive chain owns this tool directly, and it can also be mounted into
    ToolRegistry through `as_tool()` when a future proactive agent needs LLM tool
    calls.
    """

    name = "message_push"
    description = (
        "向指定渠道的用户主动发送消息、文件或图片。"
        "需要提供渠道名和目标 chat_id；message/file/image 至少提供一个。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "channel": {"type": "string", "description": "目标渠道名，如 telegram"},
            "chat_id": {"type": "string", "description": "目标会话 ID"},
            "message": {"type": "string", "description": "要发送的文本内容"},
            "file": {"type": "string", "description": "要发送的文件本地路径"},
            "image": {"type": "string", "description": "要发送的图片本地路径或 URL"},
        },
        "required": ["channel", "chat_id"],
    }

    def __init__(self) -> None:
        self._senders: dict[str, dict[str, Callable[..., Awaitable[None]]]] = {}

    def register_channel(
        self,
        channel: str,
        *,
        text: Callable[[str, str], Awaitable[None]] | None = None,
        file: Callable[[str, str, str | None], Awaitable[None]] | None = None,
        image: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        senders: dict[str, Callable[..., Awaitable[None]]] = {}
        if text is not None:
            senders["text"] = text
        if file is not None:
            senders["file"] = file
        if image is not None:
            senders["image"] = image
        self._senders[channel] = senders

    async def execute(
        self,
        *,
        channel: str,
        chat_id: str,
        message: str | None = None,
        file: str | None = None,
        image: str | None = None,
    ) -> str:
        channel = str(channel)
        chat_id = str(chat_id)
        if not message and not file and not image:
            return "错误：message、file、image 至少提供一个"

        senders = self._senders.get(channel)
        if senders is None:
            return f"渠道 {channel!r} 未注册，可用渠道：{list(self._senders) or ['无']}"

        results: list[str] = []
        try:
            if message:
                sender = senders.get("text")
                if sender is None:
                    results.append(f"渠道 {channel!r} 不支持发送文本")
                else:
                    await sender(chat_id, message)
                    results.append("文本已发送")

            if file:
                sender = senders.get("file")
                if sender is None:
                    results.append(f"渠道 {channel!r} 不支持发送文件")
                else:
                    import os

                    name = os.path.basename(file)
                    await sender(chat_id, file, name)
                    results.append(f"文件 {name!r} 已发送")

            if image:
                sender = senders.get("image")
                if sender is None:
                    results.append(f"渠道 {channel!r} 不支持发送图片")
                else:
                    await sender(chat_id, image)
                    results.append("图片已发送")
        except Exception as exc:
            logger.exception("[message_push] send failed channel=%s chat=%s", channel, chat_id)
            return f"发送失败：{exc}"

        return "；".join(results) if results else f"渠道 {channel!r} 没有可用的 sender"

    def as_tool(self) -> Tool:
        async def _handler(arguments: dict[str, Any], ctx: Any = None) -> str:
            return await self.execute(
                channel=str(arguments.get("channel") or ""),
                chat_id=str(arguments.get("chat_id") or ""),
                message=arguments.get("message"),
                file=arguments.get("file"),
                image=arguments.get("image"),
            )

        return Tool(
            name=self.name,
            description=self.description,
            parameters=dict(self.parameters),
            handler=_handler,
        )
