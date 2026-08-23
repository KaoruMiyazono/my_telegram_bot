import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, cast

logger = logging.getLogger(__name__)


@dataclass
class _TypedHandler:
    ctx_type: type
    handler: Callable
    priority: int = 0


@dataclass(frozen=True)
class EventSubscription:
    kind: str
    event_type: str | type
    handler: Callable


class EventBus:
    _instance: "EventBus | None" = None

    def __init__(self) -> None:
        #  代表三种事件类型
        #         EventBus
        # │
        # ├── 字符串事件
        # │     subscribe("turn_committed", handler)
        # │     emit("turn_committed", event=event)
        # │
        # ├── Typed GATE
        # │     on(BeforeTurnCtx, handler, priority=10)
        # │     emit(before_turn_ctx)
        # │
        # └── Typed TAP
        #       observe(AfterTurnCtx, handler, priority=10)   注册
        #       await observe(after_turn_ctx)                 触发
        self._subscribers: dict[str, list[Callable]] = defaultdict(list)
        self._gate_handlers: dict[type, list[_TypedHandler]] = defaultdict(list)
        self._tap_handlers: dict[type, list[_TypedHandler]] = defaultdict(list)

    @classmethod
    def get_instance(cls) -> "EventBus":
        #  保证全局一致
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def subscribe(self, event_type: str, handler: Callable) -> EventSubscription:
        self._subscribers[event_type].append(handler)
        return EventSubscription("string", event_type, handler)

    def on(
        self, ctx_type: type, handler: Callable, *, priority: int = 0
    ) -> EventSubscription:
        """Register an Akashic-style GATE lifecycle handler."""
        self._append_typed_handler(self._gate_handlers, ctx_type, handler, priority)
        return EventSubscription("gate", ctx_type, handler)

    def observe(
        self,
        event_or_type: Any,
        handler: Callable | None = None,
        *,
        priority: int = 0,
    ) -> Any:
        """Register or run Akashic-style TAP lifecycle handlers.

        `event_bus.observe(CtxType, handler)` registers a TAP handler.
        `await event_bus.observe(ctx)` fans out a TAP event.
        """
        if handler is not None:
            self._append_typed_handler(self._tap_handlers, event_or_type, handler, priority)
            return EventSubscription("tap", event_or_type, handler)
        return self._observe_event(event_or_type)

    def unsubscribe(self, subscription: EventSubscription) -> bool:
        """Remove exactly one prior subscription; safe to call repeatedly."""
        if subscription.kind == "string":
            handlers = self._subscribers.get(str(subscription.event_type), [])
            try:
                handlers.remove(subscription.handler)
            except ValueError:
                return False
            return True
        target = self._gate_handlers if subscription.kind == "gate" else self._tap_handlers
        handlers = target.get(cast(type, subscription.event_type), [])
        for item in list(handlers):
            if item.handler is subscription.handler:
                handlers.remove(item)
                return True
        return False

    async def emit(self, event_or_type: Any, **data: Any) -> Any:
        if isinstance(event_or_type, str):
            await self._emit_string(event_or_type, **data)
            return None

        event = event_or_type
        current = event
        for item in self._matching_handlers(self._gate_handlers, type(event)):
            try:
                result = item.handler(current)
                if asyncio.iscoroutine(result):
                    result = await result
                if result is None:
                    return None
                current = result
            except Exception as e:
                logger.warning(
                    "EventBus GATE handler %s failed for %s: %s",
                    item.handler,
                    type(event).__name__,
                    e,
                )
        return current

    def _append_typed_handler(
        self,
        target: dict[type, list[_TypedHandler]],
        ctx_type: type,
        handler: Callable,
        priority: int,
    ) -> None:
        handlers = target[ctx_type]
        handlers.append(_TypedHandler(ctx_type=ctx_type, handler=handler, priority=priority))
        handlers.sort(key=lambda h: -h.priority)

    def _matching_handlers(
        self,
        target: dict[type, list[_TypedHandler]],
        ctx_type: type,
    ) -> list[_TypedHandler]:
        matched: list[_TypedHandler] = []
        for registered_type, handlers in target.items():
            if issubclass(ctx_type, registered_type):
                matched.extend(handlers)
        matched.sort(key=lambda h: -h.priority)
        return matched

    async def _observe_event(self, event: Any) -> None:
        for item in self._matching_handlers(self._tap_handlers, type(event)):
            try:
                result = item.handler(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.warning(
                    "EventBus TAP handler %s failed for %s: %s",
                    item.handler,
                    type(event).__name__,
                    e,
                )

    async def _emit_string(self, event_type: str, **data: Any) -> None:
        handlers = self._subscribers.get(event_type, [])
        for handler in handlers:
            try:
                result = handler(**data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.warning("EventBus handler %s failed for %s: %s", handler, event_type, e)
