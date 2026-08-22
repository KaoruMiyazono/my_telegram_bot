from __future__ import annotations

import inspect
from typing import Any, Callable, get_args, get_origin

from agent.plugins.registry import (
    HandlerType,
    MetadataKind,
    PluginEventType,
    PluginHandlerMetadata,
    plugin_registry,
)


def _get_or_create_handler(
    func: Callable[..., Any],
    event_type: PluginEventType,
    handler_type: HandlerType,
    **kwargs: Any,
) -> PluginHandlerMetadata:
    existing = plugin_registry._handlers.get_by_name(
        event_type,
        func.__name__,
        func.__module__,
    )
    if existing is not None:
        return existing
    md = PluginHandlerMetadata(
        kind=MetadataKind.LIFECYCLE,
        event_type=event_type,
        handler_type=handler_type,
        handler=func,
        handler_name=func.__name__,
        plugin_module_path=func.__module__,
        **kwargs,
    )
    plugin_registry._handlers.append(md)
    return md


def on_before_turn(**options: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def deco(func: Callable[..., Any]) -> Callable[..., Any]:
        _get_or_create_handler(func, PluginEventType.BEFORE_TURN, HandlerType.GATE, **options)
        return func
    return deco


def on_before_reasoning(**options: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def deco(func: Callable[..., Any]) -> Callable[..., Any]:
        _get_or_create_handler(func, PluginEventType.BEFORE_REASONING, HandlerType.GATE, **options)
        return func
    return deco


def on_after_reasoning(**options: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def deco(func: Callable[..., Any]) -> Callable[..., Any]:
        _get_or_create_handler(func, PluginEventType.AFTER_REASONING, HandlerType.GATE, **options)
        return func
    return deco


def on_prompt_render(**options: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def deco(func: Callable[..., Any]) -> Callable[..., Any]:
        _get_or_create_handler(func, PluginEventType.PROMPT_RENDER, HandlerType.GATE, **options)
        return func
    return deco


def on_before_step(**options: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def deco(func: Callable[..., Any]) -> Callable[..., Any]:
        _get_or_create_handler(func, PluginEventType.BEFORE_STEP, HandlerType.GATE, **options)
        return func
    return deco


def on_after_step(**options: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def deco(func: Callable[..., Any]) -> Callable[..., Any]:
        _get_or_create_handler(func, PluginEventType.AFTER_STEP, HandlerType.TAP, **options)
        return func
    return deco


def on_after_turn(**options: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def deco(func: Callable[..., Any]) -> Callable[..., Any]:
        _get_or_create_handler(func, PluginEventType.AFTER_TURN, HandlerType.TAP, **options)
        return func
    return deco


def on_tool_pre(
    *,
    tool_name: str | None = None,
    **options: Any,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def deco(func: Callable[..., Any]) -> Callable[..., Any]:
        md = PluginHandlerMetadata(
            kind=MetadataKind.TOOL_HOOK,
            event_type=PluginEventType.PRE_TOOL,
            handler_type=None,
            handler=func,
            handler_name=func.__name__,
            plugin_module_path=func.__module__,
            hook_tool_name=tool_name,
            **options,
        )
        plugin_registry._handlers.append(md)
        return func
    return deco


def tool(
    name: str,
    *,
    risk: str = "read-write",
    always_on: bool = False,
    search_hint: str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def deco(func: Callable[..., Any]) -> Callable[..., Any]:
        params = list(inspect.signature(func).parameters)
        if len(params) < 2 or params[0] != "self" or params[1] != "event":
            raise TypeError(f"@tool handler 前两个参数必须是 self 和 event: {func.__qualname__}")
        md = PluginHandlerMetadata(
            kind=MetadataKind.TOOL,
            event_type=None,
            handler_type=None,
            handler=func,
            handler_name=func.__name__,
            plugin_module_path=func.__module__,
            tool_name=name,
            tool_schema=_derive_params_schema(func),
            tool_risk=risk,
            tool_always_on=always_on,
            tool_search_hint=search_hint,
        )
        plugin_registry._handlers.append(md)
        return func
    return deco


def _derive_params_schema(func: Callable[..., Any]) -> dict[str, Any]:
    sig = inspect.signature(func)
    param_docs = _parse_args_doc(func.__doc__ or "")
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        if name in ("self", "event"):
            continue
        schema = {"type": _annotation_to_json_type(param.annotation)}
        if name in param_docs:
            schema["description"] = param_docs[name]
        properties[name] = schema
        if param.default is inspect.Parameter.empty:
            required.append(name)
    return {"type": "object", "properties": properties, "required": required}


def _annotation_to_json_type(annotation: Any) -> str:
    if annotation is inspect.Parameter.empty:
        return "string"
    origin = get_origin(annotation)
    if origin in (list, tuple, set):
        return "array"
    if origin is dict:
        return "object"
    if origin is not None:
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if args:
            return _annotation_to_json_type(args[0])
    return {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        dict: "object",
        list: "array",
    }.get(annotation, "string")


def _parse_args_doc(doc: str) -> dict[str, str]:
    lines = doc.splitlines()
    result: dict[str, str] = {}
    in_args = False
    for raw_line in lines:
        line = raw_line.strip()
        if line in {"Args:", "Arguments:", "Parameters:"}:
            in_args = True
            continue
        if in_args and not line:
            continue
        if in_args and ":" in line:
            name, desc = line.split(":", 1)
            name = name.strip()
            if name:
                result[name] = desc.strip()
            continue
        if in_args and line and not raw_line.startswith((" ", "\t")):
            break
    return result
