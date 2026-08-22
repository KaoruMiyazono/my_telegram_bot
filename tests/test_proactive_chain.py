import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.tools.message_push import MessagePushTool
from agent.tools.registry import ToolRegistry
from proactive_v2.agent_tick import AgentTick, ProactiveDecision
from proactive_v2.gateway import DataGateway
from proactive_v2.loop import ProactiveLoop


async def test_message_push_tool_sends_text():
    sent: list[tuple[str, str]] = []
    push = MessagePushTool()

    async def send_text(chat_id: str, message: str) -> None:
        sent.append((chat_id, message))

    push.register_channel("telegram", text=send_text)
    result = await push.execute(channel="telegram", chat_id="42", message="hello")

    assert result == "文本已发送"
    assert sent == [("42", "hello")]
    print("test_message_push_tool_sends_text: PASS")


async def test_message_push_tool_mounts_in_registry():
    sent: list[tuple[str, str]] = []
    push = MessagePushTool()

    async def send_text(chat_id: str, message: str) -> None:
        sent.append((chat_id, message))

    push.register_channel("telegram", text=send_text)
    registry = ToolRegistry()
    registry.register(push.as_tool(), risk="read-write", always_on=True)
    result = await registry.execute(
        "message_push",
        {"channel": "telegram", "chat_id": "42", "message": "from-registry"},
    )

    assert result == "文本已发送"
    assert sent == [("42", "from-registry")]
    print("test_message_push_tool_mounts_in_registry: PASS")


async def test_gateway_prefetches_sources():
    async def alerts():
        return [{"id": "a1", "title": "Alert one"}]

    async def context():
        return [{"kind": "recent", "text": "quiet"}]

    async def feed(limit: int):
        assert limit == 2
        return [{"id": "f1", "title": "Feed one", "url": "https://example.test/a"}]

    class WebFetch:
        async def execute(self, **kwargs):
            assert kwargs["url"] == "https://example.test/a"
            return "article body"

    gateway = DataGateway(
        alert_fn=alerts,
        context_fn=context,
        feed_fn=feed,
        web_fetch_tool=WebFetch(),
        content_limit=2,
    )
    result = await gateway.run()

    assert result.alerts[0]["id"] == "a1"
    assert result.context[0]["kind"] == "recent"
    assert result.content_meta[0]["id"] == "feed:f1"
    assert result.content_store["feed:f1"] == "article body"
    print("test_gateway_prefetches_sources: PASS")


async def test_agent_tick_pushes_alert():
    sent: list[tuple[str, str]] = []
    push = MessagePushTool()

    async def send_text(chat_id: str, message: str) -> None:
        sent.append((chat_id, message))

    push.register_channel("telegram", text=send_text)

    async def alerts():
        return [{"ack_server": "alert", "id": "a1", "title": "站点告警", "body": "CPU 高"}]

    tick = AgentTick(
        gateway=DataGateway(alert_fn=alerts),
        push_tool=push,
        default_channel="telegram",
        default_chat_id="42",
    )
    result = await tick.tick()

    assert result is not None
    assert result.sent is True
    assert result.decision == "reply"
    assert result.evidence == ["alert:a1"]
    assert sent == [("42", "站点告警\nCPU 高")]
    print("test_agent_tick_pushes_alert: PASS")


async def test_proactive_loop_run_once_uses_agent_tick():
    sent: list[tuple[str, str]] = []
    push = MessagePushTool()

    async def send_text(chat_id: str, message: str) -> None:
        sent.append((chat_id, message))

    def decide(_gateway_result):
        return ProactiveDecision(
            decision="reply",
            message="manual decision",
            score=0.9,
            reason="test",
        )

    push.register_channel("telegram", text=send_text)
    loop = ProactiveLoop(
        AgentTick(
            gateway=DataGateway(),
            push_tool=push,
            default_channel="telegram",
            default_chat_id="42",
            decision_fn=decide,
        ),
        interval_seconds=1,
    )
    result = await loop.run_once()

    assert result is not None
    assert result.sent is True
    assert sent == [("42", "manual decision")]
    print("test_proactive_loop_run_once_uses_agent_tick: PASS")


async def main() -> None:
    await test_message_push_tool_sends_text()
    await test_message_push_tool_mounts_in_registry()
    await test_gateway_prefetches_sources()
    await test_agent_tick_pushes_alert()
    await test_proactive_loop_run_once_uses_agent_tick()
    print("\nAll proactive chain tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
