from __future__ import annotations

import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar, cast

logger = logging.getLogger(__name__)

I = TypeVar("I")
O = TypeVar("O")
F = TypeVar("F", bound="PhaseFrame[Any, Any]")


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

    def __init__(self, modules: Sequence[PhaseModule[F]], *, phase_name: str = "") -> None:
        self._phase_name = phase_name
        self._pending = list(modules)
        self._slots: dict[str, PhaseModule[F]] = {}
        for module in self._pending:
            slot = _module_slot(module)
            if not slot:
                raise ValueError(
                    f"PhaseModule missing slot: phase={phase_name} "
                    f"module={module.__class__.__name__}"
                )
            if slot in self._slots:
                raise ValueError(
                    f"Duplicate PhaseModule slot: phase={phase_name} slot={slot}"
                )
            self._slots[slot] = module

    async def run_ready(self, frame: F) -> F:
        progressed = True
        while progressed:
            progressed = False
            available = set(frame.slots)
            for module in list(self._pending):
                requires = set(_module_requires(module))
                if not requires.issubset(available):
                    continue
                frame = await module.run(frame)
                slot = _module_slot(module)
                if slot:
                    frame.slots.setdefault(slot, True)
                for produced in _module_produces(module):
                    frame.slots.setdefault(str(produced), frame.slots.get(str(produced)))
                self._pending.remove(module)
                progressed = True
                break
        return frame

    def warn_unresolved(self) -> None:
        if not self._pending:
            return
        for module in self._pending:
            missing = [
                slot for slot in _module_requires(module)
                if slot not in self._slots
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
    return tuple(str(slot) for slot in getattr(module, "produces", ()) or ())
