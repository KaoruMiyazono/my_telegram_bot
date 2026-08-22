from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from agent.tool_hooks.base import ToolHook
from agent.tool_hooks.types import (
    HookContext,
    HookTraceItem,
    ToolExecutionRequest,
    ToolExecutionResult,
)

ToolInvoker = Callable[[str, dict[str, Any]], Awaitable[Any]]



class HookExecutionError(RuntimeError):
    def __init__(self, hook_name: str, event: str, cause: Exception) -> None:
        self.hook_name = hook_name
        self.event = event
        self.cause = cause
        super().__init__(f"hook {hook_name} ({event}) failed: {cause}")

#  工具执行器，负责在执行工具前后调用 hook hook就是插件提供的钩子函数，允许在工具执行前后进行自定义逻辑处理
class ToolExecutor:
    def __init__(self, hooks: Sequence[ToolHook] | None = None) -> None:
        self._hooks = list(hooks or [])

    def add_hooks(self, hooks: Sequence[ToolHook]) -> None:
        self._hooks.extend(hooks)

    async def execute(
        self,
        request: ToolExecutionRequest,
        invoker: ToolInvoker,
    ) -> ToolExecutionResult:
        current_arguments = dict(request.arguments)
        extra_messages: list[str] = []
        pre_trace: list[HookTraceItem] = []
        post_trace: list[HookTraceItem] = []

        try:
            denied_reason, current_arguments = await self._run_pre_hooks(
                request=request,
                current_arguments=current_arguments,
                extra_messages=extra_messages,
                traces=pre_trace,
            )
        except HookExecutionError as exc:
            return ToolExecutionResult(
                status="error",
                output=f"工具执行出错: {exc}",
                final_arguments=dict(current_arguments),
                extra_messages=extra_messages,
                pre_hook_trace=pre_trace,
                post_hook_trace=post_trace,
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
            )

        try:
            output = await invoker(request.tool_name, final_arguments)
        except Exception as exc:
            return ToolExecutionResult(
                status="error",
                output=f"工具执行出错: {exc}",
                final_arguments=final_arguments,
                extra_messages=extra_messages,
                pre_hook_trace=pre_trace,
                post_hook_trace=post_trace,
            )

        return ToolExecutionResult(
            status="success",
            output=output,
            final_arguments=final_arguments,
            extra_messages=extra_messages,
            pre_hook_trace=pre_trace,
            post_hook_trace=post_trace,
        )

    async def _run_pre_hooks(
        self,
        *,
        request: ToolExecutionRequest,
        current_arguments: dict[str, Any],
        extra_messages: list[str],
        traces: list[HookTraceItem],
    ) -> tuple[str, dict[str, Any]]:
        for hook in self._hooks:
            if hook.event != "pre_tool_use":
                continue
            ctx = HookContext(
                event="pre_tool_use",
                request=request,
                current_arguments=dict(current_arguments),
            )
            try:
                matched = hook.matches(ctx)
            except Exception as exc:
                raise HookExecutionError(hook.name, hook.event, exc) from exc
            if not matched:
                traces.append(HookTraceItem(hook.name, hook.event, matched=False))
                continue
            try:
                outcome = await hook.run(ctx)
            except Exception as exc:
                raise HookExecutionError(hook.name, hook.event, exc) from exc
            if outcome.updated_input is not None:
                current_arguments = dict(outcome.updated_input)
            if outcome.extra_message:
                extra_messages.append(outcome.extra_message)
            traces.append(
                HookTraceItem(
                    hook_name=hook.name,
                    event=hook.event,
                    matched=True,
                    decision=outcome.decision,
                    reason=outcome.reason,
                    extra_message=outcome.extra_message,
                )
            )
            if outcome.decision == "deny":
                return outcome.reason.strip() or "工具调用被拦截", current_arguments
        return "", current_arguments
