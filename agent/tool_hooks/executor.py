from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from agent.tool_hooks.base import ToolHook
from agent.tool_hooks.builtin import SensitiveOutputRedactionHook
from agent.tool_hooks.errors import (
    ToolFailure,
    ToolInputValidationError,
    ToolOutputValidationError,
    classify_tool_exception,
)
from agent.tool_hooks.redaction import redact_sensitive
from agent.tool_hooks.types import (
    HookContext,
    HookEvent,
    HookOutcome,
    HookTraceItem,
    ToolExecutionRequest,
    ToolExecutionResult,
)

ToolInvoker = Callable[[str, dict[str, Any]], Awaitable[Any]]
ToolValidator = Callable[[Any], list[str]]

_EVENT_ALIASES: dict[str, set[str]] = {
    "before_call": {"before_call", "pre_tool_use"},
    "after_call": {"after_call", "post_tool_use"},
    "on_error": {"on_error", "post_tool_error"},
    "on_cancel": {"on_cancel"},
}


class HookExecutionError(RuntimeError):
    def __init__(self, hook_name: str, event: str, cause: Exception) -> None:
        self.hook_name = hook_name
        self.event = event
        self.cause = cause
        super().__init__(f"hook {hook_name} ({event}) failed: {cause}")


class ToolExecutor:
    """Run validation, four hook phases and one real invocation in order."""

    def __init__(self, hooks: Sequence[ToolHook] | None = None) -> None:
        self._hooks: list[ToolHook] = list(hooks or [])
        # Security post-processing is deliberately last: plugin hooks may add
        # fields, but none can reintroduce a secret after this hook runs.
        self._hooks.append(SensitiveOutputRedactionHook())

    def add_hooks(self, hooks: Sequence[ToolHook]) -> None:
        self._hooks[-1:-1] = list(hooks)

    async def execute(
        self,
        request: ToolExecutionRequest,
        invoker: ToolInvoker,
        *,
        input_validator: ToolValidator | None = None,
        output_validator: ToolValidator | None = None,
    ) -> ToolExecutionResult:
        current_arguments = dict(request.arguments)
        extra_messages: list[str] = []
        audit_metadata: dict[str, Any] = {}
        pre_trace: list[HookTraceItem] = []
        post_trace: list[HookTraceItem] = []
        error_trace: list[HookTraceItem] = []
        cancel_trace: list[HookTraceItem] = []

        try:
            self._validate_input(input_validator, current_arguments)
            denied_reason, current_arguments = await self._run_before_hooks(
                request=request,
                current_arguments=current_arguments,
                extra_messages=extra_messages,
                audit_metadata=audit_metadata,
                traces=pre_trace,
            )
            final_arguments = dict(current_arguments)
            if denied_reason:
                return ToolExecutionResult(
                    status="denied",
                    output=denied_reason,
                    final_arguments=final_arguments,
                    extra_messages=extra_messages,
                    pre_hook_trace=pre_trace,
                    post_hook_trace=post_trace,
                    error_hook_trace=error_trace,
                    cancel_hook_trace=cancel_trace,
                    audit_metadata=audit_metadata,
                    error_code="policy_check",
                    retryable=False,
                )

            # A before_call hook may have rewritten the input. Validate again
            # so an invalid rewrite never reaches the real tool.
            self._validate_input(input_validator, final_arguments)
            output = await invoker(request.tool_name, final_arguments)
            output = await self._run_after_hooks(
                request=request,
                current_arguments=final_arguments,
                output=output,
                extra_messages=extra_messages,
                audit_metadata=audit_metadata,
                traces=post_trace,
            )
            self._validate_output(output_validator, output)
            return ToolExecutionResult(
                status="success",
                output=output,
                final_arguments=final_arguments,
                extra_messages=extra_messages,
                pre_hook_trace=pre_trace,
                post_hook_trace=post_trace,
                error_hook_trace=error_trace,
                cancel_hook_trace=cancel_trace,
                audit_metadata=audit_metadata,
            )
        except asyncio.CancelledError:
            await self._run_cancel_hooks(
                request=request,
                current_arguments=current_arguments,
                audit_metadata=audit_metadata,
                traces=cancel_trace,
            )
            raise
        except Exception as exc:
            return await self._settle_error(
                request=request,
                current_arguments=current_arguments,
                cause=exc,
                extra_messages=extra_messages,
                audit_metadata=audit_metadata,
                pre_trace=pre_trace,
                post_trace=post_trace,
                error_trace=error_trace,
                cancel_trace=cancel_trace,
            )

    async def _settle_error(
        self,
        *,
        request: ToolExecutionRequest,
        current_arguments: dict[str, Any],
        cause: Exception,
        extra_messages: list[str],
        audit_metadata: dict[str, Any],
        pre_trace: list[HookTraceItem],
        post_trace: list[HookTraceItem],
        error_trace: list[HookTraceItem],
        cancel_trace: list[HookTraceItem],
    ) -> ToolExecutionResult:
        failure = classify_tool_exception(cause, source_type=request.tool_source_type)
        try:
            failure = await self._run_error_hooks(
                request=request,
                current_arguments=current_arguments,
                failure=failure,
                extra_messages=extra_messages,
                audit_metadata=audit_metadata,
                traces=error_trace,
            )
        except HookExecutionError as hook_error:
            failure = classify_tool_exception(
                hook_error, source_type=request.tool_source_type
            )
        return ToolExecutionResult(
            status="error",
            output=failure.message,
            final_arguments=dict(current_arguments),
            extra_messages=extra_messages,
            pre_hook_trace=pre_trace,
            post_hook_trace=post_trace,
            error_hook_trace=error_trace,
            cancel_hook_trace=cancel_trace,
            audit_metadata=audit_metadata,
            error_code=str(failure.code),
            retryable=failure.retryable,
            exception_type=failure.exception_type,
        )

    async def _run_before_hooks(
        self,
        *,
        request: ToolExecutionRequest,
        current_arguments: dict[str, Any],
        extra_messages: list[str],
        audit_metadata: dict[str, Any],
        traces: list[HookTraceItem],
    ) -> tuple[str, dict[str, Any]]:
        for hook in self._hooks_for("before_call"):
            before = dict(current_arguments)
            ctx = self._context(
                event="before_call",
                request=request,
                arguments=before,
                audit_metadata=audit_metadata,
            )
            matched = self._matches(hook, ctx)
            if not matched:
                traces.append(
                    self._trace(
                        hook, ctx, matched=False,
                        input_before=before, input_after=before,
                    )
                )
                continue
            outcome = await self._run(hook, ctx)
            if outcome.updated_input is not None:
                current_arguments = dict(outcome.updated_input)
            self._merge_outcome(outcome, extra_messages, audit_metadata)
            traces.append(
                self._trace(
                    hook, ctx, matched=True, outcome=outcome,
                    input_before=before, input_after=current_arguments,
                )
            )
            if outcome.decision == "deny":
                return outcome.reason.strip() or "工具调用被拦截", current_arguments
        return "", current_arguments

    async def _run_after_hooks(
        self,
        *,
        request: ToolExecutionRequest,
        current_arguments: dict[str, Any],
        output: Any,
        extra_messages: list[str],
        audit_metadata: dict[str, Any],
        traces: list[HookTraceItem],
    ) -> Any:
        for hook in self._hooks_for("after_call"):
            before = output
            ctx = self._context(
                event="after_call", request=request,
                arguments=current_arguments, result=output,
                audit_metadata=audit_metadata,
            )
            matched = self._matches(hook, ctx)
            if not matched:
                traces.append(
                    self._trace(
                        hook, ctx, matched=False,
                        output_before=before, output_after=before,
                    )
                )
                continue
            outcome = await self._run(hook, ctx)
            if outcome.output_updated:
                output = outcome.updated_output
            self._merge_outcome(outcome, extra_messages, audit_metadata)
            traces.append(
                self._trace(
                    hook, ctx, matched=True, outcome=outcome,
                    output_before=before, output_after=output,
                )
            )
        return output

    async def _run_error_hooks(
        self,
        *,
        request: ToolExecutionRequest,
        current_arguments: dict[str, Any],
        failure: ToolFailure,
        extra_messages: list[str],
        audit_metadata: dict[str, Any],
        traces: list[HookTraceItem],
    ) -> ToolFailure:
        for hook in self._hooks_for("on_error"):
            ctx = self._context(
                event="on_error", request=request,
                arguments=current_arguments, error=failure.message,
                error_code=str(failure.code), retryable=failure.retryable,
                audit_metadata=audit_metadata,
            )
            matched = self._matches(hook, ctx)
            if not matched:
                traces.append(self._trace(hook, ctx, matched=False))
                continue
            outcome = await self._run(hook, ctx)
            self._merge_outcome(outcome, extra_messages, audit_metadata)
            failure = ToolFailure(
                code=outcome.error_code or failure.code,
                message=failure.message,
                retryable=(
                    outcome.retryable
                    if outcome.retryable is not None
                    else failure.retryable
                ),
                exception_type=failure.exception_type,
            )
            traces.append(
                self._trace(
                    hook, ctx, matched=True, outcome=outcome,
                    error_code=str(failure.code), retryable=failure.retryable,
                )
            )
        return failure

    async def _run_cancel_hooks(
        self,
        *,
        request: ToolExecutionRequest,
        current_arguments: dict[str, Any],
        audit_metadata: dict[str, Any],
        traces: list[HookTraceItem],
    ) -> None:
        for hook in self._hooks_for("on_cancel"):
            ctx = self._context(
                event="on_cancel", request=request,
                arguments=current_arguments, error="asyncio task cancelled",
                error_code="cancelled", audit_metadata=audit_metadata,
            )
            try:
                matched = self._matches(hook, ctx)
                outcome = await self._run(hook, ctx) if matched else None
                if outcome is not None:
                    audit_metadata.update(outcome.audit_metadata)
                traces.append(self._trace(hook, ctx, matched=matched, outcome=outcome))
            except Exception:
                # Cancellation must keep propagating even if cleanup telemetry fails.
                continue

    def _hooks_for(self, event: str) -> list[ToolHook]:
        aliases = _EVENT_ALIASES[event]
        return [hook for hook in self._hooks if str(hook.event) in aliases]

    @staticmethod
    def _matches(hook: ToolHook, ctx: HookContext) -> bool:
        try:
            return bool(hook.matches(ctx))
        except Exception as exc:
            raise HookExecutionError(hook.name, str(hook.event), exc) from exc

    @staticmethod
    async def _run(hook: ToolHook, ctx: HookContext) -> HookOutcome:
        try:
            return await hook.run(ctx)
        except Exception as exc:
            raise HookExecutionError(hook.name, str(hook.event), exc) from exc

    @staticmethod
    def _context(
        *,
        event: HookEvent,
        request: ToolExecutionRequest,
        arguments: dict[str, Any],
        audit_metadata: dict[str, Any],
        result: Any = "",
        error: str = "",
        error_code: str = "",
        retryable: bool = False,
    ) -> HookContext:
        return HookContext(
            event=event, request=request,
            current_arguments=dict(arguments), result=result,
            error=error, error_code=error_code, retryable=retryable,
            attempt=request.attempt, audit_metadata=dict(audit_metadata),
        )

    @staticmethod
    def _merge_outcome(
        outcome: HookOutcome,
        extra_messages: list[str],
        audit_metadata: dict[str, Any],
    ) -> None:
        if outcome.extra_message:
            extra_messages.append(outcome.extra_message)
        audit_metadata.update(outcome.audit_metadata)

    @staticmethod
    def _trace(
        hook: ToolHook,
        ctx: HookContext,
        *,
        matched: bool,
        outcome: HookOutcome | None = None,
        input_before: dict[str, Any] | None = None,
        input_after: dict[str, Any] | None = None,
        output_before: Any = None,
        output_after: Any = None,
        error_code: str = "",
        retryable: bool | None = None,
    ) -> HookTraceItem:
        return HookTraceItem(
            hook_name=hook.name,
            event=ctx.event,
            matched=matched,
            decision=outcome.decision if outcome is not None else "pass",
            reason=str(redact_sensitive(outcome.reason)) if outcome else "",
            extra_message=(str(redact_sensitive(outcome.extra_message)) if outcome else ""),
            attempt=ctx.attempt,
            input_before=redact_sensitive(input_before or {}),
            input_after=redact_sensitive(input_after or {}),
            output_before=redact_sensitive(output_before),
            output_after=redact_sensitive(output_after),
            audit_metadata=redact_sensitive(outcome.audit_metadata if outcome else {}),
            error_code=error_code or ctx.error_code,
            retryable=retryable,
        )

    @staticmethod
    def _validate_input(validator: ToolValidator | None, arguments: dict[str, Any]) -> None:
        errors = validator(arguments) if validator is not None else []
        if errors:
            raise ToolInputValidationError("; ".join(errors))

    @staticmethod
    def _validate_output(validator: ToolValidator | None, output: Any) -> None:
        errors = validator(output) if validator is not None else []
        if errors:
            raise ToolOutputValidationError("; ".join(errors))
