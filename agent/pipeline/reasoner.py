import asyncio
import json
import re
from collections.abc import Sequence
from copy import deepcopy

from openai import AsyncOpenAI

from agent.core.event_bus import EventBus
from agent.core.types import (
    AfterToolResultCtx,
    AfterStepCtx,
    BeforeReasoningCtx,
    BeforeStepCtx,
    BeforeToolCallCtx,
    ReasonerResult,
)
from agent.lifecycle.phase import (
    PhaseFrame,
    PhaseModuleRunner,
    append_string_exports,
    collect_prefixed_slots,
)
from agent.runtime.context_budget import (
    ContextBudget,
    ContextBudgetConfig,
    ContextBudgetExceeded,
    ContextLevel,
)
from agent.runtime.session_compaction import (
    SessionCompactionConfig,
    SessionContextCompactionError,
    SessionContextCompactor,
)
from agent.tool_hooks.executor import ToolExecutor
from agent.tools.registry import ToolRegistry
from agent.tools.runtime import ToolRuntime, unwrap_tool_envelope
from config.settings import settings


_MAX_LLM_ITERATIONS = 4
_EVIDENCE_SOURCE_TOOLS = {"recall_memory", "search_messages"}
_RAW_SEARCH_GUARD_KEYWORDS = (
    "现在",
    "以前",
    "之前",
    "曾经",
    "全部",
    "变化",
    "更新",
    "后来",
    "还",
    "还是",
    "不再",
    "戒",
    "改",
    "换",
    "项目",
    "擅长",
    "技术栈",
)
_MEMORY_GUARD_KEYWORDS = (
    "我",
    "我的",
    "给我",
    "推荐",
    "建议",
    "喜欢",
    "偏好",
    "记得",
    "以前",
    "之前",
    "上次",
    "后来",
    "现在",
    "做过",
    "用什么",
    "是什么",
    "喝什么",
    "吃什么",
)
_SEARCH_DOMAIN_TERMS = (
    "咖啡",
    "拿铁",
    "茶",
    "饮料",
    "偏好",
    "生日",
    "公司",
    "城市",
    "居住",
    "工作",
    "手机",
    "iPhone",
    "Android",
    "Python",
    "FastAPI",
    "Rust",
    "后端",
    "工程师",
    "技术栈",
    "项目",
    "音乐",
    "爵士",
    "古典",
    "摇滚",
)
_SEARCH_ACTION_TERMS = (
    "喜欢",
    "常用",
    "擅长",
    "戒",
    "改喝",
    "换",
    "推荐",
    "建议",
    "现在",
    "以前",
)


