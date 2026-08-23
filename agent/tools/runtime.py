from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from agent.tool_hooks.errors import ToolRuntimeTimeoutError
from agent.tool_hooks.executor import ToolExecutor
from agent.tool_hooks.redaction import redact_sensitive
from agent.tool_hooks.types import HookTraceItem, ToolExecutionRequest
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolMeta, ToolRegistry

ToolRuntimeStatus = Literal["success", "denied", "error"]
ToolRuntimeErrorCode = Literal[
    "argument_parse",
    "tool_lookup",
    "tool_locked",
    "input_validation",
    "policy_check",
    "tool_invoke",
    "timeout",
    "network_error",
    "mcp_unavailable",
    "business_error",
    "cancelled",
    "hook_error",
    "output_validation",
    "unknown",
]


@dataclass(frozen=True)
class ToolRetryPolicy:
    max_retries: int = 0
    backoff_s: float = 0.0


@dataclass(frozen=True)
class ToolRuntimeConfig:
    default_timeout_s: float = 30.0
    read_only_max_retries: int = 1
    retry_backoff_s: float = 0.05


@dataclass
class ToolRuntimeResult:
    ok: bool
    status: ToolRuntimeStatus
    tool_name: str
    call_id: str = ""
    data: Any = None
    data_text: str = ""
    error_code: ToolRuntimeErrorCode | str = ""
    message: str = ""
    retryable: bool = False
    retry_count: int = 0
    duration_ms: int = 0
    arguments: dict[str, Any] = field(default_factory=dict)
    final_arguments: dict[str, Any] = field(default_factory=dict)
    extra_messages: list[str] = field(default_factory=list)
    pre_hook_trace: list[HookTraceItem] = field(default_factory=list)
    post_hook_trace: list[HookTraceItem] = field(default_factory=list)
    error_hook_trace: list[HookTraceItem] = field(default_factory=list)
    cancel_hook_trace: list[HookTraceItem] = field(default_factory=list)
    audit_metadata: dict[str, Any] = field(default_factory=dict)
    exception_type: str = ""
    turn_id: str = ""
    trace_id: str = ""

    def to_envelope(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "status": self.status,
            "data": redact_sensitive(self.data) if self.ok else None,
            "error": None,
            "meta": {
                "tool_name": self.tool_name,
                "call_id": self.call_id,
                "duration_ms": self.duration_ms,
                "retry_count": self.retry_count,
                "final_arguments": redact_sensitive(self.final_arguments),
                "turn_id": self.turn_id,
                "trace_id": self.trace_id,
            },
        }
        if self.data_text:
            payload["data_text"] = redact_sensitive(self.data_text)
        if self.extra_messages:
            payload["meta"]["extra_messages"] = redact_sensitive(self.extra_messages)
        if self.pre_hook_trace:
            payload["meta"]["pre_hook_trace"] = _serialize_trace(self.pre_hook_trace)
        if self.post_hook_trace:
            payload["meta"]["post_hook_trace"] = _serialize_trace(self.post_hook_trace)
        if self.error_hook_trace:
            payload["meta"]["error_hook_trace"] = _serialize_trace(self.error_hook_trace)
        if self.cancel_hook_trace:
            payload["meta"]["cancel_hook_trace"] = _serialize_trace(self.cancel_hook_trace)
        if self.audit_metadata:
            payload["meta"]["audit_metadata"] = redact_sensitive(self.audit_metadata)
        if not self.ok:
            payload["error"] = {
                "code": self.error_code or "unknown",
                "message": redact_sensitive(self.message),
                "retryable": self.retryable,
                "exception_type": self.exception_type,
            }
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_envelope(), ensure_ascii=False)


