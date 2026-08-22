"""
对话日志记录器：记录真实对话为原始数据，用于后续评估
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from agent.core.event_bus import EventBus
from agent.core.types import TurnCommittedEvent

logger = logging.getLogger(__name__)


class ConversationLogger:
    """对话日志记录器，订阅 TurnCommittedEvent 并记录对话到 JSONL 文件"""

    def __init__(self, log_dir: str = "./data/evaluation") -> None:
        """初始化日志记录器

        Args:
            log_dir: 日志文件存放目录
        """
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # 使用原始会话日志文件
        self.raw_log_path = self.log_dir / "raw_conversations.jsonl"

        # 内存中缓存未写入的对话（用于异步写入）
        self._pending_writes: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._write_task: asyncio.Task[None] | None = None

        # 订阅 EventBus
        self.event_bus = EventBus.get_instance()

        # 内存中的会话缓存：{session_id: conversation_dict}
        self._conversation_cache: dict[str, dict[str, Any]] = {}

    async def start(self) -> None:
        """启动日志记录器，订阅事件并启动写入任务"""
        # 订阅 turn_committed 事件
        self.event_bus.subscribe("turn_committed", self._handle_turn_committed)

        # 启动后台写入任务
        self._write_task = asyncio.create_task(self._write_loop(), name="conversation_logger_write")

        logger.info(f"ConversationLogger started, logging to {self.raw_log_path}")

    async def stop(self) -> None:
        """停止日志记录器，确保所有日志都写入"""
        if self._write_task:
            self._write_task.cancel()
            try:
                await self._write_task
            except asyncio.CancelledError:
                pass

        # 写入剩余的对话
        await self._flush_all()
        logger.info("ConversationLogger stopped")

    def _handle_turn_committed(self, event: TurnCommittedEvent) -> None:
        """处理 TurnCommittedEvent，将对话数据加入待写入队列

        注意：这是同步回调，不能执行异步操作，所以只是把数据放入队列
        """
        turn_data = {
            "turn_id": event.turn_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_id": event.user_id,
            "inbound_content": event.inbound_content,
            "outbound_message": {
                "chat_id": event.outbound_message.chat_id,
                "content": event.outbound_message.content,
                "format": event.outbound_message.format,
            },
            "new_memory_ids": [str(mid) for mid in event.new_memory_ids],
        }

        # 放入写入队列（非阻塞）
        try:
            self._pending_writes.put_nowait(turn_data)
        except asyncio.QueueFull:
            logger.warning("ConversationLogger queue full, dropping turn: %s", event.turn_id)

    async def _write_loop(self) -> None:
        """后台循环：从队列中取出数据并写入文件"""
        while True:
            try:
                turn_data = await self._pending_writes.get()
                await self._append_to_file(turn_data)
            except asyncio.CancelledError:
                # 退出前刷新所有待写数据
                break
            except Exception as e:
                logger.error("Error in write loop: %s", e)

    async def _append_to_file(self, turn_data: dict[str, Any]) -> None:
        """将一条对话数据追加到 JSONL 文件

        Args:
            turn_data: 包含 turn_id, timestamp, user_id, outbound_message 的字典
        """
        # 在单独的线程中写入文件，避免阻塞事件循环
        def _write():
            with open(self.raw_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(turn_data, ensure_ascii=False) + "\n")

        # 使用 run_in_executor 在线程池中执行
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _write)

        logger.debug("Logged turn: %s", turn_data["turn_id"])

    async def _flush_all(self) -> None:
        """将所有待写入数据刷新到文件"""
        while not self._pending_writes.empty():
            turn_data = self._pending_writes.get_nowait()
            await self._append_to_file(turn_data)

    def load_raw_conversations(self, limit: int = 0) -> list[dict[str, Any]]:
        """加载原始对话日志

        Args:
            limit: 限制返回的对话数量，0 表示全部

        Returns:
            对话数据列表
        """
        if not self.raw_log_path.exists():
            logger.warning("No raw conversations log file found: %s", self.raw_log_path)
            return []

        conversations = []
        with open(self.raw_log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        conversations.append(json.loads(line))
                        if limit > 0 and len(conversations) >= limit:
                            break
                    except json.JSONDecodeError as e:
                        logger.warning("Failed to parse JSON line: %s", e)

        logger.info("Loaded %d conversations from %s", len(conversations), self.raw_log_path)
        return conversations

    def clear_raw_conversations(self) -> None:
        """清空原始对话日志"""
        if self.raw_log_path.exists():
            self.raw_log_path.unlink()
            logger.info("Cleared raw conversations log: %s", self.raw_log_path)