class Reasoner:
    """LLM 调用器，管理 DeepSeek API 调用和 tool call 循环"""

    def __init__(
        self,
        tool_registry: ToolRegistry | None = None,
        tool_executor: ToolExecutor | None = None,
        event_bus: EventBus | None = None,
        before_step_modules: Sequence[object] | None = None,
        after_step_modules: Sequence[object] | None = None,
        context_budget: ContextBudget | None = None,
        session_compaction_config: SessionCompactionConfig | None = None,
    ) -> None:
        #  LLM client for reasoning and tool call orchestration
        self.client = AsyncOpenAI(
            api_key=settings.DEEPSEEK_API_KEY,
            base_url=settings.DEEPSEEK_BASE_URL,
            timeout=60.0,
        )
        # 说明选择哪个模型
        self.model = settings.LLM_MODEL
        self._tool_registry = tool_registry
        self._tool_executor = tool_executor or ToolExecutor()
        self._tool_runtime = ToolRuntime(
            registry=self._tool_registry,
            executor=self._tool_executor,
        )
        self._event_bus = event_bus or EventBus.get_instance()
        # Explicit injection keeps the former L0-L4 projector available for
        # compatibility tests. Production uses the persistent session ledger.
        self._legacy_context_budget = context_budget
        self._session_compaction_config = session_compaction_config or SessionCompactionConfig(
            context_window=settings.LLM_CONTEXT_WINDOW,
            output_reserve=settings.LLM_OUTPUT_RESERVE,
            soft_limit_ratio=settings.LLM_CONTEXT_SOFT_LIMIT_RATIO,
            keep_recent_tokens=settings.LLM_CONTEXT_KEEP_RECENT_TOKENS,
            summary_max_tokens=settings.LLM_COMPACTION_SUMMARY_MAX_TOKENS,
        )
        self.set_step_modules(
            before_step=before_step_modules,
            after_step=after_step_modules,
        )

    def set_step_modules(
        self,
        *,
        before_step: Sequence[object] | None = None,
        after_step: Sequence[object] | None = None,
    ) -> None:
        self._before_step_modules = list(before_step or [])
        self._after_step_modules = list(after_step or [])

    def add_tool_hooks(self, hooks) -> None:
        self._tool_executor.add_hooks(hooks)

    async def close(self) -> None:
        await self.client.close()

    async def _execute_tool(
        self,
        tool_name: str,
        arguments: dict | str,
        ctx: BeforeReasoningCtx,
        *,
        call_id: str = "",
        tool_batch: tuple[dict, ...] = (),
        tool_batch_index: int = 0,
        enforce_visibility: bool = True,
    ) -> str:
        """Execute a tool call. 工具结果以 JSON 字符串返回给 LLM。"""
        session_key, channel, chat_id = _tool_context(ctx)
        observed_args = arguments if isinstance(arguments, dict) else _load_json_object(arguments)
        await self._event_bus.observe(
            BeforeToolCallCtx(
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
                tool_name=tool_name,
                arguments=dict(observed_args),
            )
        )

        runtime_result = await self._tool_runtime.execute_call(
            call_id=call_id,
            tool_name=tool_name,
            raw_arguments=arguments,
            ctx=ctx,
            source="passive",
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            request_text=ctx.content,
            tool_batch=tool_batch,
            tool_batch_index=tool_batch_index,
            turn_id=ctx.turn_id,
            trace_id=ctx.trace_id,
            allowed_tool_names=(
                frozenset(
                    str(schema.get("function", {}).get("name", ""))
                    for schema in ctx.tools
                    if schema.get("function", {}).get("name")
                )
                if enforce_visibility
                else None
            ),
        )
        if runtime_result.ok and self._tool_registry is not None:
            self._tool_registry.remember_tool_use(session_key, tool_name)
        result = runtime_result.to_json()
        await self._event_bus.observe(
            AfterToolResultCtx(
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
                tool_name=tool_name,
                arguments=runtime_result.final_arguments,
                result=result,
                status=runtime_result.status,
            )
        )
        return result

    async def _observe_tool_result(
        self,
        ctx: BeforeReasoningCtx,
        tool_name: str,
        arguments: dict,
        result: str,
        status: str,
    ) -> None:
        session_key, channel, chat_id = _tool_context(ctx)
        await self._event_bus.observe(
            AfterToolResultCtx(
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
                tool_name=tool_name,
                arguments=dict(arguments),
                result=result,
                status=status,
            )
        )

    async def run_turn(self, ctx: BeforeReasoningCtx) -> ReasonerResult:
        """Run a reasoning turn with potential tool calls."""
        messages = ctx.messages.copy()
        tool_calls: list[dict] = []
        context_trace: list[dict[str, object]] = []
        compactor = (
            None
            if self._legacy_context_budget is not None
            else SessionContextCompactor(
                user_id=ctx.session.user_id,
                chat_id=ctx.session.chat_id,
                session_messages=ctx.session.messages,
                model=self.model,
                summary_builder=self._build_session_compaction_summary,
                config=self._session_compaction_config,
            )
        )

        for iteration in range(_MAX_LLM_ITERATIONS):
            step_ctx = await self._run_before_step(ctx, iteration, messages)
            if step_ctx.early_stop:
                return ReasonerResult(
                    content=step_ctx.early_stop_reply or "",
                    tool_calls=tool_calls,
                    finish_reason="early_stop",
                    turn_id=ctx.turn_id,
                    trace_id=ctx.trace_id,
                    context_trace=list(context_trace),
                )
            if step_ctx.extra_hints:
                messages.append(
                    {
                        "role": "system",
                        "content": "# Step Hints\n" + "\n".join(step_ctx.extra_hints),
                    }
                )
            if self._legacy_context_budget is not None:
                response = await self._call_with_legacy_context_budget(
                    ctx=ctx,
                    messages=messages,
                    context_trace=context_trace,
                    iteration=iteration,
                )
            else:
                assert compactor is not None
                response = await self._call_with_session_compaction(
                    ctx=ctx,
                    messages=messages,
                    context_trace=context_trace,
                    compactor=compactor,
                    iteration=iteration,
                )
            if response is None:
                if iteration == 0:
                    await asyncio.sleep(0.5)
                continue

            choice = response.choices[0]
            message = choice.message

            # Check for tool calls
            if message.tool_calls:
                iteration_tool_names = tuple(tc.function.name for tc in message.tool_calls)
                tool_calls.extend([
                    {
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in message.tool_calls
                ])

                # Add assistant message with tool calls
                messages.append({
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": tc.type,
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in message.tool_calls
                    ],
                })

                # Execute tools and add responses
                for idx, tc in enumerate(message.tool_calls):
                    tool_batch = _tool_call_batch_snapshot(message.tool_calls)
                    result = await self._execute_tool(
                        tc.function.name,
                        tc.function.arguments,
                        ctx,
                        call_id=tc.id,
                        tool_batch=tool_batch,
                        tool_batch_index=idx,
                    )
                    call_record = tool_calls[len(tool_calls) - len(message.tool_calls) + idx]
                    call_record["result"] = result
                    _annotate_tool_call_from_runtime(call_record, result)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })

                #  只是执行了recall，看看是否需要返回原文查询
                guard_tools = await self._run_raw_search_guard(
                    ctx=ctx,
                    messages=messages,
                    tool_calls=tool_calls,
                )
                guard_tools.extend(
                    await self._run_evidence_fetch_guard(
                        ctx=ctx,
                        messages=messages,
                        tool_calls=tool_calls,
                    )
                )
                await self._run_after_step(
                    ctx=ctx,
                    iteration=iteration,
                    tools_called=iteration_tool_names + tuple(guard_tools),
                    partial_reply=message.content or "",
                    tool_calls=tool_calls,
                    has_more=True,
                )
                # Continue loop for LLM to respond with final answer
                continue
            else:
                #  本次Guard的工具列表，只保留工具名称
                guard_tools: list[str] = []
                #  如果这个问题里面包含我的，我这些词语，证明这可能需要检索记忆。 这里就是判断之前是否检索过，没有检索过强制recall记忆
                recall_guard_tools = await self._run_explicit_recall_guard(
                    ctx=ctx,
                    messages=messages,
                    tool_calls=tool_calls,
                )
                if recall_guard_tools:
                    guard_tools.extend(recall_guard_tools)
                #  如果这个问题里面包含现在、以前、之前、曾经、全部、变化、更新、后来、还、还是、不再、戒、改、换这些词语，证明这可能需要检索记忆。 这里就是判断之前是否检索过，没有检索过强制raw search，搜索原文，返回的是截断后的原文预览
                #  本质上上一层是线索，这一层是原始记忆的摘要，或者定位器械
                search_guard_tools = await self._run_raw_search_guard(
                    ctx=ctx,
                    messages=messages,
                    tool_calls=tool_calls,
                )
                if search_guard_tools:
                    guard_tools.extend(search_guard_tools)
                #  回去找原文
                evidence_guard_tools = await self._run_evidence_fetch_guard(
                    ctx=ctx,
                    messages=messages,
                    tool_calls=tool_calls,
                )
                if evidence_guard_tools:
                    guard_tools.extend(
                        evidence_guard_tools
                    )
                if guard_tools:
                    #  如果有Guard工具被触发，继续循环，让LLM生成最终答案
                    #  这里的run_after_step是一个钩子函数，主要是记录当前的迭代次数、调用的工具、部分回复和工具调用的结果，方便后续的调试和分析
                    await self._run_after_step(
                        ctx=ctx,
                        iteration=iteration,
                        tools_called=tuple(guard_tools),
                        partial_reply=message.content or "",
                        tool_calls=tool_calls,
                        has_more=True,
                    )
                    continue
                await self._run_after_step(
                    ctx=ctx,
                    iteration=iteration,
                    tools_called=(),
                    partial_reply=message.content or "",
                    tool_calls=tool_calls,
                    has_more=False,
                )
                # No tool calls, this is the final response
                #  这个是工程改进，就是在开头加上一段话
                content = _apply_final_answer_guard(
                    ctx,
                    message.content or "",
                    tool_calls,
                )
                return ReasonerResult(
                    content=content,
                    tool_calls=tool_calls,
                    finish_reason=choice.finish_reason or "stop",
                    turn_id=ctx.turn_id,
                    trace_id=ctx.trace_id,
                    context_trace=list(context_trace),
                )

        # Max iterations reached
        return ReasonerResult(
            content="抱歉，处理请求时遇到问题。",
            tool_calls=tool_calls,
            finish_reason="max_iterations",
            turn_id=ctx.turn_id,
            trace_id=ctx.trace_id,
            context_trace=list(context_trace),
        )

    async def _call_with_legacy_context_budget(
        self,
        *,
        ctx: BeforeReasoningCtx,
        messages: list[dict],
        context_trace: list[dict[str, object]],
        iteration: int,
    ):
        budget = self._legacy_context_budget
        assert budget is not None
        minimum_level = ContextLevel.COMPLETE
        while True:
            projection = budget.project(
                messages,
                tools=ctx.tools,
                prompt_sections=ctx.prompt_sections,
                source_ref=f"session:{ctx.session.user_id}:{ctx.session.chat_id}",
                min_level=minimum_level,
            )
            context_trace.append(projection.trace.to_dict())
            try:
                request_kwargs = {
                    "model": self.model,
                    "messages": projection.messages,
                    "tools": projection.tools,
                }
                if budget.config.output_reserve > 0:
                    request_kwargs["max_tokens"] = budget.config.output_reserve
                return await self.client.chat.completions.create(**request_kwargs)
            except Exception as error:
                if _is_context_length_error(error) and projection.trace.level < ContextLevel.MINIMAL_SAFE:
                    minimum_level = ContextLevel(int(projection.trace.level) + 1)
                    continue
                if _is_context_length_error(error):
                    raise ContextBudgetExceeded(
                        "Provider rejected the minimal safe context"
                    ) from error
                if iteration > 0:
                    raise
                return None

    async def _call_with_session_compaction(
        self,
        *,
        ctx: BeforeReasoningCtx,
        messages: list[dict],
        context_trace: list[dict[str, object]],
        compactor: SessionContextCompactor,
        iteration: int,
    ):
        prepared = await compactor.prepare(messages, tools=ctx.tools)
        context_trace.append(prepared.trace)
        forced = False
        while True:
            request_kwargs = {
                "model": self.model,
                "messages": deepcopy(messages),
                # Snapshot the visible schemas for this provider call. tool_search
                # may mutate ctx.tools while the ReAct loop is still running.
                "tools": deepcopy(ctx.tools),
            }
            if self._session_compaction_config.output_reserve > 0:
                request_kwargs["max_tokens"] = self._session_compaction_config.output_reserve
            try:
                return await self.client.chat.completions.create(**request_kwargs)
            except Exception as error:
                if _is_context_length_error(error) and not forced:
                    prepared = await compactor.prepare(
                        messages,
                        tools=ctx.tools,
                        trigger="context_overflow",
                        force=True,
                    )
                    context_trace.append(prepared.trace)
                    forced = True
                    continue
                if _is_context_length_error(error):
                    raise SessionContextCompactionError(
                        "Provider rejected context after forced compaction"
                    ) from error
                if iteration > 0:
                    raise
                return None

    async def _build_session_compaction_summary(
        self,
        prompt: str,
        max_tokens: int,
    ) -> tuple[str, dict[str, object] | None]:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            max_tokens=max_tokens,
        )
        content = str(response.choices[0].message.content or "").strip()
        usage = getattr(response, "usage", None)
        if usage is None:
            usage_data = None
        elif hasattr(usage, "model_dump"):
            usage_data = dict(usage.model_dump())
        else:
            usage_data = {
                key: value
                for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                if (value := getattr(usage, key, None)) is not None
            }
        return content, usage_data

    async def _run_evidence_fetch_guard(
        self,
        *,
        ctx: BeforeReasoningCtx,
        messages: list[dict],
        tool_calls: list[dict],
    ) -> list[str]:
        if not _tool_is_visible(ctx, "fetch_messages"):
            return []
        if self._tool_registry is None or not self._tool_registry.has_tool("fetch_messages"):
            return []
        source_refs = _pending_evidence_source_refs(tool_calls)
        if not source_refs:
            return []

        args = {
            "source_refs": source_refs[:5],
            "context": 2,
            "limit": 20,
        }
        call_id = f"guard_fetch_{len(tool_calls) + 1}"
        arguments = json.dumps(args, ensure_ascii=False)
        tool_calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "fetch_messages",
                    "arguments": arguments,
                },
                "guard": "source_ref_requires_fetch",
            }
        )
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "fetch_messages",
                            "arguments": arguments,
                        },
                    }
                ],
            }
        )
        result = await self._execute_tool(
            "fetch_messages", args, ctx, enforce_visibility=False
        )
        tool_calls[-1]["result"] = result
        _annotate_tool_call_from_runtime(tool_calls[-1], result)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": result,
            }
        )
        messages.append(
            {
                "role": "system",
                "content": (
                    "# Evidence Guard\n"
                    "fetch_messages 已读取 source_ref 原文。最终回答必须基于这些原文证据；"
                    "如果证据不足，明确说明不足。"
                    "若本轮 recall_memory 返回了 active 与 superseded 记忆，"
                    "active 代表当前有效事实，superseded 代表被替代的历史事实；"
                    "当前状态、推荐、偏好、身份类问题必须优先使用 active，"
                    "只有用户明确问以前/历史/变化过程时才使用 superseded 作为结论。"
                ),
            }
        )
        return ["fetch_messages"]

    async def _run_explicit_recall_guard(
        self,
        *,
        ctx: BeforeReasoningCtx,
        messages: list[dict],
        tool_calls: list[dict],
    ) -> list[str]:
        if not _should_force_explicit_recall(ctx, tool_calls):
            return []
        if self._tool_registry is None or not self._tool_registry.has_tool("recall_memory"):
            return []

        args = _recall_guard_args(ctx)
        call_id = f"guard_recall_{len(tool_calls) + 1}"
        arguments = json.dumps(args, ensure_ascii=False)
        tool_calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "recall_memory",
                    "arguments": arguments,
                },
                "guard": "passive_memory_requires_explicit_recall",
            }
        )
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "recall_memory",
                            "arguments": arguments,
                        },
                    }
                ],
            }
        )
        result = await self._execute_tool(
            "recall_memory", args, ctx, enforce_visibility=False
        )
        tool_calls[-1]["result"] = result
        _annotate_tool_call_from_runtime(tool_calls[-1], result)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": result,
            }
        )
        messages.append(
            {
                "role": "system",
                "content": (
                    "# Memory Guard\n"
                    "系统检测到本轮回答正在使用已注入的历史记忆。"
                    "已补充一次 recall_memory；最终回答必须以显式检索结果为准。"
                ),
            }
        )
        return ["recall_memory"]

    async def _run_raw_search_guard(
        self,
        *,
        ctx: BeforeReasoningCtx,
        messages: list[dict],
        tool_calls: list[dict],
    ) -> list[str]:
        if not _should_force_raw_search(ctx, tool_calls):
            return []
        if self._tool_registry is None or not self._tool_registry.has_tool("search_messages"):
            return []

        args = {
            "query": _search_guard_query(ctx, tool_calls),
            "role": "user",
            "limit": 10,
        }
        call_id = f"guard_search_{len(tool_calls) + 1}"
        arguments = json.dumps(args, ensure_ascii=False)
        tool_calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "search_messages",
                    "arguments": arguments,
                },
                "guard": "recall_requires_raw_search",
            }
        )
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "search_messages",
                            "arguments": arguments,
                        },
                    }
                ],
            }
        )
        result = await self._execute_tool(
            "search_messages", args, ctx, enforce_visibility=False
        )
        tool_calls[-1]["result"] = result
        _annotate_tool_call_from_runtime(tool_calls[-1], result)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": result,
            }
        )
        messages.append(
            {
                "role": "system",
                "content": (
                    "# Raw Search Guard\n"
                    "本轮问题涉及历史更新、变化过程或基于用户长期信息的建议。"
                    "已补充 search_messages 定位原始消息；若返回 source_ref，必须回源取证后回答。"
                ),
            }
        )
        return ["search_messages"]

    async def _run_before_step(
        self,
        ctx: BeforeReasoningCtx,
        iteration: int,
        messages: list[dict],
    ) -> BeforeStepCtx:
        session_key, channel, chat_id = _tool_context(ctx)
        visible_tool_names = frozenset(
            str(tool.get("function", {}).get("name", ""))
            for tool in ctx.tools
            if tool.get("function", {}).get("name")
        )
        step_ctx = BeforeStepCtx(
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            iteration=iteration,
            input_tokens_estimate=_estimate_message_tokens(messages),
            visible_tool_names=visible_tool_names,
        )
        plugin_runner = PhaseModuleRunner(
            self._before_step_modules,
            phase_name="before_step",
        )
        frame = PhaseFrame(
            input=messages,
            slots={
                "step:ctx": step_ctx,
                "before_step.build_ctx": True,
            },
        )
        frame = await plugin_runner.run_ready(frame)
        step_ctx = frame.slots.get("step:ctx", step_ctx)
        emitted = await self._event_bus.emit(step_ctx)
        if emitted is not None:
            step_ctx = emitted
        frame.slots["step:ctx"] = step_ctx
        frame.slots["before_step.emit"] = True
        frame = await plugin_runner.run_ready(frame)
        step_ctx = frame.slots.get("step:ctx", step_ctx)
        append_string_exports(
            step_ctx.extra_hints,
            collect_prefixed_slots(frame.slots, "step:extra_hint:"),
        )
        frame.slots["before_step.collect_exports"] = True
        abort_reply = frame.slots.get("step:abort_reply")
        if isinstance(abort_reply, str) and abort_reply:
            step_ctx.early_stop = True
            step_ctx.early_stop_reply = abort_reply
        frame.slots["before_step.inject_hints"] = True
        frame.slots["before_step.return"] = True
        plugin_runner.warn_unresolved()
        return step_ctx

    async def _run_after_step(
        self,
        *,
        ctx: BeforeReasoningCtx,
        iteration: int,
        tools_called: tuple[str, ...],
        partial_reply: str,
        tool_calls: list[dict],
        has_more: bool,
    ) -> AfterStepCtx:
        session_key, channel, chat_id = _tool_context(ctx)
        tools_used = tuple(
            call.get("function", {}).get("name", "")
            for call in tool_calls
            if call.get("function", {}).get("name")
        )
        step_ctx = AfterStepCtx(
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            iteration=iteration,
            tools_called=tools_called,
            partial_reply=partial_reply,
            tools_used_so_far=tools_used,
            tool_chain_partial=tuple(tool_calls),
            partial_thinking=None,
            has_more=has_more,
        )
        plugin_runner = PhaseModuleRunner(
            self._after_step_modules,
            phase_name="after_step",
        )
        frame = PhaseFrame(
            input=step_ctx,
            slots={
                "step:ctx": step_ctx,
                "after_step.copy_input": True,
            },
        )
        frame = await plugin_runner.run_ready(frame)
        step_ctx = frame.slots.get("step:ctx", step_ctx)
        step_ctx.extra_metadata.update(
            collect_prefixed_slots(frame.slots, "step:telemetry:")
        )
        await self._event_bus.observe(step_ctx)
        frame.slots["step:ctx"] = step_ctx
        frame.slots["after_step.fanout"] = True
        frame = await plugin_runner.run_ready(frame)
        collected = set(collect_prefixed_slots(frame.slots, "step:telemetry:"))
        step_ctx = frame.slots.get("step:ctx", step_ctx)
        late_telemetry = collect_prefixed_slots(frame.slots, "step:telemetry:")
        for key, value in late_telemetry.items():
            if key not in collected:
                step_ctx.extra_metadata[key] = value
        frame.slots["after_step.collect_telemetry"] = True
        frame.slots["after_step.return"] = True
        plugin_runner.warn_unresolved()
        return step_ctx


