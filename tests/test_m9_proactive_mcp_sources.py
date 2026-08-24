from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agent.mcp.manager import McpManager
from agent.mcp.spec import McpServerSpec
from agent.tools.message_push import MessagePushTool
from agent.tools.registry import ToolRegistry
from persistence.database import init_db
from proactive_v2.agent_tick import AgentTick
from proactive_v2.contracts import ProactivePolicy
from proactive_v2.mcp_sources import (
    McpManagerSourceCaller,
    McpProactiveGateway,
    McpProactiveSourceSpec,
    ProactiveSourceRegistry,
    load_proactive_source_specs,
    register_proactive_source_management_tools,
)
from proactive_v2.state import ProactiveStateStore


class FakeCaller:
    def __init__(self, responses: dict[tuple[str, str], Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def call(
        self,
        server: str,
        tool_name: str,
        arguments: dict[str, object],
    ) -> Any:
        self.calls.append((server, tool_name, dict(arguments)))
        value = self.responses[(server, tool_name)]
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            return value(arguments)
        return value


def source(
    source_id: str,
    channel: str,
    fetch_tool: str,
    *,
    ack_tool: str = "ack_events",
    enabled: bool = True,
    page_size: int = 0,
) -> McpProactiveSourceSpec:
    return McpProactiveSourceSpec(
        plugin_id="test",
        source_id=source_id,
        channels=(channel,),  # type: ignore[arg-type]
        mcp_server="shared",
        fetch_tool=fetch_tool,
        ack_tool="" if channel == "context" else ack_tool,
        enabled=enabled,
        page_size=page_size,
    )


def registry(*specs: McpProactiveSourceSpec) -> ProactiveSourceRegistry:
    return ProactiveSourceRegistry({spec.key: spec for spec in specs})


def test_shipped_config_declares_three_channels_on_one_server() -> None:
    config_path = Path(__file__).parent.parent / "config" / "proactive_sources.toml"
    specs = load_proactive_source_specs(config_path)

    assert set(specs) == {
        "builtin:health_alerts",
        "builtin:news_content",
        "builtin:user_context",
    }
    assert {spec.mcp_server for spec in specs.values()} == {"proactive_demo"}
    assert {channel for spec in specs.values() for channel in spec.channels} == {
        "alert", "content", "context"
    }
    assert all(not spec.enabled for spec in specs.values())
    assert specs["builtin:user_context"].ack_tool == ""


@pytest.mark.asyncio
async def test_sources_enable_disable_without_restarting_shared_server() -> None:
    alerts = source("alerts", "alert", "fetch_alerts", enabled=False)
    sources = registry(alerts)
    tools = ToolRegistry()
    register_proactive_source_management_tools(tools, sources)

    initial = json.loads(await tools.execute("proactive_source_list", {}))
    assert initial["sources"][0]["runtime_enabled"] is False

    enabled = json.loads(
        await tools.execute(
            "proactive_source_enable", {"source_key": "test:alerts"}
        )
    )
    assert enabled["runtime_enabled"] is True
    assert sources.enabled() == (alerts,)

    disabled = json.loads(
        await tools.execute(
            "proactive_source_disable", {"source_key": "test:alerts"}
        )
    )
    assert disabled["runtime_enabled"] is False
    assert sources.enabled() == ()


@pytest.mark.asyncio
async def test_one_server_populates_alert_content_context_snapshot() -> None:
    caller = FakeCaller(
        {
            ("shared", "fetch_alerts"): [
                {"event_id": "a1", "title": "CPU", "body": "95%"}
            ],
            ("shared", "fetch_content"): [
                {
                    "event_id": "n1",
                    "title": "Agent news",
                    "content": "article body",
                    "relevance_score": 0.9,
                }
            ],
            ("shared", "fetch_context"): {
                "online": True,
                "ack_server": "must-be-removed",
            },
        }
    )
    sources = registry(
        source("alerts", "alert", "fetch_alerts"),
        source("news", "content", "fetch_content"),
        source("presence", "context", "fetch_context"),
    )

    result = await McpProactiveGateway(caller, sources).run()

    assert result.alerts[0]["event_id"] == "a1"
    assert result.alerts[0]["ack_server"] == "test:alerts"
    assert result.content_meta[0]["id"] == "test:news:n1"
    assert result.content_meta[0]["event_id"] == "n1"
    assert result.content_meta[0]["relevance_score"] == 0.9
    assert result.content_store["test:news:n1"] == "article body"
    assert result.context == [
        {"online": True, "kind": "context", "_source": "test:presence"}
    ]
    assert result.source_failures == {}
    assert len(caller.calls) == 3


@pytest.mark.asyncio
async def test_bad_source_schema_is_isolated_from_other_sources() -> None:
    caller = FakeCaller(
        {
            ("shared", "bad_fetch"): {"not": "a list"},
            ("shared", "good_fetch"): [
                {"event_id": "a2", "title": "healthy source"}
            ],
        }
    )
    sources = registry(
        source("bad", "alert", "bad_fetch"),
        source("good", "alert", "good_fetch"),
    )

    result = await McpProactiveGateway(caller, sources).run()

    assert [item["event_id"] for item in result.alerts] == ["a2"]
    assert "test:bad" in result.source_failures
    assert "test:good" not in result.source_failures


@pytest.mark.asyncio
async def test_invalid_item_is_quarantined_without_losing_valid_sibling() -> None:
    caller = FakeCaller(
        {
            ("shared", "fetch_alerts"): [
                {"event_id": "good", "title": "valid"},
                {"title": "missing provider id"},
                "not-an-object",
            ]
        }
    )
    gateway = McpProactiveGateway(
        caller,
        registry(source("alerts", "alert", "fetch_alerts")),
    )

    result = await gateway.run()

    assert [item["event_id"] for item in result.alerts] == ["good"]
    assert [item["item_id"] for item in result.quarantined] == [
        "index:1", "index:2"
    ]
    assert result.source_failures == {}


@pytest.mark.asyncio
async def test_paged_source_uses_provider_offset_and_limit() -> None:
    all_items = [
        {"event_id": f"e{index}", "title": str(index)} for index in range(5)
    ]

    def page(arguments: dict[str, object]) -> list[dict[str, str]]:
        offset = int(arguments["offset"])
        limit = int(arguments["limit"])
        return all_items[offset : offset + limit]

    caller = FakeCaller({("shared", "fetch_alerts"): page})
    gateway = McpProactiveGateway(
        caller,
        registry(
            source("alerts", "alert", "fetch_alerts", page_size=2)
        ),
    )

    result = await gateway.run()

    assert [item["event_id"] for item in result.alerts] == [
        "e0", "e1", "e2", "e3", "e4"
    ]
    assert [call[2] for call in caller.calls] == [
        {"offset": 0, "limit": 2},
        {"offset": 2, "limit": 2},
        {"offset": 4, "limit": 2},
    ]


@pytest.mark.asyncio
async def test_exact_ack_and_context_never_creates_ack_handler() -> None:
    def ack(arguments: dict[str, object]) -> dict[str, object]:
        return {
            "status": "committed",
            "ids": list(arguments["event_ids"]),  # type: ignore[arg-type]
        }

    caller = FakeCaller({("shared", "ack_events"): ack})
    gateway = McpProactiveGateway(
        caller,
        registry(
            source("alerts", "alert", "fetch_alerts"),
            source("presence", "context", "fetch_context"),
        ),
    )

    handlers = gateway.ack_handlers()
    assert set(handlers) == {"test:alerts"}
    await handlers["test:alerts"]("a1", "delivered")
    assert caller.calls == [
        (
            "shared",
            "ack_events",
            {"event_ids": ["a1"], "feedback": "delivered"},
        )
    ]


@pytest.mark.asyncio
async def test_ack_rejects_unknown_or_partial_provider_response() -> None:
    caller = FakeCaller(
        {("shared", "ack_events"): {"status": "committed", "ids": []}}
    )
    gateway = McpProactiveGateway(
        caller,
        registry(source("alerts", "alert", "fetch_alerts")),
    )

    with pytest.raises(ValueError, match="exactly match"):
        await gateway.ack_handlers()["test:alerts"]("a1", "delivered")


@pytest.mark.asyncio
async def test_optional_ack_tool_becomes_safe_local_noop() -> None:
    caller = FakeCaller({})
    gateway = McpProactiveGateway(
        caller,
        registry(
            source(
                "fire-and-forget",
                "alert",
                "fetch_alerts",
                ack_tool="",
            )
        ),
    )

    await gateway.ack_handlers()["test:fire-and-forget"]("a1", "delivered")
    assert caller.calls == []


@pytest.mark.asyncio
async def test_real_stdio_mcp_server_fetches_three_sources_and_acks() -> None:
    init_db()
    fixture = Path(__file__).parent / "fixtures" / "mcp_proactive_server.py"
    spec = McpServerSpec(
        name="proactive",
        transport="stdio",
        command=sys.executable,
        args=(str(fixture),),
        connect_timeout=10,
        call_timeout=10,
    )
    manager = McpManager(
        registry=ToolRegistry(),
        specs={"proactive": spec},
        allowed_commands={Path(sys.executable).name},
    )
    await manager.add("proactive")
    try:
        with pytest.raises(ValueError, match="Unknown MCP tool"):
            await manager.call_tool("proactive", "missing_fetch_tool", {})
        assert manager.state_store.get("proactive").status == "ready"  # type: ignore[union-attr]

        specs = [
            McpProactiveSourceSpec(
                "test", "alerts", ("alert",), "proactive", "fetch_alerts",
                "ack_events", 50, True,
            ),
            McpProactiveSourceSpec(
                "test", "news", ("content",), "proactive", "fetch_content",
                "ack_events", 50, True,
            ),
            McpProactiveSourceSpec(
                "test", "presence", ("context",), "proactive", "fetch_context",
                "", 0, True,
            ),
        ]
        gateway = McpProactiveGateway(
            McpManagerSourceCaller(manager),
            registry(*specs),
        )

        first = await gateway.run()
        assert first.alerts[0]["event_id"] == "cpu-95"
        assert first.content_meta[0]["event_id"] == "news-001"
        assert first.context[0]["online"] is True

        handlers = gateway.ack_handlers()
        await handlers["test:alerts"]("cpu-95", "delivered")
        await handlers["test:news"]("news-001", "delivered")

        second = await gateway.run()
        assert second.alerts == []
        assert second.content_meta == []
        assert len(second.context) == 1
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_mcp_source_to_delivery_outbox_and_exact_ack_end_to_end() -> None:
    def ack(arguments: dict[str, object]) -> dict[str, object]:
        return {
            "status": "committed",
            "ids": list(arguments["event_ids"]),  # type: ignore[arg-type]
        }

    caller = FakeCaller(
        {
            ("shared", "fetch_alerts"): [
                {"event_id": "end-to-end", "title": "磁盘告警", "body": "剩余 5%"}
            ],
            ("shared", "ack_events"): ack,
        }
    )
    gateway = McpProactiveGateway(
        caller,
        registry(source("alerts", "alert", "fetch_alerts")),
    )
    push = MessagePushTool()
    sent: list[tuple[str, str]] = []

    async def send_text(chat_id: str, message: str) -> None:
        sent.append((chat_id, message))

    push.register_channel("telegram", text=send_text)
    state = ProactiveStateStore(":memory:")
    tick = AgentTick(
        gateway=gateway,
        push_tool=push,
        default_chat_id="42",
        session_key="telegram:42:42",
        mode="live",
        policy=ProactivePolicy(
            cooldown_seconds=0,
            daily_limit=99,
            quiet_start_hour=0,
            quiet_end_hour=0,
        ),
        state_store=state,
        ack_handlers=gateway.ack_handlers(),
    )

    result = await tick.tick()

    assert result is not None and result.sent is True
    assert result.evidence == ["test:alerts:end-to-end"]
    assert sent == [("42", "磁盘告警\n剩余 5%")]
    assert state.delivery_count("telegram:42:42") == 1
    assert state.ack_status(result.ack_outbox_ids[0]) == "acked"
    assert caller.calls[-1] == (
        "shared",
        "ack_events",
        {"event_ids": ["end-to-end"], "feedback": "delivered"},
    )
