from __future__ import annotations

import functools
import importlib.util
import inspect
import json
import logging
import sys
from pathlib import Path
from typing import Any, Callable, cast

from agent.core.event_bus import EventBus
from agent.lifecycle.types import (
    AfterReasoningCtx,
    AfterStepCtx,
    AfterToolResultCtx,
    AfterTurnCtx,
    BeforeReasoningCtx,
    BeforeStepCtx,
    BeforeToolCallCtx,
    BeforeTurnCtx,
    PreToolCtx,
    PromptRenderCtx,
)
from agent.plugins.registry import HandlerType, MetadataKind, PluginEventType, plugin_registry
from agent.tool_hooks.base import ToolHook
from agent.tool_hooks.types import HookContext, HookOutcome
from agent.tools.base import Tool

logger = logging.getLogger(__name__)

_EVENT_TYPE_MAP: dict[PluginEventType, type] = {
    PluginEventType.BEFORE_TURN: BeforeTurnCtx,
    PluginEventType.BEFORE_REASONING: BeforeReasoningCtx,
    PluginEventType.PROMPT_RENDER: PromptRenderCtx,
    PluginEventType.BEFORE_STEP: BeforeStepCtx,
    PluginEventType.AFTER_STEP: AfterStepCtx,
    PluginEventType.AFTER_REASONING: AfterReasoningCtx,
    PluginEventType.AFTER_TURN: AfterTurnCtx,
    PluginEventType.BEFORE_TOOL_CALL: BeforeToolCallCtx,
    PluginEventType.AFTER_TOOL_RESULT: AfterToolResultCtx,
}