def _tool_call_batch_snapshot(tool_calls) -> tuple[dict, ...]:
    batch: list[dict] = []
    for tool_call in tool_calls:
        batch.append(
            {
                "name": str(tool_call.function.name),
                "arguments": _load_json_object(str(tool_call.function.arguments or "")),
            }
        )
    return tuple(batch)


def _annotate_tool_call_from_runtime(call: dict, result: str) -> None:
    envelope = _load_raw_json_object(result)
    if not envelope:
        return
    call["status"] = str(envelope.get("status") or "")
    error = envelope.get("error")
    if isinstance(error, dict):
        call["error_code"] = str(error.get("code") or "")
    meta = envelope.get("meta")
    if isinstance(meta, dict):
        final_arguments = meta.get("final_arguments")
        if isinstance(final_arguments, dict):
            call["final_arguments"] = final_arguments
        retry_count = meta.get("retry_count")
        if retry_count is not None:
            call["retry_count"] = retry_count


def _tool_context(ctx: BeforeReasoningCtx) -> tuple[str, str, str]:
    session_key = ctx.session_key or ctx.session.session_key or (
        f"{ctx.channel or 'telegram'}:{ctx.session.chat_id}:{ctx.session.user_id}"
    )
    channel = ctx.channel or "telegram"
    chat_id = ctx.chat_id or str(ctx.session.chat_id)
    return session_key, channel, chat_id


