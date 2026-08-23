from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

import httpx

ToolFailureCode = Literal[
    "input_validation",
    "policy_check",
    "timeout",
    "network_error",
    "mcp_unavailable",
    "business_error",
    "cancelled",
    "output_validation",
    "hook_error",
    "unknown",
]


class ToolInputValidationError(ValueError):
    pass


class ToolOutputValidationError(ValueError):
    pass


class ToolBusinessError(RuntimeError):
    pass


class ToolUserCancelledError(RuntimeError):
    pass


class ToolRuntimeTimeoutError(TimeoutError):
    pass


@dataclass(frozen=True)
class ToolFailure:
    code: ToolFailureCode | str
    message: str
    retryable: bool
    exception_type: str


def classify_tool_exception(
    exc: BaseException,
    *,
    source_type: str = "builtin",
) -> ToolFailure:
    """Classify an exception without relying only on its display text."""

    exception_type = type(exc).__name__
    message = str(exc) or exception_type
    if isinstance(exc, ToolInputValidationError):
        return ToolFailure("input_validation", message, False, exception_type)
    if isinstance(exc, ToolOutputValidationError):
        return ToolFailure("output_validation", message, False, exception_type)
    if isinstance(exc, ToolRuntimeTimeoutError) or isinstance(
        exc, (asyncio.TimeoutError, httpx.TimeoutException)
    ):
        return ToolFailure("timeout", message, True, exception_type)
    if isinstance(exc, ToolUserCancelledError):
        return ToolFailure("cancelled", message, False, exception_type)
    if isinstance(exc, PermissionError):
        return ToolFailure("policy_check", message, False, exception_type)
    if exception_type == "HookExecutionError":
        return ToolFailure("hook_error", message, False, exception_type)
    if exception_type == "McpGenerationStaleError":
        return ToolFailure("mcp_unavailable", message, True, exception_type)
    if isinstance(exc, (ConnectionError, httpx.NetworkError, httpx.RemoteProtocolError)):
        code = "mcp_unavailable" if source_type == "mcp" else "network_error"
        return ToolFailure(code, message, True, exception_type)
    if isinstance(exc, ToolBusinessError):
        return ToolFailure("business_error", message, False, exception_type)

    lowered = message.lower()
    if any(token in lowered for token in ("429", "503", "rate limit", "temporarily")):
        code = "mcp_unavailable" if source_type == "mcp" else "network_error"
        return ToolFailure(code, message, True, exception_type)
    if source_type == "mcp" and any(
        token in lowered for token in ("connection", "session", "transport", "generation")
    ):
        return ToolFailure("mcp_unavailable", message, True, exception_type)
    return ToolFailure("business_error", message, False, exception_type)
