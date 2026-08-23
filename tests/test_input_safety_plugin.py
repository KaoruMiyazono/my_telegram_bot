from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from agent.core.event_bus import EventBus
from agent.core.types import InboundMessage, Session
from agent.pipeline.phases.before_turn import BeforeTurnPhase, _sessions
from agent.plugins import PluginManager
from memory.engine import MemoryRetrieveResult


class _RecordingMemoryEngine:
    def __init__(self) -> None:
        self.requests: list[object] = []

    async def retrieve(self, request: object) -> MemoryRetrieveResult:
        self.requests.append(request)
        return MemoryRetrieveResult(items=[], text_block="", trace={})


def _copy_plugin(tmp_path: Path, *, max_chars: int) -> Path:
    source = Path(__file__).parent.parent / "plugins" / "01_input_safety"
    target_root = tmp_path / "plugins"
    target = target_root / "01_input_safety"
    shutil.copytree(source, target)
    (target / "plugin_config.json").write_text(
        json.dumps({"max_chars": max_chars}),
        encoding="utf-8",
    )
    return target_root


@pytest.mark.asyncio
async def test_input_safety_accepts_normal_message(tmp_path: Path) -> None:
    _sessions.clear()
    _sessions[(71, 81)] = Session(user_id=71, chat_id=81)
    manager = PluginManager(
        [_copy_plugin(tmp_path, max_chars=20)],
        event_bus=EventBus(),
    )
    await manager.load_all()
    engine = _RecordingMemoryEngine()
    phase = BeforeTurnPhase(
        memory_engine=engine,
        plugin_modules=manager.before_turn_modules,
    )

    ctx = await phase.build_ctx(
        InboundMessage(user_id=71, chat_id=81, content="正常消息")
    )

    assert ctx.abort is False
    assert len(engine.requests) == 1
    assert manager.phase_graphs["before_turn"]["order"] == [
        "input_safety.validate"
    ]
    await manager.terminate_all()


@pytest.mark.asyncio
async def test_input_safety_blocks_oversized_message_before_retrieval(
    tmp_path: Path,
) -> None:
    _sessions.clear()
    _sessions[(72, 82)] = Session(user_id=72, chat_id=82)
    manager = PluginManager(
        [_copy_plugin(tmp_path, max_chars=5)],
        event_bus=EventBus(),
    )
    await manager.load_all()
    engine = _RecordingMemoryEngine()
    phase = BeforeTurnPhase(
        memory_engine=engine,
        plugin_modules=manager.before_turn_modules,
    )

    ctx = await phase.build_ctx(
        InboundMessage(user_id=72, chat_id=82, content="123456")
    )

    assert ctx.abort is True
    assert "不超过 5 个字符" in ctx.abort_reply
    assert engine.requests == []
    await manager.terminate_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["hello\x00world", "hello\x01world"])
async def test_input_safety_blocks_unsafe_control_characters(
    tmp_path: Path,
    content: str,
) -> None:
    _sessions.clear()
    _sessions[(73, 83)] = Session(user_id=73, chat_id=83)
    manager = PluginManager(
        [_copy_plugin(tmp_path, max_chars=100)],
        event_bus=EventBus(),
    )
    await manager.load_all()
    engine = _RecordingMemoryEngine()
    phase = BeforeTurnPhase(
        memory_engine=engine,
        plugin_modules=manager.before_turn_modules,
    )

    ctx = await phase.build_ctx(
        InboundMessage(user_id=73, chat_id=83, content=content)
    )

    assert ctx.abort is True
    assert "控制字符" in ctx.abort_reply
    assert engine.requests == []
    await manager.terminate_all()