def _tool_is_visible(ctx: BeforeReasoningCtx, tool_name: str) -> bool:
    return any(
        str(tool.get("function", {}).get("name", "")) == tool_name
        for tool in ctx.tools
    )


def _should_force_explicit_recall(ctx: BeforeReasoningCtx, tool_calls: list[dict]) -> bool:
    if not _tool_is_visible(ctx, "recall_memory"):
        return False
    if any(
        str(call.get("function", {}).get("name", "")) == "recall_memory"
        for call in tool_calls
    ):
        return False
    if not _has_memory_context(ctx):
        return False
    content = _last_user_content(ctx)
    if not content:
        return False
    return any(keyword in content for keyword in _MEMORY_GUARD_KEYWORDS)


def _has_memory_context(ctx: BeforeReasoningCtx) -> bool:
    if ctx.memories or str(ctx.retrieved_memory_block or "").strip():
        return True
    return any(
        section.name in {"long_term_memory", "self_model", "recent_context"}
        and str(section.content or "").strip()
        for section in ctx.prompt_sections
    )


def _should_force_raw_search(ctx: BeforeReasoningCtx, tool_calls: list[dict]) -> bool:
    if not _tool_is_visible(ctx, "search_messages"):
        return False
    names = [
        str(call.get("function", {}).get("name", ""))
        for call in tool_calls
    ]
    if "search_messages" in names or "recall_memory" not in names:
        return False
    content = _last_user_content(ctx)
    if not content:
        return False
    return any(keyword in content for keyword in _RAW_SEARCH_GUARD_KEYWORDS)


