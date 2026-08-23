from __future__ import annotations

import functools
import importlib.util
import inspect
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, cast

from agent.core.event_bus import EventBus, EventSubscription
from agent.lifecycle.phase import (
    CompiledPhaseModules,
    PHASE_NAMES,
    PhaseGraph,
    compile_phase_modules,
)
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
from agent.tool_hooks.types import HookContext, HookEvent, HookOutcome
from agent.tools.base import Tool

logger = logging.getLogger(__name__)


@dataclass
class _PluginResources:
    plugin_id: str
    tools: list[str] = field(default_factory=list)
    hooks: list[ToolHook] = field(default_factory=list)
    subscriptions: list[EventSubscription] = field(default_factory=list)
    phase_modules: dict[str, list[object]] = field(default_factory=dict)

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
        self._phase_plans = {
            phase_name: CompiledPhaseModules(phase_name) for phase_name in PHASE_NAMES
        }
        self._resources: dict[str, _PluginResources] = {}
        self._tool_executor: Any = None

    @property
    def loaded_count(self) -> int:
        return len(self._loaded)

    @property
    def tool_hooks(self) -> list[ToolHook]:
        return list(self._tool_hooks)

    @property
    def before_turn_modules(self) -> CompiledPhaseModules:
        return self._phase_plans["before_turn"]

    @property
    def before_reasoning_modules(self) -> CompiledPhaseModules:
        return self._phase_plans["before_reasoning"]

    @property
    def prompt_render_modules(self) -> CompiledPhaseModules:
        return self._phase_plans["prompt_render"]

    @property
    def before_step_modules(self) -> CompiledPhaseModules:
        return self._phase_plans["before_step"]

    @property
    def after_step_modules(self) -> CompiledPhaseModules:
        return self._phase_plans["after_step"]

    @property
    def after_reasoning_modules(self) -> CompiledPhaseModules:
        return self._phase_plans["after_reasoning"]

    @property
    def after_turn_modules(self) -> CompiledPhaseModules:
        return self._phase_plans["after_turn"]

    @property
    def phase_graphs(self) -> dict[str, dict[str, object]]:
        return {
            name: plan.graph.to_dict() for name, plan in self._phase_plans.items()
        }

    def attach_tool_executor(self, executor: Any) -> None:
        """Keep runtime hook installation/removal aligned with plugin lifetime."""
        self._tool_executor = executor
        executor.add_hooks(self._tool_hooks)

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
            await self.unload(module_path)

    async def unload(self, plugin: str) -> bool:
        """Unload one plugin and every resource it registered."""
        module_path = next(
            (
                path
                for path, resources in self._resources.items()
                if path == plugin or resources.plugin_id == plugin
            ),
            None,
        )
        if module_path is None:
            return False
        instance = plugin_registry.get_instance(module_path)
        if instance is not None and hasattr(instance, "terminate"):
            try:
                await cast(Any, instance).terminate()
            except Exception as exc:
                logger.warning("插件 terminate 失败 (%s): %s", module_path, exc)
        self._remove_resources(module_path, self._resources[module_path])
        plugin_registry.remove_plugin(module_path)
        self._resources.pop(module_path, None)
        self._loaded.discard(module_path)
        self._compile_phase_graphs()
        logger.info("插件已卸载: %s", plugin)
        return True

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

        resources = _PluginResources(plugin_id=plugin_id)
        try:
            resources.subscriptions = self._bind_handlers(instance, module_path)
            resources.tools = self._register_tools(instance, module_path)
            resources.hooks = self._bind_tool_hooks(instance, module_path)
            resources.phase_modules = self._collect_phase_modules(instance)
            self._compile_phase_graphs()
            if hasattr(instance, "initialize"):
                await instance.initialize()
        except Exception as exc:
            logger.warning("插件 %s 初始化失败，回滚: %s", mod["name"], exc)
            self._remove_resources(module_path, resources)
            self._compile_phase_graphs()
            plugin_registry.remove_plugin(module_path)
            return

        self._resources[module_path] = resources
        self._loaded.add(module_path)
        if self._tool_executor is not None:
            self._tool_executor.add_hooks(resources.hooks)
        logger.info(
            "插件已加载: %s lifecycle_dag=%s",
            mod["name"],
            json.dumps(self.phase_graphs, ensure_ascii=False, sort_keys=True),
        )

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

    def _bind_handlers(
        self, instance: Any, module_path: str
    ) -> list[EventSubscription]:
        subscriptions: list[EventSubscription] = []
        for md in plugin_registry.get_handlers_by_module_path(module_path):
            if md.kind != MetadataKind.LIFECYCLE or md.event_type is None:
                continue
            ctx_type = _EVENT_TYPE_MAP.get(md.event_type)
            if ctx_type is None:
                continue
            bound = functools.partial(md.handler, instance)
            if md.handler_type == HandlerType.TAP:
                subscription = self._event_bus.observe(
                    ctx_type, bound, priority=md.priority
                )
            else:
                subscription = self._event_bus.on(
                    ctx_type, bound, priority=md.priority
                )
            if isinstance(subscription, EventSubscription):
                subscriptions.append(subscription)
        return subscriptions

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

    def _bind_tool_hooks(self, instance: Any, module_path: str) -> list[ToolHook]:
        hooks: list[ToolHook] = []
        for md in plugin_registry.get_handlers_by_module_path(module_path):
            if md.kind != MetadataKind.TOOL_HOOK:
                continue
            bound = functools.partial(md.handler, instance)
            hook = _PluginToolHook(
                    name=f"plugin:{getattr(instance, 'name', module_path)}:{md.handler_name}",
                    handler=bound,
                    tool_name_filter=md.hook_tool_name,
                    event=_hook_event(md.event_type),
                )
            hooks.append(hook)
            self._tool_hooks.append(hook)
        return hooks

    def _collect_phase_modules(self, instance: Any) -> dict[str, list[object]]:
        # Providers are evaluated before mutating live lists.  If any provider
        # fails, no earlier phase from the same plugin leaks into the runtime.
        collected = {
            phase_name: _load_module_list(instance, f"{phase_name}_modules")
            for phase_name in PHASE_NAMES
        }
        for phase_name, loaded in collected.items():
            self._module_list(phase_name).extend(loaded)
        return collected

    def _module_list(self, phase_name: str) -> list[object]:
        return cast(list[object], getattr(self, f"_{phase_name}_modules"))

    def _compile_phase_graphs(self) -> None:
        # Compile all candidates first.  A failure leaves every live plan on
        # its previous valid generation.
        graphs: dict[str, PhaseGraph] = {
            name: compile_phase_modules(self._module_list(name), phase_name=name)
            for name in PHASE_NAMES
        }
        for name, graph in graphs.items():
            self._phase_plans[name].graph = graph

    def _remove_resources(
        self, module_path: str, resources: _PluginResources
    ) -> None:
        for subscription in resources.subscriptions:
            self._event_bus.unsubscribe(subscription)
        for tool_name in resources.tools:
            if self._tool_registry is not None:
                self._tool_registry.unregister(tool_name)
        if self._tool_executor is not None:
            self._tool_executor.remove_hooks(resources.hooks)
        hook_ids = {id(hook) for hook in resources.hooks}
        self._tool_hooks[:] = [
            hook for hook in self._tool_hooks if id(hook) not in hook_ids
        ]
        for phase_name, modules in resources.phase_modules.items():
            module_ids = {id(module) for module in modules}
            target = self._module_list(phase_name)
            target[:] = [module for module in target if id(module) not in module_ids]