class PluginManager:
    def __init__(
        self,
        plugin_dirs: list[Path],
        *,
        event_bus: EventBus,
        tool_registry: Any = None,
        workspace: Path | None = None,
        memory_engine: Any = None,
    ) -> None:
        self._dirs = plugin_dirs
        self._event_bus = event_bus
        self._tool_registry = tool_registry
        self._workspace = workspace
        self._memory_engine = memory_engine
        self._loaded: set[str] = set()
        self._tool_hooks: list[ToolHook] = []
        self._before_turn_modules: list[object] = []
        self._before_reasoning_modules: list[object] = []
        self._prompt_render_modules: list[object] = []
        self._before_step_modules: list[object] = []
        self._after_step_modules: list[object] = []
        self._after_reasoning_modules: list[object] = []
        self._after_turn_modules: list[object] = []

    @property
    def loaded_count(self) -> int:
        return len(self._loaded)

    @property
    def tool_hooks(self) -> list[ToolHook]:
        return list(self._tool_hooks)

    @property
    def before_turn_modules(self) -> list[object]:
        return list(self._before_turn_modules)

    @property
    def before_reasoning_modules(self) -> list[object]:
        return list(self._before_reasoning_modules)

    @property
    def prompt_render_modules(self) -> list[object]:
        return list(self._prompt_render_modules)

    @property
    def before_step_modules(self) -> list[object]:
        return list(self._before_step_modules)

    @property
    def after_step_modules(self) -> list[object]:
        return list(self._after_step_modules)

    @property
    def after_reasoning_modules(self) -> list[object]:
        return list(self._after_reasoning_modules)

    @property
    def after_turn_modules(self) -> list[object]:
        return list(self._after_turn_modules)

    def discover(self) -> list[dict[str, str]]:
        mods: list[dict[str, str]] = []
        seen_names: set[str] = set()
        for plugin_dir in self._dirs:
            if not plugin_dir.is_dir():
                continue
            source = plugin_dir.name
            for child in sorted(plugin_dir.iterdir()):
                main = child / "plugin.py"
                if not child.is_dir() or not main.exists():
                    continue
                if child.name in seen_names:
                    logger.warning("插件名重复，跳过: %s (%s)", child.name, main)
                    continue
                seen_names.add(child.name)
                mods.append(
                    {
                        "name": child.name,
                        "module_path": str(main),
                        "import_path": f"telegram_bot_plugin_{source}_{child.name}",
                    }
                )
        return mods

    async def load_all(self) -> None:
        for mod in self.discover():
            await self._load_one(mod)

    async def terminate_all(self) -> None:
        for module_path in list(self._loaded):
            instance = plugin_registry.get_instance(module_path)
            if instance is not None and hasattr(instance, "terminate"):
                try:
                    await instance.terminate()
                except Exception as exc:
                    logger.warning("插件 terminate 失败 (%s): %s", module_path, exc)
            for md in plugin_registry.get_handlers_by_module_path(module_path):
                if md.kind == MetadataKind.TOOL and self._tool_registry is not None:
                    self._tool_registry.unregister(md.tool_name or md.handler_name)
            plugin_registry.remove_plugin(module_path)
        self._loaded.clear()
        self._tool_hooks.clear()
        self._before_turn_modules.clear()
        self._before_reasoning_modules.clear()
        self._prompt_render_modules.clear()
        self._before_step_modules.clear()
        self._after_step_modules.clear()
        self._after_reasoning_modules.clear()
        self._after_turn_modules.clear()

    async def _load_one(self, mod: dict[str, str]) -> None:
        module_path = mod["import_path"]
        if module_path in self._loaded:
            return
        try:
            self._import_plugin(module_path, Path(mod["module_path"]))
        except Exception as exc:
            logger.warning("插件 %s 导入失败: %s", mod["name"], exc)
            return

        cls = plugin_registry._classes.get(module_path)
        if cls is None:
            logger.warning("插件 %s 未注册类", mod["name"])
            return

        instance = cls()
        plugin_dir = Path(mod["module_path"]).parent
        _apply_manifest(instance, plugin_dir)
        plugin_id = str(instance.name) if instance.name else mod["name"]

        from agent.plugins.context import PluginContext, PluginKVStore

        instance.context = PluginContext(  # type: ignore[attr-defined]
            event_bus=self._event_bus,
            tool_registry=self._tool_registry,
            plugin_id=plugin_id,
            plugin_dir=plugin_dir,
            kv_store=PluginKVStore(plugin_dir / ".kv.json"),
            config=_load_plugin_config(plugin_dir),
            workspace=self._workspace,
            memory_engine=self._memory_engine,
        )
        plugin_registry.register_instance(module_path, instance)

        self._bind_handlers(instance, module_path)
        tool_names = self._register_tools(instance, module_path)
        hook_count_before = len(self._tool_hooks)
        self._bind_tool_hooks(instance, module_path)
        module_counts_before = self._module_counts()
        self._collect_phase_modules(instance)

        try:
            if hasattr(instance, "initialize"):
                await instance.initialize()
        except Exception as exc:
            logger.warning("插件 %s 初始化失败，回滚: %s", mod["name"], exc)
            plugin_registry.remove_plugin(module_path)
            for tool_name in tool_names:
                if self._tool_registry is not None:
                    self._tool_registry.unregister(tool_name)
            del self._tool_hooks[hook_count_before:]
            self._rollback_phase_modules(module_counts_before)
            return

        self._loaded.add(module_path)
        logger.info("插件已加载: %s", mod["name"])

    def _import_plugin(self, module_name: str, path: Path) -> None:
        spec = importlib.util.spec_from_file_location(
            module_name,
            path,
            submodule_search_locations=[str(path.parent)],
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载插件文件: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

    def _bind_handlers(self, instance: Any, module_path: str) -> None:
        for md in plugin_registry.get_handlers_by_module_path(module_path):
            if md.kind != MetadataKind.LIFECYCLE or md.event_type is None:
                continue
            ctx_type = _EVENT_TYPE_MAP.get(md.event_type)
            if ctx_type is None:
                continue
            bound = functools.partial(md.handler, instance)
            if md.handler_type == HandlerType.TAP:
                self._event_bus.observe(ctx_type, bound, priority=md.priority)
            else:
                self._event_bus.on(ctx_type, bound, priority=md.priority)

    def _register_tools(self, instance: Any, module_path: str) -> list[str]:
        tool_names: list[str] = []
        if self._tool_registry is None:
            return tool_names
        for md in plugin_registry.get_handlers_by_module_path(module_path):
            if md.kind != MetadataKind.TOOL:
                continue
            bound = functools.partial(md.handler, instance, None)
            accepted = _accepted_tool_params(bound)
            tool_name = md.tool_name or md.handler_name
            description = (md.handler.__doc__ or "").strip()
            schema = md.tool_schema or {"type": "object", "properties": {}, "required": []}

            async def handler(
                arguments: dict[str, Any],
                ctx: Any,
                *,
                bound_handler: Callable[..., Any] = bound,
                accepted_params: frozenset[str] = accepted,
            ) -> str:
                filtered = {k: v for k, v in arguments.items() if k in accepted_params}
                result = bound_handler(**filtered)
                if inspect.isawaitable(result):
                    result = await result
                return str(result)

            self._tool_registry.register(
                Tool(
                    name=tool_name,
                    description=description,
                    parameters=schema,
                    handler=handler,
                ),
                risk=md.tool_risk or "read-write",
                always_on=md.tool_always_on,
                search_hint=md.tool_search_hint,
                source_type="plugin",
                source_name=str(getattr(instance, "name", None) or module_path),
            )
            tool_names.append(tool_name)
        return tool_names

    def _bind_tool_hooks(self, instance: Any, module_path: str) -> None:
        for md in plugin_registry.get_handlers_by_module_path(module_path):
            if md.kind != MetadataKind.TOOL_HOOK:
                continue
            bound = functools.partial(md.handler, instance)
            self._tool_hooks.append(
                _PluginToolHook(
                    name=f"plugin:{getattr(instance, 'name', module_path)}:{md.handler_name}",
                    handler=bound,
                    tool_name_filter=md.hook_tool_name,
                )
            )

    def _collect_phase_modules(self, instance: Any) -> None:
        self._before_turn_modules.extend(_load_module_list(instance, "before_turn_modules"))
        self._before_reasoning_modules.extend(
            _load_module_list(instance, "before_reasoning_modules")
        )
        self._prompt_render_modules.extend(_load_module_list(instance, "prompt_render_modules"))
        self._before_step_modules.extend(_load_module_list(instance, "before_step_modules"))
        self._after_step_modules.extend(_load_module_list(instance, "after_step_modules"))
        self._after_reasoning_modules.extend(
            _load_module_list(instance, "after_reasoning_modules")
        )
        self._after_turn_modules.extend(_load_module_list(instance, "after_turn_modules"))

    def _module_counts(self) -> dict[str, int]:
        return {
            "before_turn": len(self._before_turn_modules),
            "before_reasoning": len(self._before_reasoning_modules),
            "prompt_render": len(self._prompt_render_modules),
            "before_step": len(self._before_step_modules),
            "after_step": len(self._after_step_modules),
            "after_reasoning": len(self._after_reasoning_modules),
            "after_turn": len(self._after_turn_modules),
        }

    def _rollback_phase_modules(self, counts: dict[str, int]) -> None:
        del self._before_turn_modules[counts["before_turn"]:]
        del self._before_reasoning_modules[counts["before_reasoning"]:]
        del self._prompt_render_modules[counts["prompt_render"]:]
        del self._before_step_modules[counts["before_step"]:]
        del self._after_step_modules[counts["after_step"]:]
        del self._after_reasoning_modules[counts["after_reasoning"]:]
        del self._after_turn_modules[counts["after_turn"]:]


def _accepted_tool_params(bound: Callable[..., Any]) -> frozenset[str]:
    sig = inspect.signature(bound)
    return frozenset(name for name in sig.parameters if name not in {"self", "event"})


def _load_module_list(instance: Any, method_name: str) -> list[object]:
    provider = getattr(instance, method_name, None)
    if provider is None:
        return []
    if not callable(provider):
        logger.warning("插件 %s.%s 不是可调用对象", type(instance).__name__, method_name)
        return []
    try:
        loaded = provider()
    except Exception as exc:
        logger.warning("插件 %s.%s 加载失败: %s", type(instance).__name__, method_name, exc)
        return []
    if loaded is None:
        return []
    if not isinstance(loaded, list):
        logger.warning("插件 %s.%s 返回值不是 list", type(instance).__name__, method_name)
        return []
    return loaded


class _PluginToolHook(ToolHook):
    event = "pre_tool_use"

    def __init__(
        self,
        *,
        name: str,
        handler: Callable[..., Any],
        tool_name_filter: str | None,
    ) -> None:
        self.name = name
        self._handler = handler
        self._tool_name_filter = tool_name_filter

    def matches(self, ctx: HookContext) -> bool:
        return self._tool_name_filter is None or ctx.request.tool_name == self._tool_name_filter

    async def run(self, ctx: HookContext) -> HookOutcome:
        event = PreToolCtx(
            session_key=ctx.request.session_key,
            channel=ctx.request.channel,
            chat_id=ctx.request.chat_id,
            tool_name=ctx.request.tool_name,
            arguments=dict(ctx.current_arguments),
            call_id=ctx.request.call_id,
            source=ctx.request.source,
            request_text=ctx.request.request_text,
            tool_batch=ctx.request.tool_batch,
            tool_batch_index=ctx.request.tool_batch_index,
        )
        result = self._handler(event)
        if inspect.isawaitable(result):
            result = await result
        if result is None:
            return HookOutcome()
        if isinstance(result, HookOutcome):
            return result
        if isinstance(result, dict):
            return HookOutcome(updated_input=result)
        return HookOutcome(
            decision="deny",
            reason=f"插件 hook {self.name} 返回了不支持的结果类型",
        )


def _load_plugin_config(plugin_dir: Path) -> Any:
    from agent.plugins.config import PluginConfig

    values: dict[str, Any] = {}
    schema_path = plugin_dir / "_conf_schema.json"
    if schema_path.exists():
        try:
            loaded = json.loads(schema_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("_conf_schema.json 读取失败 (%s): %s", plugin_dir, exc)
        else:
            if isinstance(loaded, dict):
                for key, spec in cast(dict[str, Any], loaded).items():
                    if isinstance(spec, dict) and "default" in spec:
                        values[str(key)] = spec["default"]

    override_path = plugin_dir / "plugin_config.json"
    if override_path.exists():
        try:
            loaded_override = json.loads(override_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("plugin_config.json 读取失败 (%s): %s", plugin_dir, exc)
        else:
            if isinstance(loaded_override, dict):
                values.update(cast(dict[str, Any], loaded_override))
    return PluginConfig(values)


def _apply_manifest(instance: Any, plugin_dir: Path) -> None:
    manifest_path = plugin_dir / "manifest.yaml"
    if not manifest_path.exists():
        return
    try:
        lines = manifest_path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        logger.warning("manifest.yaml 读取失败 (%s): %s", plugin_dir, exc)
        return
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        key = key.strip()
        if key in {"name", "version", "desc", "author"}:
            setattr(instance, key, value.strip().strip('"').strip("'"))