def _recall_guard_args(ctx: BeforeReasoningCtx) -> dict:
    args: dict[str, object] = {
        "query": _recall_guard_query(ctx),
        "limit": 5,
    }
    memory_type = _dominant_memory_type(ctx)
    if memory_type:
        args["memory_type"] = memory_type
    if _asks_for_history_or_updates(_last_user_content(ctx)):
        args["include_superseded"] = True
    return args


def _recall_guard_query(ctx: BeforeReasoningCtx) -> str:
    content = _last_user_content(ctx)
    summaries = [
        str(getattr(memory, "summary", "") or "").strip()
        for memory in ctx.memories[:3]
    ]
    summaries = [summary for summary in summaries if summary]
    if summaries:
        return f"用户当前问题相关的长期记忆：{content}。已检索线索：{'；'.join(summaries)}"
    return f"用户当前问题相关的长期记忆：{content}"


def _dominant_memory_type(ctx: BeforeReasoningCtx) -> str:
    memory_types = [
        str(getattr(memory, "memory_type", "") or "").strip()
        for memory in ctx.memories
    ]
    memory_types = [memory_type for memory_type in memory_types if memory_type]
    if not memory_types:
        return ""
    first = memory_types[0]
    if all(memory_type == first for memory_type in memory_types):
        return first
    return ""


