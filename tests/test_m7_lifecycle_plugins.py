from __future__ import annotations

from pathlib import Path

import pytest

from agent.core.event_bus import EventBus
from agent.core.types import AfterTurnCtx
from agent.lifecycle.phase import (
    PHASE_ANCHOR_SLOTS,
    PHASE_NAMES,
    PhaseFrame,
    PhaseGraphError,
    PhaseModuleRunner,
    compile_phase_modules,
)
from agent.plugins import PluginManager
from agent.tool_hooks import ToolExecutor
from agent.tools import ToolRegistry


class _RecordModule:
    def __init__(
        self,
        slot: str,
        *,
        requires: tuple[str, ...],
        produces: tuple[str, ...] = (),
        record: list[str] | None = None,
    ) -> None:
        self.slot = slot
        self.requires = requires
        self.produces = produces
        self.record = record if record is not None else []

    async def run(self, frame: PhaseFrame) -> PhaseFrame:
        self.record.append(self.slot)
        for output in self.produces:
            frame.slots[output] = f"from:{self.slot}"
        return frame


@pytest.mark.asyncio
@pytest.mark.parametrize("phase_name", PHASE_NAMES)
async def test_all_seven_phases_execute_with_trace(phase_name: str) -> None:
    anchor = sorted(PHASE_ANCHOR_SLOTS[phase_name])[0]
    module = _RecordModule(f"test.{phase_name}", requires=(anchor,))
    runner = PhaseModuleRunner([module], phase_name=phase_name)

    await runner.run_ready(PhaseFrame(input={}, slots={anchor: True}))

    assert module.record == [f"test.{phase_name}"]
    assert runner.trace == (
        {
            "phase": phase_name,
            "slot": f"test.{phase_name}",
            "requires": [anchor],
            "produced": [f"test.{phase_name}"],
        },
    )


@pytest.mark.asyncio
async def test_graph_is_precompiled_in_topological_order_without_runtime_loop() -> None:
    record: list[str] = []
    consumer = _RecordModule(
        "demo.consumer",
        requires=("demo:data",),
        record=record,
    )
    producer = _RecordModule(
        "demo.producer",
        requires=("before_turn.acquire_session",),
        produces=("demo:data",),
        record=record,
    )
    graph = compile_phase_modules(
        [consumer, producer],
        phase_name="before_turn",
    )
    assert [module.slot for module in graph.modules] == [
        "demo.producer",
        "demo.consumer",
    ]

    runner = PhaseModuleRunner(graph.modules, phase_name="before_turn")
    await runner.run_ready(
        PhaseFrame(input={}, slots={"before_turn.acquire_session": True})
    )
    assert record == ["demo.producer", "demo.consumer"]


def test_invalid_graphs_fail_during_compile() -> None:
    anchor = "before_turn.acquire_session"
    with pytest.raises(PhaseGraphError, match="Duplicate"):
        compile_phase_modules(
            [
                _RecordModule("same.slot", requires=(anchor,)),
                _RecordModule("same.slot", requires=(anchor,)),
            ],
            phase_name="before_turn",
        )

    with pytest.raises(PhaseGraphError, match="Missing"):
        compile_phase_modules(
            [_RecordModule("missing.consumer", requires=("missing.input",))],
            phase_name="before_turn",
        )

    with pytest.raises(PhaseGraphError, match="Cyclic"):
        compile_phase_modules(
            [
                _RecordModule("cycle.a", requires=("cycle.b",)),
                _RecordModule("cycle.b", requires=("cycle.a",)),
            ],
            phase_name="before_turn",
        )

    first = _RecordModule("type.first", requires=(anchor,))
    first.output_types = {"shared:value": str}  # type: ignore[attr-defined]
    second = _RecordModule("type.second", requires=(anchor,))
    second.output_types = {"shared:value": int}  # type: ignore[attr-defined]
    with pytest.raises(PhaseGraphError, match="Output type conflict"):
        compile_phase_modules([first, second], phase_name="before_turn")


def _write_runtime_plugin(root: Path, *, fail_initialize: bool = False) -> None:
    plugin_dir = root / "plugins" / ("broken" if fail_initialize else "runtime")
    plugin_dir.mkdir(parents=True)
    failure = "raise RuntimeError('boom')" if fail_initialize else "return None"
    (plugin_dir / "plugin.py").write_text(
        f'''
from agent.plugins import Plugin, on_after_turn, on_tool_pre, tool

class InputModule:
    slot = "m7.{'broken' if fail_initialize else 'runtime'}.input"
    requires = ("before_turn.acquire_session",)
    async def run(self, frame):
        frame.slots["m7:input_checked"] = True
        return frame

class RuntimePlugin(Plugin):
    name = "{'broken' if fail_initialize else 'runtime'}"

    async def initialize(self):
        {failure}

    @tool(name="m7_echo")
    async def echo(self, event, text: str) -> str:
        return text

    @on_tool_pre(tool_name="m7_echo")
    async def hook(self, event):
        return event.arguments

    @on_after_turn()
    async def observer(self, event):
        event.extra_metadata["m7_observed"] = True

    def before_turn_modules(self):
        return [InputModule()]
''',
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_unload_removes_tool_hook_observer_and_phase_module(tmp_path: Path) -> None:
    _write_runtime_plugin(tmp_path)
    event_bus = EventBus()
    registry = ToolRegistry()
    executor = ToolExecutor()
    manager = PluginManager(
        [tmp_path / "plugins"], event_bus=event_bus, tool_registry=registry
    )
    manager.attach_tool_executor(executor)
    await manager.load_all()

    assert registry.has_tool("m7_echo")
    assert len(manager.before_turn_modules) == 1
    assert any(hook.name.startswith("plugin:runtime") for hook in executor._hooks)
    before = AfterTurnCtx(
        session_key="s", channel="telegram", chat_id="1",
        reply="ok", tools_used=(), thinking=None, will_dispatch=True,
    )
    await event_bus.observe(before)
    assert before.extra_metadata["m7_observed"] is True

    assert await manager.unload("runtime") is True
    assert not registry.has_tool("m7_echo")
    assert len(manager.before_turn_modules) == 0
    assert not any(hook.name.startswith("plugin:runtime") for hook in executor._hooks)
    after = AfterTurnCtx(
        session_key="s", channel="telegram", chat_id="1",
        reply="ok", tools_used=(), thinking=None, will_dispatch=True,
    )
    await event_bus.observe(after)
    assert "m7_observed" not in after.extra_metadata


@pytest.mark.asyncio
async def test_failed_plugin_leaves_no_partial_registration(tmp_path: Path) -> None:
    _write_runtime_plugin(tmp_path, fail_initialize=True)
    event_bus = EventBus()
    registry = ToolRegistry()
    manager = PluginManager(
        [tmp_path / "plugins"], event_bus=event_bus, tool_registry=registry
    )
    await manager.load_all()

    assert manager.loaded_count == 0
    assert not registry.has_tool("m7_echo")
    assert not manager.tool_hooks
    assert len(manager.before_turn_modules) == 0
    event = AfterTurnCtx(
        session_key="s", channel="telegram", chat_id="1",
        reply="ok", tools_used=(), thinking=None, will_dispatch=True,
    )
    await event_bus.observe(event)
    assert "m7_observed" not in event.extra_metadata
