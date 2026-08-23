from __future__ import annotations

import logging
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar, cast

logger = logging.getLogger(__name__)

I = TypeVar("I")
O = TypeVar("O")
F = TypeVar("F", bound="PhaseFrame[Any, Any]")

PHASE_NAMES = (
    "before_turn",
    "before_reasoning",
    "prompt_render",
    "before_step",
    "after_step",
    "after_reasoning",
    "after_turn",
)

# Built-in slots are phase-owned anchors.  They may become available at
# different points during a phase, but are all valid dependencies at compile
# time.  Plugin-produced slots are validated separately below.
PHASE_ANCHOR_SLOTS: dict[str, frozenset[str]] = {
    "before_turn": frozenset(
        {
            "session:session",
            "session:ctx",
            "session:retrieved_memories",
            "session:retrieved_memory_block",
            "session:retrieval_trace_raw",
            "session:abort_reply",
            "before_turn.acquire_session",
            "before_turn.prepare_context",
            "before_turn.build_ctx",
            "before_turn.emit",
            "before_turn.collect_exports",
            "before_turn.return",
        }
    ),
    "before_reasoning": frozenset(
        {
            "reasoning:ctx",
            "reasoning:tools",
            "reasoning:abort_reply",
            "before_reasoning.sync_tools",
            "before_reasoning.build_ctx",
            "before_reasoning.emit",
            "before_reasoning.collect_exports",
            "before_reasoning.prompt_warmup",
            "before_reasoning.return",
        }
    ),
    "prompt_render": frozenset(
        {
            "prompt:ctx",
            "prompt_render.build_ctx",
            "prompt_render.emit",
            "prompt_render.collect_exports",
            "prompt_render.return",
        }
    ),
    "before_step": frozenset(
        {
            "step:ctx",
            "step:abort_reply",
            "before_step.build_ctx",
            "before_step.emit",
            "before_step.collect_exports",
            "before_step.inject_hints",
            "before_step.return",
        }
    ),
    "after_step": frozenset(
        {
            "step:ctx",
            "after_step.copy_input",
            "after_step.fanout",
            "after_step.collect_telemetry",
            "after_step.return",
        }
    ),
    "after_reasoning": frozenset(
        {
            "reasoning:ctx",
            "after_reasoning.build_ctx",
            "after_reasoning.emit",
            "after_reasoning.persist_user",
            "after_reasoning.persist_assistant",
            "after_reasoning.collect_exports",
            "after_reasoning.return",
        }
    ),
    "after_turn": frozenset(
        {
            "turn:ctx",
            "turn:committed",
            "after_turn.build_work",
            "after_turn.fanout_committed",
            "after_turn.collect_telemetry",
            "after_turn.dispatch",
            "after_turn.return",
        }
    ),
}


class PhaseGraphError(ValueError):
    """A plugin phase graph is invalid and must not enter the runtime."""


@dataclass(frozen=True)
class PhaseGraph:
    phase_name: str
    modules: tuple[object, ...]
    anchors: frozenset[str]
    edges: tuple[tuple[str, str], ...]
    output_types: Mapping[str, type]

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase_name,
            "anchors": sorted(self.anchors),
            "order": [_module_slot(module) for module in self.modules],
            "edges": [list(edge) for edge in self.edges],
            "outputs": {
                name: value.__name__ for name, value in sorted(self.output_types.items())
            },
        }


class CompiledPhaseModules(Sequence[object]):
    """Live sequence whose validated graph can be replaced atomically."""

    def __init__(self, phase_name: str) -> None:
        self.phase_name = phase_name
        self.graph = compile_phase_modules((), phase_name=phase_name)

    def replace(self, modules: Sequence[object]) -> None:
        graph = compile_phase_modules(modules, phase_name=self.phase_name)
        self.graph = graph

    def __len__(self) -> int:
        return len(self.graph.modules)

    def __getitem__(self, index: int) -> object:
        return self.graph.modules[index]

    def __iter__(self) -> Iterator[object]:
        return iter(self.graph.modules)