def _asks_for_history_or_updates(content: str) -> bool:
    return any(
        keyword in content
        for keyword in ("以前", "之前", "上次", "曾经", "后来", "变化", "更新", "现在")
    )


def _last_user_content(ctx: BeforeReasoningCtx) -> str:
    for message in reversed(ctx.messages):
        if message.get("role") == "user":
            content = message.get("content", "")
            if isinstance(content, str) and content.strip():
                return content.strip()
    return str(ctx.content or "").strip()


def _search_guard_query(ctx: BeforeReasoningCtx, tool_calls: list[dict]) -> str:
    haystack = _search_guard_haystack(ctx, tool_calls)
    terms: list[str] = []
    haystack_lower = haystack.lower()
    for term in _SEARCH_DOMAIN_TERMS:
        if term.lower() in haystack_lower:
            _append_search_term(terms, term)
    for term in _SEARCH_ACTION_TERMS:
        if term.lower() in haystack_lower:
            _append_search_term(terms, term)
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_+.-]*|\d{2,4}", haystack):
        _append_search_term(terms, token)
    if terms:
        return " ".join(terms[:10])
    content = _last_user_content(ctx)
    return content or haystack[:80]


def _search_guard_haystack(ctx: BeforeReasoningCtx, tool_calls: list[dict]) -> str:
    parts = [_last_user_content(ctx)]
    parts.extend(
        str(getattr(memory, "summary", "") or "").strip()
        for memory in ctx.memories
    )
    for call in tool_calls:
        if str(call.get("function", {}).get("name", "")) != "recall_memory":
            continue
        payload = _load_json_object(str(call.get("result", "") or ""))
        items = payload.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                parts.append(str(item.get("summary", "") or "").strip())
    return " ".join(part for part in parts if part)


