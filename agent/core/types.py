from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


def _empty_str_list() -> list[str]:
    return []


def _empty_metadata() -> dict[str, Any]:
    return {}


def _empty_history() -> tuple[Any, ...]:
    return ()


def _empty_tool_chain() -> tuple[dict[str, Any], ...]:
    return ()


def _empty_prompt_sections() -> list[Any]:
    return []


@dataclass(frozen=True)
class InboundMessage:
    user_id: int
    chat_id: int
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OutboundMessage:
    chat_id: int
    content: str
    format: str = "text"


@dataclass
class MemoryItem:
    id: UUID
    user_id: int
    memory_type: str
    summary: str
    embedding: list[float] | None
    status: str
    source_ref: str | None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Session:
    user_id: int
    chat_id: int
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    last_consolidated: int = 0  # 对齐 akashic session.last_consolidated


@dataclass
class PipelineContext:
    base_class_marker: bool = field(init=False, default=True)


@dataclass
class BeforeTurnCtx(PipelineContext):
    inbound_message: InboundMessage
    session: Session
    retrieved_memories: list[MemoryItem]
    session_key: str = ""
    channel: str = "telegram"
    chat_id: str = ""
    content: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)
    skill_names: list[str] = field(default_factory=_empty_str_list)
    retrieved_memory_block: str = ""
    retrieval_trace_raw: object | None = None
    history_messages: tuple[Any, ...] = field(default_factory=_empty_history)
    extra_hints: list[str] = field(default_factory=_empty_str_list)
    extra_metadata: dict[str, Any] = field(default_factory=_empty_metadata)
    abort: bool = False
    abort_reply: str = ""


@dataclass
class BeforeReasoningCtx(PipelineContext):
    session: Session
    memories: list[MemoryItem]
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    session_key: str = ""
    channel: str = "telegram"
    chat_id: str = ""
    content: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)
    skill_names: list[str] = field(default_factory=_empty_str_list)
    retrieved_memory_block: str = ""
    extra_hints: list[str] = field(default_factory=_empty_str_list)
    abort: bool = False
    abort_reply: str = ""
    prompt_sections: list[Any] = field(default_factory=_empty_prompt_sections)


@dataclass
class PromptRenderCtx(PipelineContext):
    session_key: str
    channel: str
    chat_id: str
    user_id: int | None
    content: str
    timestamp: datetime
    history: list[dict[str, Any]]
    memories: list[MemoryItem]
    benchmark_mode: bool = False
    skill_names: list[str] = field(default_factory=_empty_str_list)
    retrieved_memory_block: str = ""
    extra_hints: list[str] = field(default_factory=_empty_str_list)
    system_sections_top: list[Any] = field(default_factory=_empty_prompt_sections)
    system_sections_bottom: list[Any] = field(default_factory=_empty_prompt_sections)


@dataclass(frozen=True)
class PromptRenderResult:
    messages: list[dict[str, Any]]
    system_prompt: str
    system_sections: list[Any]


@dataclass
class BeforeStepCtx(PipelineContext):
    session_key: str
    channel: str
    chat_id: str
    iteration: int
    input_tokens_estimate: int
    visible_tool_names: frozenset[str] | None
    extra_hints: list[str] = field(default_factory=_empty_str_list)
    early_stop: bool = False
    early_stop_reply: str = ""


@dataclass
class AfterStepCtx(PipelineContext):
    session_key: str
    channel: str
    chat_id: str
    iteration: int
    tools_called: tuple[str, ...]
    partial_reply: str
    tools_used_so_far: tuple[str, ...]
    tool_chain_partial: tuple[dict[str, Any], ...]
    partial_thinking: str | None
    has_more: bool
    extra_metadata: dict[str, Any] = field(default_factory=_empty_metadata)


@dataclass
class ReasonerResult:
    content: str
    tool_calls: list[dict[str, Any]]
    finish_reason: str


@dataclass
class AfterReasoningCtx(PipelineContext):
    reasoner_result: ReasonerResult
    outbound_message: OutboundMessage
    session_key: str = ""
    channel: str = "telegram"
    chat_id: str = ""
    reply: str = ""
    thinking: str | None = None
    tools_used: tuple[str, ...] = field(default_factory=tuple)
    tool_chain: tuple[dict[str, Any], ...] = field(default_factory=_empty_tool_chain)
    media: list[str] = field(default_factory=_empty_str_list)
    outbound_metadata: dict[str, Any] = field(default_factory=_empty_metadata)


@dataclass
class AfterTurnCtx(PipelineContext):
    session_key: str
    channel: str
    chat_id: str
    reply: str
    tools_used: tuple[str, ...]
    thinking: str | None
    will_dispatch: bool
    extra_metadata: dict[str, Any] = field(default_factory=_empty_metadata)


@dataclass(frozen=True)
class BeforeToolCallCtx:
    session_key: str
    channel: str
    chat_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class AfterToolResultCtx:
    session_key: str
    channel: str
    chat_id: str
    tool_name: str
    arguments: dict[str, Any]
    result: str
    status: str


@dataclass
class PreToolCtx:
    session_key: str
    channel: str
    chat_id: str
    tool_name: str
    arguments: dict[str, Any]
    call_id: str = ""
    source: str = ""
    request_text: str = ""
    tool_batch: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    tool_batch_index: int = 0


@dataclass
class TurnCommittedEvent:
    turn_id: str
    user_id: int
    inbound_content: str
    outbound_message: OutboundMessage
    new_memory_ids: list[UUID]
    timestamp: datetime = field(default_factory=datetime.utcnow)