def compile_phase_modules(
    modules: Sequence[object],
    *,
    phase_name: str,
    anchors: Collection[str] | None = None,
) -> PhaseGraph:
    """Validate and topologically compile a phase module graph."""
    if phase_name not in PHASE_NAMES:
        raise PhaseGraphError(f"Unknown lifecycle phase: {phase_name}")
    anchor_slots = frozenset(anchors or PHASE_ANCHOR_SLOTS[phase_name])
    module_by_slot: dict[str, object] = {}
    order: dict[str, int] = {}
    output_owner: dict[str, str] = {}
    output_types: dict[str, type] = {}

    for index, module in enumerate(modules):
        slot = _module_slot(module)
        if not slot:
            raise PhaseGraphError(
                f"PhaseModule missing slot: phase={phase_name} "
                f"module={module.__class__.__name__}"
            )
        if slot in anchor_slots or slot in module_by_slot or slot in output_owner:
            raise PhaseGraphError(
                f"Duplicate PhaseModule slot: phase={phase_name} slot={slot}"
            )
        module_by_slot[slot] = module
        order[slot] = index
        for produced in _module_produces(module):
            if produced in anchor_slots or produced in module_by_slot or produced in output_owner:
                owner = output_owner.get(produced, "built-in/module slot")
                raise PhaseGraphError(
                    f"Conflicting output slot: phase={phase_name} slot={produced} owner={owner}"
                )
            output_owner[produced] = slot
        for produced, value_type in _module_output_types(module).items():
            previous = output_types.get(produced)
            if previous is not None and previous is not value_type:
                raise PhaseGraphError(
                    f"Output type conflict: phase={phase_name} slot={produced} "
                    f"types={previous.__name__},{value_type.__name__}"
                )
            output_types[produced] = value_type

    producers = dict(output_owner)
    producers.update({slot: slot for slot in module_by_slot})
    edges: set[tuple[str, str]] = set()
    missing: dict[str, list[str]] = {}
    for slot, module in module_by_slot.items():
        for required in _module_requires(module):
            if required in anchor_slots:
                edges.add((required, slot))
                continue
            producer = producers.get(required)
            if producer is None:
                missing.setdefault(slot, []).append(required)
                continue
            if producer == slot:
                raise PhaseGraphError(
                    f"Cyclic/unreachable phase modules: phase={phase_name} slots={slot}"
                )
            edges.add((producer, slot))
    if missing:
        details = "; ".join(
            f"{slot} requires {','.join(values)}" for slot, values in sorted(missing.items())
        )
        raise PhaseGraphError(f"Missing phase dependency: phase={phase_name} {details}")

    indegree = {slot: 0 for slot in module_by_slot}
    children: dict[str, list[str]] = {slot: [] for slot in module_by_slot}
    for source, target in edges:
        if source in module_by_slot:
            indegree[target] += 1
            children[source].append(target)
    ready = sorted(
        (slot for slot, degree in indegree.items() if degree == 0),
        key=order.__getitem__,
    )
    sorted_modules: list[object] = []
    while ready:
        slot = ready.pop(0)
        sorted_modules.append(module_by_slot[slot])
        for child in sorted(children[slot], key=order.__getitem__):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort(key=order.__getitem__)
    if len(sorted_modules) != len(module_by_slot):
        cyclic = sorted(slot for slot, degree in indegree.items() if degree)
        raise PhaseGraphError(
            f"Cyclic/unreachable phase modules: phase={phase_name} slots={','.join(cyclic)}"
        )

    return PhaseGraph(
        phase_name=phase_name,
        modules=tuple(sorted_modules),
        anchors=anchor_slots,
        edges=tuple(sorted(edges)),
        output_types=output_types,
    )


def collect_prefixed_slots(
    slots: Mapping[str, object],
    prefix: str,
    *,
    reserved: Collection[str] = (),
) -> dict[str, object]:
    values: dict[str, object] = {}
    reserved_fields = set(reserved)
    for key, value in slots.items():
        if not key.startswith(prefix):
            continue
        field_name = key.removeprefix(prefix)
        if not field_name or field_name in reserved_fields:
            continue
        values[field_name] = value
    return values


def append_string_exports(target: list[str], exports: Mapping[str, object]) -> None:
    for key, value in exports.items():
        if isinstance(value, str) and value.strip():
            target.append(value)
            continue
        if isinstance(value, list):
            for item in cast(list[object], value):
                if isinstance(item, str) and item.strip():
                    target.append(item)
                elif item is not None:
                    logger.warning(
                        "忽略非字符串 slot export: key=%s type=%s",
                        key,
                        type(item).__name__,
                    )
            continue
        if value is not None:
            logger.warning(
                "忽略非字符串 slot export: key=%s type=%s",
                key,
                type(value).__name__,
            )


@dataclass
class PhaseFrame(Generic[I, O]):
    input: I
    slots: dict[str, Any] = field(default_factory=dict)
    output: O | None = None


class PhaseModule(Protocol[F]):
    async def run(self, frame: F) -> F:
        ...