def _accepted_tool_params(bound: Callable[..., Any]) -> frozenset[str]:
    sig = inspect.signature(bound)
    return frozenset(name for name in sig.parameters if name not in {"self", "event"})


def _load_module_list(instance: Any, method_name: str) -> list[object]:
    provider = getattr(instance, method_name, None)
    if provider is None:
        return []
    if not callable(provider):
        raise TypeError(f"插件 {type(instance).__name__}.{method_name} 不是可调用对象")
    loaded = provider()
    if loaded is None:
        return []
    if not isinstance(loaded, list):
        raise TypeError(f"插件 {type(instance).__name__}.{method_name} 返回值不是 list")
    return loaded


class _PluginToolHook(ToolHook):
    def __init__(
        self,
        *,
        name: str,
        handler: Callable[..., Any],
        tool_name_filter: str | None,
        event: HookEvent,
    ) -> None:
        self.name = name
        self.event = event
        self._handler = handler
        self._tool_name_filter = tool_name_filter

    def matches(self, ctx: HookContext) -> bool:
        return self._tool_name_filter is None or ctx.request.tool_name == self._tool_name_filter

    async def run(self, ctx: HookContext) -> HookOutcome:
        if self.event != "before_call":
            result = self._handler(ctx)
            if inspect.isawaitable(result):
                result = await result
            if result is None:
                return HookOutcome()
            if isinstance(result, HookOutcome):
                return result
            if self.event == "after_call":
                return HookOutcome(output_updated=True, updated_output=result)
            if isinstance(result, dict):
                return HookOutcome(audit_metadata=dict(result))
            return HookOutcome()

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


def _hook_event(event_type: PluginEventType | None) -> HookEvent:
    mapping: dict[PluginEventType, HookEvent] = {
        PluginEventType.PRE_TOOL: "before_call",
        PluginEventType.POST_TOOL: "after_call",
        PluginEventType.TOOL_ERROR: "on_error",
        PluginEventType.TOOL_CANCEL: "on_cancel",
    }
    if event_type is None:
        return "before_call"
    return mapping.get(event_type, "before_call")


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