class ToolRuntime:
    def __init__(
        self,
        *,
        registry: ToolRegistry | None,
        executor: ToolExecutor | None = None,
        config: ToolRuntimeConfig | None = None,
    ) -> None:
        self._registry = registry
        self._executor = executor or ToolExecutor()
        self._config = config or ToolRuntimeConfig()

    async def execute_call(
        self,
        *,
        call_id: str,
        tool_name: str,
        raw_arguments: str | dict[str, Any],
        ctx: Any = None,
        source: str = "passive",
        session_key: str = "",
        channel: str = "",
        chat_id: str = "",
        request_text: str = "",
        tool_batch: tuple[dict[str, Any], ...] = (),
        tool_batch_index: int = 0,
        turn_id: str = "",
        trace_id: str = "",
        allowed_tool_names: frozenset[str] | None = None,
    ) -> ToolRuntimeResult:
        started = time.monotonic()

        def finish(result: ToolRuntimeResult) -> ToolRuntimeResult:
            result.turn_id = turn_id
            result.trace_id = trace_id
            return result

        #  解析传入的参数
        parsed = self._parse_arguments(raw_arguments)
        #  这个一般是错误了，才做这一步，直接返回了
        if isinstance(parsed, ToolRuntimeResult):
            parsed.tool_name = tool_name
            parsed.call_id = call_id
            parsed.duration_ms = _elapsed_ms(started)
            return finish(parsed)
        arguments = parsed

        tool = self._lookup_tool(tool_name)
        if tool is None:
            return finish(self._error(
                started=started,
                tool_name=tool_name,
                call_id=call_id,
                arguments=arguments,
                final_arguments=arguments,
                status="error",
                error_code="tool_lookup",
                message=f"Unknown tool: {tool_name}",
                retryable=False,
            ))
        if allowed_tool_names is not None and tool_name not in allowed_tool_names:
            return finish(self._error(
                started=started,
                tool_name=tool_name,
                call_id=call_id,
                arguments=arguments,
                final_arguments=arguments,
                status="denied",
                error_code="tool_locked",
                message=(
                    f"工具 '{tool_name}' 已注册但当前Turn尚未解锁。"
                    f"请先调用 tool_search(query='select:{tool_name}') 搜索并解锁。"
                ),
                retryable=False,
            ))
        meta = self._metadata(tool_name)
        # 得到一个数据结构，里面有最大重试次数和重试间隔
        retry_policy = self._retry_policy(tool, meta)
        max_attempts = 1 + retry_policy.max_retries
        last_result: ToolRuntimeResult | None = None
        all_extra_messages: list[str] = []
        all_pre_trace: list[HookTraceItem] = []
        all_post_trace: list[HookTraceItem] = []
        all_error_trace: list[HookTraceItem] = []
        all_cancel_trace: list[HookTraceItem] = []
        all_audit_metadata: dict[str, Any] = {}

        for attempt in range(max_attempts):
            request = ToolExecutionRequest(
                call_id=call_id,
                tool_name=tool_name,
                arguments=dict(arguments),
                source=source,  # type: ignore[arg-type]
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
                request_text=request_text,
                tool_batch=tool_batch,
                tool_batch_index=tool_batch_index,
                attempt=attempt,
                tool_source_type=meta.source_type,
                tool_source_name=meta.source_name,
                risk=meta.risk,
                idempotent=bool(getattr(tool, "idempotent", True)),
            )
            executed = await self._executor.execute(
                request,
                lambda name, args: self._invoke_with_timeout(
                    name,
                    args,
                    ctx,
                    timeout_s=_tool_timeout(tool, self._config.default_timeout_s),
                ),
                input_validator=lambda value: validate_json_schema(value, tool.parameters),
                output_validator=lambda output: validate_json_schema(
                    normalize_tool_output(output)[0], tool.output_schema
                ),
            )
            all_extra_messages.extend(executed.extra_messages)
            all_pre_trace.extend(executed.pre_hook_trace)
            all_post_trace.extend(executed.post_hook_trace)
            all_error_trace.extend(executed.error_hook_trace)
            all_cancel_trace.extend(executed.cancel_hook_trace)
            all_audit_metadata.update(executed.audit_metadata)
            if executed.status == "success":
                data, data_text = normalize_tool_output(executed.output)
                return finish(ToolRuntimeResult(
                    ok=True,
                    status="success",
                    tool_name=tool_name,
                    call_id=call_id,
                    data=data,
                    data_text=data_text,
                    retry_count=attempt,
                    duration_ms=_elapsed_ms(started),
                    arguments=arguments,
                    final_arguments=executed.final_arguments,
                    extra_messages=all_extra_messages,
                    pre_hook_trace=all_pre_trace,
                    post_hook_trace=all_post_trace,
                    error_hook_trace=all_error_trace,
                    cancel_hook_trace=all_cancel_trace,
                    audit_metadata=all_audit_metadata,
                ))

            error_code = executed.error_code or _classify_executor_error(
                executed.output, executed.status
            )
            retryable = bool(executed.retryable)
            last_result = self._error(
                started=started,
                tool_name=tool_name,
                call_id=call_id,
                arguments=arguments,
                final_arguments=executed.final_arguments,
                status="denied" if executed.status == "denied" else "error",
                error_code=error_code,
                message=str(executed.output),
                retryable=retryable and attempt < max_attempts - 1,
                retry_count=attempt,
                extra_messages=all_extra_messages,
                pre_hook_trace=all_pre_trace,
                post_hook_trace=all_post_trace,
                error_hook_trace=all_error_trace,
                cancel_hook_trace=all_cancel_trace,
                audit_metadata=all_audit_metadata,
                exception_type=executed.exception_type,
            )
            if executed.status == "denied":
                return finish(last_result)
            if not last_result.retryable:
                return finish(last_result)
            if retry_policy.backoff_s > 0:
                await asyncio.sleep(retry_policy.backoff_s)

        if last_result is not None:
            last_result.retryable = False
            last_result.duration_ms = _elapsed_ms(started)
            return finish(last_result)
        return finish(self._error(
            started=started,
            tool_name=tool_name,
            call_id=call_id,
            arguments=arguments,
            final_arguments=arguments,
            status="error",
            error_code="unknown",
            message="Tool runtime failed without result",
            retryable=False,
        ))

    async def _invoke_with_timeout(
        self,
        name: str,
        arguments: dict[str, Any],
        ctx: Any,
        *,
        timeout_s: float,
    ) -> Any:
        if self._registry is None:
            raise RuntimeError(f"Unknown tool: {name}")
        try:
            return await asyncio.wait_for(
                self._registry.execute(name, arguments, ctx),
                timeout=max(0.001, float(timeout_s)),
            )
        except asyncio.TimeoutError as exc:
            raise ToolRuntimeTimeoutError(
                f"Tool timed out after {timeout_s:.1f}s"
            ) from exc
    #  检验输入是否是字典活着json，并且json要是有效的
    def _parse_arguments(
        self,
        raw_arguments: str | dict[str, Any],
    ) -> dict[str, Any] | ToolRuntimeResult:
        if isinstance(raw_arguments, dict):
            return dict(raw_arguments)
        try:
            parsed = json.loads(raw_arguments or "")
        except (TypeError, json.JSONDecodeError) as exc:
            return ToolRuntimeResult(
                ok=False,
                status="error",
                tool_name="",
                error_code="argument_parse",
                message=f"Tool arguments must be valid JSON object: {exc}",
                retryable=True,
            )
        if not isinstance(parsed, dict):
            return ToolRuntimeResult(
                ok=False,
                status="error",
                tool_name="",
                error_code="argument_parse",
                message="Tool arguments must decode to a JSON object",
                retryable=True,
            )
        return parsed

    def _lookup_tool(self, name: str) -> Tool | None:
        if self._registry is None:
            return None
        return self._registry.get_tool(name)

    def _metadata(self, name: str) -> ToolMeta:
        if self._registry is None:
            return ToolMeta()
        getter = getattr(self._registry, "get_metadata", None)
        meta = getter(name) if callable(getter) else None
        return meta if isinstance(meta, ToolMeta) else ToolMeta()

    def _retry_policy(self, tool: Tool, meta: ToolMeta) -> ToolRetryPolicy:
        explicit = getattr(tool, "retry_policy", None)
        if isinstance(explicit, ToolRetryPolicy):
            return explicit
        max_retries = int(getattr(tool, "retry_count", 0) or 0)
        risk = str(getattr(meta, "risk", "") or "read-only")
        idempotent = bool(getattr(tool, "idempotent", True))
        if max_retries <= 0 and risk == "read-only" and idempotent:
            max_retries = self._config.read_only_max_retries
        if risk != "read-only" or not idempotent:
            max_retries = 0
        return ToolRetryPolicy(
            max_retries=max(0, max_retries),
            backoff_s=max(0.0, self._config.retry_backoff_s),
        )

    def _error(
        self,
        *,
        started: float,
        tool_name: str,
        call_id: str,
        arguments: dict[str, Any],
        final_arguments: dict[str, Any],
        status: ToolRuntimeStatus,
        error_code: ToolRuntimeErrorCode | str,
        message: str,
        retryable: bool,
        retry_count: int = 0,
        extra_messages: list[str] | None = None,
        pre_hook_trace: list[HookTraceItem] | None = None,
        post_hook_trace: list[HookTraceItem] | None = None,
        error_hook_trace: list[HookTraceItem] | None = None,
        cancel_hook_trace: list[HookTraceItem] | None = None,
        audit_metadata: dict[str, Any] | None = None,
        exception_type: str = "",
    ) -> ToolRuntimeResult:
        return ToolRuntimeResult(
            ok=False,
            status=status,
            tool_name=tool_name,
            call_id=call_id,
            error_code=error_code,
            message=message,
            retryable=retryable,
            retry_count=retry_count,
            duration_ms=_elapsed_ms(started),
            arguments=arguments,
            final_arguments=final_arguments,
            extra_messages=list(extra_messages or []),
            pre_hook_trace=list(pre_hook_trace or []),
            post_hook_trace=list(post_hook_trace or []),
            error_hook_trace=list(error_hook_trace or []),
            cancel_hook_trace=list(cancel_hook_trace or []),
            audit_metadata=dict(audit_metadata or {}),
            exception_type=exception_type,
        )


