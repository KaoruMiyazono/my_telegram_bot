from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Callable


class HandlerType(Enum):
    GATE = auto()
    TAP = auto()


class MetadataKind(Enum):
    LIFECYCLE = auto()
    TOOL = auto()
    TOOL_HOOK = auto()


class PluginEventType(Enum):
    BEFORE_TURN = "before_turn"
    BEFORE_REASONING = "before_reasoning"
    PROMPT_RENDER = "prompt_render"
    BEFORE_STEP = "before_step"
    AFTER_STEP = "after_step"
    AFTER_REASONING = "after_reasoning"
    AFTER_TURN = "after_turn"
    BEFORE_TOOL_CALL = "before_tool_call"
    AFTER_TOOL_RESULT = "after_tool_result"
    PRE_TOOL = "pre_tool"


@dataclass
class PluginHandlerMetadata:
    kind: MetadataKind
    event_type: PluginEventType | None
    handler_type: HandlerType | None
    handler: Callable[..., Any]
    handler_name: str
    plugin_module_path: str
    tool_name: str | None = None
    tool_schema: dict[str, Any] | None = None
    tool_risk: str | None = None
    tool_always_on: bool = False
    tool_search_hint: str | None = None
    hook_tool_name: str | None = None
    priority: int = 0
    active: bool = True


class PluginHandlerRegistry:
    def __init__(self) -> None:
        self._handlers: list[PluginHandlerMetadata] = []

    def append(self, md: PluginHandlerMetadata) -> None:
        self._handlers.append(md)
        self._handlers.sort(key=lambda h: -h.priority)

    def get_by_name(
        self,
        event_type: PluginEventType,
        handler_name: str,
        module_path: str,
    ) -> PluginHandlerMetadata | None:
        for handler in self._handlers:
            if (
                handler.event_type == event_type
                and handler.handler_name == handler_name
                and handler.plugin_module_path == module_path
            ):
                return handler
        return None

    def get_by_module_path(self, module_path: str) -> list[PluginHandlerMetadata]:
        return [h for h in self._handlers if h.plugin_module_path == module_path]

    def remove_by_module_path(self, module_path: str) -> None:
        self._handlers = [
            h for h in self._handlers if h.plugin_module_path != module_path
        ]


class PluginRegistry:
    def __init__(self) -> None:
        self._handlers = PluginHandlerRegistry()
        self._classes: dict[str, type] = {}
        self._instances: dict[str, object] = {}

    def register_class(self, cls: type) -> None:
        self._classes[cls.__module__] = cls

    def register_instance(self, module_path: str, instance: object) -> None:
        self._instances[module_path] = instance

    def get_instance(self, module_path: str) -> object | None:
        return self._instances.get(module_path)

    def get_handlers_by_module_path(
        self,
        module_path: str,
    ) -> list[PluginHandlerMetadata]:
        return self._handlers.get_by_module_path(module_path)

    def remove_plugin(self, module_path: str) -> None:
        self._handlers.remove_by_module_path(module_path)
        self._classes.pop(module_path, None)
        self._instances.pop(module_path, None)


plugin_registry = PluginRegistry()
