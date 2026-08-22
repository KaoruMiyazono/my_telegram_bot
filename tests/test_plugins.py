import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["TG_BOT_TOKEN"] = "test_token"
os.environ["DEEPSEEK_API_KEY"] = "test_deepseek_key"
os.environ["ALIYUN_DASHSCOPE_API_KEY"] = "test_aliyun_key"
os.environ["DEEPSEEK_BASE_URL"] = "https://api.test.com"
os.environ["LLM_MODEL"] = "test-model"

from agent.core.event_bus import EventBus
from agent.core.types import (
    AfterTurnCtx,
    BeforeTurnCtx,
    InboundMessage,
    ReasonerResult,
    Session,
)
from agent.plugins import PluginManager
from agent.pipeline.phases.after_reasoning import AfterReasoningPhase
from agent.pipeline.phases.after_turn import AfterTurnPhase
from agent.pipeline.phases.before_reasoning import BeforeReasoningPhase
from agent.pipeline.reasoner import Reasoner
from agent.lifecycle.types import BeforeReasoningCtx
from agent.tool_hooks import ToolExecutor
from agent.tool_hooks.types import ToolExecutionRequest
from agent.tools import ToolRegistry


def _write_plugin(root: Path) -> None:
    plugin_dir = root / "plugins" / "01_sample"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text(
        '''
from agent.plugins import Plugin, on_after_turn, on_before_reasoning, on_tool_pre, tool


class ReasoningSlotModule:
    slot = "sample.before_reasoning_hint"
    requires = ("before_reasoning.emit",)

    async def run(self, frame):
        frame.slots["reasoning:extra_hint:sample"] = "slot-hint"
        return frame


class PromptBottomModule:
    slot = "sample.prompt_section"
    requires = ("prompt_render.emit",)

    async def run(self, frame):
        frame.slots["prompt:section_bottom:sample"] = "## Plugin Section\\nplugin-section"
        return frame


class AfterReasoningSlotModule:
    slot = "sample.outbound_metadata"
    requires = ("after_reasoning.emit",)

    async def run(self, frame):
        frame.slots["outbound:metadata:sample"] = "metadata-from-slot"
        return frame


class AfterTurnTelemetryModule:
    slot = "sample.turn_telemetry"
    requires = ("after_turn.fanout_committed",)

    async def run(self, frame):
        frame.slots["turn:telemetry:sample"] = "telemetry-from-slot"
        return frame


class BeforeStepStopModule:
    slot = "sample.before_step_stop"
    requires = ("before_step.emit",)

    async def run(self, frame):
        frame.slots["step:abort_reply"] = "stopped-by-step"
        return frame


class Sample(Plugin):
    name = "sample"

    async def initialize(self):
        self.context.kv_store.set("memory_engine_present", self.context.memory_engine is not None)

    @tool(name="echo_plugin", risk="read-only", search_hint="echo")
    async def echo_plugin(self, event, text: str) -> str:
        """Echo text.

        Args:
            text: Text to echo.
        """
        return f"echo:{text}"

    @on_tool_pre(tool_name="echo_plugin")
    async def rewrite_echo(self, event):
        return dict(event.arguments, text=event.arguments.get("text", "") + ":hooked")

    @on_before_reasoning(priority=10)
    async def add_hint(self, event):
        event.extra_hints.append("plugin-hint")
        return event

    @on_after_turn()
    async def count_turn(self, event):
        self.context.kv_store.increment("turns")

    def before_reasoning_modules(self):
        return [ReasoningSlotModule()]

    def prompt_render_modules(self):
        return [PromptBottomModule()]

    def before_step_modules(self):
        return [BeforeStepStopModule()]

    def after_reasoning_modules(self):
        return [AfterReasoningSlotModule()]

    def after_turn_modules(self):
        return [AfterTurnTelemetryModule()]
''',
        encoding="utf-8",
    )