def validate_json_schema(value: Any, schema: dict[str, Any] | None) -> list[str]:
    if not schema:
        return []
    return _validate(value, schema, "")


def normalize_tool_output(output: Any) -> tuple[Any, str]:
    if isinstance(output, ToolResult):
        if output.data is not None:
            return output.data, output.text
        if output.text:
            parsed = _try_parse_json(output.text)
            if parsed is not None:
                return parsed, ""
            return output.text, output.text
        if output.content_blocks:
            return {"content_blocks": output.content_blocks}, output.preview()
        return "", ""
    if isinstance(output, str):
        parsed = _try_parse_json(output)
        if parsed is not None:
            return parsed, ""
        return output, output
    return output, ""


def unwrap_tool_envelope(raw: str | dict[str, Any]) -> dict[str, Any]:
    payload: Any
    if isinstance(raw, dict):
        payload = raw
    else:
        payload = _try_parse_json(str(raw or ""))
    if not isinstance(payload, dict):
        return {}
    if "ok" in payload and "data" in payload:
        data = payload.get("data")
        return data if isinstance(data, dict) else {}
    return payload


def _validate(value: Any, schema: dict[str, Any], path: str) -> list[str]:
    label = path or "参数"
    errors: list[str] = []
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        if not any(_matches_type(value, item) for item in schema_type):
            errors.append(f"{label} 应为 {schema_type} 类型")
            return errors
    elif isinstance(schema_type, str) and not _matches_type(value, schema_type):
        errors.append(f"{label} 应为 {schema_type} 类型")
        return errors

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{label} 须为以下值之一：{schema['enum']}")

    if schema_type in ("integer", "number"):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{label} 须 >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{label} 须 <= {schema['maximum']}")

    if schema_type == "string":
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{label} 最短 {schema['minLength']} 个字符")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{label} 最长 {schema['maxLength']} 个字符")

    if schema_type == "object" or isinstance(value, dict):
        props = schema.get("properties") or {}
        if not isinstance(props, dict):
            props = {}
        if isinstance(value, dict):
            for key in schema.get("required", []) or []:
                if key not in value:
                    errors.append(f"缺少必填字段：{path + '.' + key if path else key}")
            for key, item in value.items():
                if key in props and isinstance(props[key], dict):
                    child_path = f"{path}.{key}" if path else str(key)
                    errors.extend(_validate(item, props[key], child_path))

    if schema_type == "array" and isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                child_path = f"{path}[{index}]" if path else f"[{index}]"
                errors.extend(_validate(item, item_schema, child_path))
    return errors


