from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

HookEvent = Literal[
    "before_call",
    "after_call",
    "on_error",
    "on_cancel",
    "pre_tool_use",
    "post_tool_use",
    "post_tool_error",
]
ToolSource = Literal["passive", "proactive", "subagent"]
ToolExecStatus = Literal["success", "denied", "error"]
HookDecision = Literal["pass", "deny"]


@dataclass
class ToolExecutionRequest:
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    source: ToolSource
    session_key: str = ""
    channel: str = ""
    chat_id: str = ""
    request_text: str = ""
    tool_batch: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    tool_batch_index: int = 0
    attempt: int = 0
    tool_source_type: str = "builtin"
    tool_source_name: str = ""
    risk: str = "read-only"
    idempotent: bool = True


@dataclass
class HookContext:
    event: HookEvent
    request: ToolExecutionRequest
    current_arguments: dict[str, Any]
    result: Any = ""
    error: str = ""
    error_code: str = ""
    retryable: bool = False
    attempt: int = 0
    audit_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class HookOutcome:
    decision: HookDecision = "pass"
    updated_input: dict[str, Any] | None = None
    extra_message: str = ""
    reason: str = ""
    output_updated: bool = False
    updated_output: Any = None
    audit_metadata: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    retryable: bool | None = None


@dataclass
class HookTraceItem:
    hook_name: str
    event: HookEvent
    matched: bool
    decision: HookDecision = "pass"
    reason: str = ""
    extra_message: str = ""
    attempt: int = 0
    input_before: dict[str, Any] = field(default_factory=dict)
    input_after: dict[str, Any] = field(default_factory=dict)
    output_before: Any = None
    output_after: Any = None
    audit_metadata: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    retryable: bool | None = None


@dataclass
class ToolExecutionResult:
    status: ToolExecStatus
    output: Any
    final_arguments: dict[str, Any]
    extra_messages: list[str] = field(default_factory=list)
    pre_hook_trace: list[HookTraceItem] = field(default_factory=list)
    post_hook_trace: list[HookTraceItem] = field(default_factory=list)
    error_hook_trace: list[HookTraceItem] = field(default_factory=list)
    cancel_hook_trace: list[HookTraceItem] = field(default_factory=list)
    audit_metadata: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    retryable: bool | None = None
    exception_type: str = ""
