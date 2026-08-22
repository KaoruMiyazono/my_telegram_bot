"""
事件定义：Pipeline 各阶段的事件类型
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from agent.core.message_bus import InboundMessage, OutboundMessage


@dataclass
class BeforeTurnEvent:
    """BeforeTurn 阶段事件"""
    inbound: InboundMessage
    session_id: str
    context: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


@dataclass
class BeforeReasoningEvent:
    """BeforeReasoning 阶段事件"""
    session_id: str
    messages: list[dict]
    retrieved_memories: list[dict] = field(default_factory=list)
    tools: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class AfterReasoningEvent:
    """AfterReasoning 阶段事件"""
    session_id: str
    response_content: str
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


@dataclass
class AfterTurnEvent:
    """AfterTurn 阶段事件"""
    session_id: str
    inbound: InboundMessage
    outbound: OutboundMessage
    memories_written: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class TurnCommittedEvent:
    """Turn 完成提交事件（用于后处理）"""
    session_id: str
    user_id: str
    inbound_message: dict
    response_content: str
    new_memory_ids: list[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class MemoryWriteEvent:
    """记忆写入事件"""
    user_id: str
    memory_type: str  # profile/preference/procedure/event
    summary: str
    embedding: list[float] | None = None
    source_ref: str | None = None
    happened_at: str | None = None
    emotional_weight: int = 0
    extra: dict = field(default_factory=dict)


@dataclass
class MemorySupersedeEvent:
    """记忆废弃事件"""
    old_item_ids: list[str]
    new_item_id: str
    relation_type: str = "supersede"
    source_ref: str | None = None