def _matches_type(value: Any, schema_type: str) -> bool:
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "object":
        return isinstance(value, dict)
    if schema_type == "null":
        return value is None
    return True


def _classify_executor_error(output: Any, status: str) -> ToolRuntimeErrorCode:
    text = str(output or "")
    if status == "denied":
        return "policy_check"
    if "Tool timed out" in text or "timed out" in text.lower():
        return "timeout"
    if text.startswith("工具执行出错: hook "):
        return "hook_error"
    return "tool_invoke"


def _is_retryable_error(error_code: str, message: str) -> bool:
    if error_code == "timeout":
        return True
    lowered = message.lower()
    return any(
        token in lowered
        for token in ("timeout", "timed out", "temporarily", "connection", "rate limit", "429", "503")
    )


def _tool_timeout(tool: Tool, default_timeout_s: float) -> float:
    try:
        return float(getattr(tool, "timeout_s", default_timeout_s) or default_timeout_s)
    except (TypeError, ValueError):
        return default_timeout_s


def _try_parse_json(text: str) -> Any | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None


def _serialize_trace(items: list[HookTraceItem]) -> list[dict[str, Any]]:
    return [
        {
            "hook_name": item.hook_name,
            "event": item.event,
            "matched": item.matched,
            "decision": item.decision,
            "reason": item.reason,
            "extra_message": item.extra_message,
            "attempt": item.attempt,
            "input_before": redact_sensitive(item.input_before),
            "input_after": redact_sensitive(item.input_after),
            "output_before": redact_sensitive(item.output_before),
            "output_after": redact_sensitive(item.output_after),
            "audit_metadata": redact_sensitive(item.audit_metadata),
            "error_code": item.error_code,
            "retryable": item.retryable,
        }
        for item in items
    ]


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))