class Phase(Generic[I, O, F]):
    def __init__(self, modules: Sequence[PhaseModule[F]]) -> None:
        self._modules = list(modules)
        self._validate()

    async def run(self, frame: F) -> O:
        for module in self._modules:
            frame = await module.run(frame)
        if frame.output is None:
            raise RuntimeError("Phase 模块链未产生 output")
        return frame.output

    async def run_frame(self, frame: F) -> F:
        for module in self._modules:
            frame = await module.run(frame)
        return frame

    def _validate(self) -> None:
        provided: set[str] = set()
        for index, module in enumerate(self._modules):
            requires = tuple(getattr(module, "requires", ()))
            produces = tuple(getattr(module, "produces", ()))
            for slot in requires:
                if slot not in provided:
                    logger.warning(
                        "Phase slot 未闭合: module=%d name=%s requires=%s",
                        index,
                        module.__class__.__name__,
                        slot,
                    )
            provided.update(str(slot) for slot in produces)


async def run_phase_modules(
    frame: F,
    modules: Sequence[PhaseModule[F]],
) -> F:
    for module in modules:
        frame = await module.run(frame)
    return frame

# 一个 Phase 里有多个插件，但每个插件可以运行的时间不同，如何自动决定先运行谁、后运行谁？
#  其实就是个有向无环图的调度器，因为有的插件可能依赖别的
class PhaseModuleRunner(Generic[F]):
    """Run plugin PhaseModules when their declared slots become available.

    Plugin modules are topological participants in a phase. A module declares a
    unique `slot`, the slots it `requires`, and any data slots it `produces`.
    The phase marks built-in anchor slots as it advances; after each anchor the
    runner executes every still-pending plugin whose dependencies are satisfied.
    """

    def __init__(self, modules: Sequence[object], *, phase_name: str = "") -> None:
        self._phase_name = phase_name
        if isinstance(modules, CompiledPhaseModules):
            self._graph = modules.graph
        else:
            self._graph = compile_phase_modules(modules, phase_name=phase_name)
        self._executed: set[str] = set()
        self._trace: list[dict[str, object]] = []

    async def run_ready(self, frame: F) -> F:
        # A single pass is sufficient because the graph was topologically
        # sorted at load time.  Modules waiting for a later built-in anchor are
        # reconsidered on the next phase checkpoint.
        for module in self._graph.modules:
            slot = _module_slot(module)
            if slot in self._executed:
                continue
            requires = set(_module_requires(module))
            if not requires.issubset(frame.slots):
                continue
            before = set(frame.slots)
            frame = await cast(Any, module).run(frame)
            frame.slots.setdefault(slot, True)
            for produced in _module_produces(module):
                frame.slots.setdefault(produced, frame.slots.get(produced))
            self._executed.add(slot)
            self._trace.append(
                {
                    "phase": self._phase_name,
                    "slot": slot,
                    "requires": sorted(requires),
                    "produced": sorted(set(frame.slots) - before),
                }
            )
        return frame

    @property
    def trace(self) -> tuple[dict[str, object], ...]:
        return tuple(dict(item) for item in self._trace)

    def warn_unresolved(self) -> None:
        pending = [
            module
            for module in self._graph.modules
            if _module_slot(module) not in self._executed
        ]
        if not pending:
            return
        for module in pending:
            missing = [
                slot for slot in _module_requires(module)
                if slot not in self._graph.anchors
            ]
            logger.warning(
                "PhaseModule unresolved: phase=%s module=%s slot=%s requires=%s missing_external=%s",
                self._phase_name,
                module.__class__.__name__,
                _module_slot(module),
                _module_requires(module),
                missing,
            )


def _module_slot(module: object) -> str:
    return str(getattr(module, "slot", "") or "").strip()


def _module_requires(module: object) -> tuple[str, ...]:
    return tuple(str(slot) for slot in getattr(module, "requires", ()) or ())


def _module_produces(module: object) -> tuple[str, ...]:
    produces = getattr(module, "produces", ()) or ()
    if isinstance(produces, Mapping):
        return tuple(str(slot) for slot in produces)
    return tuple(str(slot) for slot in produces)


def _module_output_types(module: object) -> dict[str, type]:
    declared = getattr(module, "output_types", None)
    if declared is None and isinstance(getattr(module, "produces", None), Mapping):
        declared = getattr(module, "produces")
    if not isinstance(declared, Mapping):
        return {}
    return {
        str(slot): value_type
        for slot, value_type in declared.items()
        if isinstance(value_type, type)
    }