async def test_plugin_manager_registers_tool_hook_and_lifecycle():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_plugin(root)

        event_bus = EventBus()
        registry = ToolRegistry()
        memory_engine = object()
        manager = PluginManager(
            [root / "plugins"],
            event_bus=event_bus,
            tool_registry=registry,
            workspace=root,
            memory_engine=memory_engine,
        )
        await manager.load_all()

        assert manager.loaded_count == 1
        kv_payload = json.loads((root / "plugins" / "01_sample" / ".kv.json").read_text())
        assert kv_payload["memory_engine_present"] is True
        assert registry.has_tool("echo_plugin")
        schema = registry.get_schemas()[0]["function"]
        assert schema["name"] == "echo_plugin"
        assert "text" in schema["parameters"]["properties"]

        executor = ToolExecutor(manager.tool_hooks)
        result = await executor.execute(
            ToolExecutionRequest(
                call_id="call_1",
                tool_name="echo_plugin",
                arguments={"text": "hello"},
                source="passive",
                session_key="1:2",
                channel="telegram",
                chat_id="2",
            ),
            lambda name, args: registry.execute(name, args),
        )
        assert result.status == "success"
        assert result.output == "echo:hello:hooked"

        ctx = BeforeReasoningCtx(
            session=Session(user_id=1, chat_id=2),
            memories=[],
            messages=[],
            tools=[],
        )
        emitted = await event_bus.emit(ctx)
        assert emitted.extra_hints == ["plugin-hint"]

        phase = BeforeReasoningPhase(
            event_bus=event_bus,
            tool_registry=registry,
            plugin_modules=manager.before_reasoning_modules,
            prompt_render_modules=manager.prompt_render_modules,
        )
        rendered = await phase.build_ctx(
            BeforeTurnCtx(
                inbound_message=InboundMessage(user_id=1, chat_id=2, content="hello"),
                session=Session(user_id=1, chat_id=2),
                retrieved_memories=[],
                content="hello",
                session_key="1:2",
                chat_id="2",
            )
        )
        system_prompt = rendered.messages[0]["content"]
        assert "plugin-section" in system_prompt
        assert "slot-hint" in system_prompt

        reasoner = Reasoner(
            event_bus=event_bus,
            before_step_modules=manager.before_step_modules,
        )
        early_stop = await reasoner.run_turn(
            BeforeReasoningCtx(
                session=Session(user_id=1, chat_id=2),
                memories=[],
                messages=[{"role": "user", "content": "hello"}],
                tools=[],
                content="hello",
                session_key="1:2",
                chat_id="2",
            )
        )
        assert early_stop.content == "stopped-by-step"
        assert early_stop.finish_reason == "early_stop"

        after_reasoning = AfterReasoningPhase(
            store=None,
            event_bus=event_bus,
            plugin_modules=manager.after_reasoning_modules,
        )
        after_ctx = await after_reasoning.build_ctx(
            result=ReasonerResult(content="ok", tool_calls=[], finish_reason="stop"),
            session=Session(user_id=1, chat_id=2),
            chat_id=2,
            user_id=1,
        )
        assert after_ctx.outbound_metadata["sample"] == "metadata-from-slot"

        observed_turns = []

        async def observe_turn(event: AfterTurnCtx):
            observed_turns.append(event)

        event_bus.observe(AfterTurnCtx, observe_turn)
        after_turn = AfterTurnPhase(
            event_bus,
            None,
            plugin_modules=manager.after_turn_modules,
        )
        await after_turn.execute(
            ctx=after_ctx,
            user_id=1,
            new_memory_ids=[],
            inbound_content="hello",
        )
        assert observed_turns[-1].extra_metadata["sample"] == "telemetry-from-slot"

        await manager.terminate_all()
        assert not registry.has_tool("echo_plugin")
        print("test_plugin_manager_registers_tool_hook_and_lifecycle: PASS")


async def main() -> None:
    await test_plugin_manager_registers_tool_hook_and_lifecycle()
    print("\nAll plugin tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