def _append_search_term(terms: list[str], value: str) -> None:
    term = str(value or "").strip()
    if term and term not in terms:
        terms.append(term)


def _apply_final_answer_guard(
    ctx: BeforeReasoningCtx,
    content: str,
    tool_calls: list[dict],
) -> str:
    reply = str(content or "").strip()
    if not reply:
        return reply
    if not _is_memory_grounded(tool_calls):
        return reply
    user_content = _last_user_content(ctx)
    if not any(keyword in user_content for keyword in ("推荐", "建议")):
        return reply
    evidence_reply = _project_suggestion_from_evidence(user_content, tool_calls)
    if evidence_reply:
        return evidence_reply
    if any(keyword in reply for keyword in ("根据", "喜好", "偏好", "擅长", "技术栈")):
        return reply
    if any(keyword in user_content for keyword in ("项目", "技术栈", "擅长")):
        return f"根据你擅长的技术栈，{reply}"
    return f"根据你的喜好，{reply}"


def _is_memory_grounded(tool_calls: list[dict]) -> bool:
    names = {
        str(call.get("function", {}).get("name", ""))
        for call in tool_calls
    }
    return "recall_memory" in names or "fetch_messages" in names


def _project_suggestion_from_evidence(user_content: str, tool_calls: list[dict]) -> str:
    if not any(keyword in user_content for keyword in ("项目", "技术栈", "擅长")):
        return ""
    evidence_text = " ".join(_fetched_message_contents(tool_calls))
    if "Python" not in evidence_text:
        return ""
    frameworks = [
        name
        for name in ("Django", "FastAPI")
        if name in evidence_text
    ]
    if not frameworks:
        return ""
    if len(frameworks) == 1:
        framework_text = frameworks[0]
    else:
        framework_text = " 或 ".join(frameworks)
    return f"根据你擅长的技术栈，你可以用 Python 的 {framework_text} 框架来做后端。"


