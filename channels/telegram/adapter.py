import asyncio
import logging
from typing import TYPE_CHECKING

from telegram import Update
from telegram.error import TimedOut, NetworkError, RetryAfter
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

if TYPE_CHECKING:
    from agent.pipeline.passive_turn import PassiveTurnPipeline
    from agent.runtime.turn_runtime import TurnRuntime

from agent.core.envelope import MessageEnvelope, MessagePriority, envelope_from_inbound
from agent.core.types import InboundMessage
from channels.base import ChannelIdentityStore, ChannelRequest, RuntimeChannelAdapter

logger = logging.getLogger(__name__)


class TelegramAdapter:
    """Telegram bot adapter using python-telegram-bot."""

    def __init__(
        self,
        token: str,
        pipeline: "PassiveTurnPipeline | None" = None,
        proxy: str | None = None,
        runtime: "TurnRuntime | None" = None,
        identities: ChannelIdentityStore | None = None,
    ) -> None:
        self.token = token
        self.pipeline = pipeline
        self.proxy = proxy
        self.runtime = runtime
        self.application: Application | None = None
        self._initialized = False
        self.identities = identities
        self._runtime_adapter = (
            RuntimeChannelAdapter(runtime, identities)
            if runtime is not None and identities is not None
            else None
        )

    async def _handle_message(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle incoming message."""
        if not update.effective_message or not update.effective_user:
            return

        try:
            # Parse Update to InboundMessage
            inbound = InboundMessage(
                user_id=update.effective_user.id,
                chat_id=update.effective_chat.id,
                content=update.effective_message.text or "",
                metadata={
                    "update_id": update.update_id,
                    "username": update.effective_user.username,
                },
                channel="telegram",
            )

            logger.info(
                f"Received message from {inbound.user_id}: {inbound.content[:50]}"
            )

            if self.runtime is not None:
                account_id = str(update.effective_user.id)
                thread_id = str(
                    getattr(update.effective_message, "message_thread_id", None) or "main"
                )
                if self._runtime_adapter is not None and self.identities is not None:
                    user_id = self.identities.resolve(
                        "telegram",
                        account_id,
                        trusted_native_user_id=update.effective_user.id,
                    )
                    receipt = await self._runtime_adapter.submit(
                        ChannelRequest(
                            channel="telegram",
                            account_id=account_id,
                            chat_id=update.effective_chat.id,
                            thread_id=thread_id,
                            content=inbound.content,
                            user_id=user_id,
                            client_message_id=f"telegram:{update.update_id}",
                            metadata={**inbound.metadata, "preempt_active": True},
                        )
                    )
                    accepted = receipt.accepted
                else:
                    inbound = InboundMessage(
                        user_id=inbound.user_id,
                        chat_id=inbound.chat_id,
                        content=inbound.content,
                        metadata={
                            **inbound.metadata,
                            "preempt_active": True,
                            "account_id": account_id,
                            "thread_id": thread_id,
                        },
                        channel=inbound.channel,
                    )
                    accepted = await self.runtime.bus.publish_inbound(
                        envelope_from_inbound(
                            inbound,
                            client_message_id=f"telegram:{update.update_id}",
                        )
                    )
                if not accepted:
                    logger.info("Duplicate Telegram update ignored update_id=%s", update.update_id)
            elif self.pipeline is not None:
                outbound = await self.pipeline.execute(inbound)
                logger.info("Sent response to %s: %.50s", outbound.chat_id, outbound.content)
            else:
                raise RuntimeError("TelegramAdapter requires pipeline or runtime")

        except Exception as e:
            logger.error(f"Error handling message: {e}", exc_info=True)

    async def _start_command(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle /start command."""
        if update.effective_message:
            await update.effective_message.reply_text(
                "你好！我是一个 AI 助手，有什么我可以帮你的吗？"
            )

    async def _stop_command(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Put a high-priority interrupt onto the runtime control path."""
        if (
            self.runtime is None
            or not update.effective_message
            or not update.effective_user
            or not update.effective_chat
        ):
            if update.effective_message:
                await update.effective_message.reply_text("当前运行模式不支持中断。")
            return
        account_id = str(update.effective_user.id)
        thread_id = str(
            getattr(update.effective_message, "message_thread_id", None) or "main"
        )
        if self._runtime_adapter is not None and self.identities is not None:
            user_id = self.identities.resolve(
                "telegram", account_id, trusted_native_user_id=update.effective_user.id
            )
            await self._runtime_adapter.cancel(
                ChannelRequest(
                    channel="telegram",
                    account_id=account_id,
                    chat_id=update.effective_chat.id,
                    thread_id=thread_id,
                    content="/stop",
                    user_id=user_id,
                    client_message_id=f"telegram:{update.update_id}",
                    metadata={"update_id": update.update_id},
                )
            )
        else:
            inbound = InboundMessage(
                user_id=update.effective_user.id,
                chat_id=update.effective_chat.id,
                content="/stop",
                metadata={
                    "update_id": update.update_id,
                    "account_id": account_id,
                    "thread_id": thread_id,
                },
                channel="telegram",
            )
            await self.runtime.bus.publish_inbound(
                envelope_from_inbound(
                    inbound,
                    client_message_id=f"telegram:{update.update_id}",
                    priority=MessagePriority.INTERRUPT,
                )
            )

    async def send_envelope(self, envelope: MessageEnvelope) -> None:
        await self.send(envelope.as_outbound())

    async def send(self, message) -> bool:
        """Send message via Telegram (called by AfterTurnPhase). Retries on network errors."""
        if not self.application:
            logger.error("send() called but application is None — message dropped")
            return False

        max_retries = 3
        for attempt in range(max_retries):
            try:
                await self.application.bot.send_message(
                    chat_id=message.chat_id,
                    text=message.content,
                )
                return True
            except RetryAfter as e:
                delay = float(getattr(e, "retry_after", 1.0) or 1.0) + 1.0
                logger.warning(
                    "send_message rate limited, retry %d/%d in %.1fs",
                    attempt + 1, max_retries, delay,
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(delay)
            except (TimedOut, NetworkError) as e:
                delay = 2 ** attempt  # 1s, 2s, 4s
                logger.warning(
                    "send_message failed (%s), retry %d/%d in %.1fs  chat_id=%s",
                    type(e).__name__, attempt + 1, max_retries, delay, message.chat_id,
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(delay)
        logger.error(
            "send_message FAILED after %d attempts  chat_id=%s  text=%.100s",
            max_retries, message.chat_id, message.content,
        )
        return False

    async def start(self) -> None:
        """Start the bot with polling."""
        if self.proxy:
            from telegram.request import HTTPXRequest

            request = HTTPXRequest(
                proxy=self.proxy,
                connect_timeout=30.0,
                read_timeout=60.0,
                write_timeout=30.0,
                connection_pool_size=8,
                pool_timeout=10.0,
            )
            get_updates_request = HTTPXRequest(
                proxy=self.proxy,
                connect_timeout=30.0,
                read_timeout=60.0,
                write_timeout=30.0,
                connection_pool_size=1,
                pool_timeout=10.0,
            )
            self.application = (
                Application.builder()
                .token(self.token)
                .request(request)
                .get_updates_request(get_updates_request)
                .build()
            )
            logger.info(f"Using proxy: {self.proxy}")
        else:
            self.application = Application.builder().token(self.token).build()

        # Register handlers
        self.application.add_handler(CommandHandler("start", self._start_command))
        self.application.add_handler(CommandHandler("stop", self._stop_command))
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )

        logger.info("Starting Telegram bot polling...")
        await self.application.initialize()
        self._initialized = True
        await self.application.start()
        await self.application.updater.start_polling(drop_pending_updates=False)

    async def stop(self) -> None:
        """Stop the bot."""
        if self.application:
            logger.info("Stopping Telegram bot...")
            if self.application.updater.running:
                await self.application.updater.stop()
            if self.application.running:
                await self.application.stop()
            if self._initialized:
                await self.application.shutdown()
                self._initialized = False