def _fetched_message_contents(tool_calls: list[dict]) -> list[str]:
    contents: list[str] = []
    for call in tool_calls:
        if str(call.get("function", {}).get("name", "")) != "fetch_messages":
            continue
        payload = _load_json_object(str(call.get("result", "") or ""))
        messages = payload.get("messages")
        if not isinstance(messages, list):
            continue
        for message in messages:
            if isinstance(message, dict):
                content = str(message.get("content", "") or "").strip()
                if content:
                    contents.append(content)
    return contents


def _pending_evidence_source_refs(tool_calls: list[dict]) -> list[str]:
    fetched_refs: list[str] = []
    evidence_refs: list[str] = []
    for call in tool_calls:
        name = str(call.get("function", {}).get("name", ""))
        result = str(call.get("result", "") or "")
        arguments = _load_json_object(
            str(call.get("function", {}).get("arguments", "") or "")
        )
        if name == "fetch_messages":
            fetched_refs.extend(_source_refs_from_args(arguments))
            fetched_refs.extend(_source_refs_from_payload(result))
            continue
        if name in _EVIDENCE_SOURCE_TOOLS:
            evidence_refs.extend(_source_refs_from_payload(result))

    return [
        ref
        for ref in _dedupe_refs(evidence_refs)
        if not _is_ref_fetched(ref, fetched_refs)
    ]


def _source_refs_from_args(arguments: dict) -> list[str]:
    refs: list[str] = []
    _append_ref(refs, arguments.get("source_ref"))
    raw_refs = arguments.get("source_refs")
    if isinstance(raw_refs, list):
        for ref in raw_refs:
            _append_ref(refs, ref)
    else:
        _append_ref(refs, raw_refs)
    return _dedupe_refs(refs)


def _source_refs_from_payload(raw: str) -> list[str]:
    payload = _load_json_object(raw)
    refs: list[str] = []
    _append_ref(refs, payload.get("source_ref"))
    raw_refs = payload.get("source_refs")
    if isinstance(raw_refs, list):
        for ref in raw_refs:
            _append_ref(refs, ref)
    else:
        _append_ref(refs, raw_refs)
    for key in ("items", "messages"):
        values = payload.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, dict):
                _append_ref(refs, value.get("source_ref"))
    return _dedupe_refs(refs)


def _load_json_object(raw: str) -> dict:
    payload = _load_raw_json_object(raw)
    return unwrap_tool_envelope(payload) if payload else {}


def _load_raw_json_object(raw: str) -> dict:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _append_ref(refs: list[str], value) -> None:
    ref = str(value or "").strip()
    if ref:
        refs.append(ref)


def _dedupe_refs(refs: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for ref in refs:
        if ref in seen:
            continue
        seen.add(ref)
        deduped.append(ref)
    return deduped


def _is_ref_fetched(source_ref: str, fetched_refs: list[str]) -> bool:
    for fetched in fetched_refs:
        if source_ref == fetched:
            return True
        if "#" not in fetched and source_ref.startswith(fetched + "#"):
            return True
    return False


def _estimate_message_tokens(messages: list[dict]) -> int:
    total_chars = 0
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
    return max(1, total_chars // 3)


def _is_context_length_error(error: BaseException) -> bool:
    """Recognize provider-specific context overflow messages."""

    text = str(error).lower()
    markers = (
        "context length",
        "context_length_exceeded",
        "maximum context",
        "max context",
        "too many tokens",
        "token limit",
        "上下文长度",
    )
    return any(marker in text for marker in markers)
