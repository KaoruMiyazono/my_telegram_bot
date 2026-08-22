from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from agent.core.types import BeforeReasoningCtx, Session
from agent.pipeline.reasoner import Reasoner
from agent.tools import Tool, ToolRegistry, ToolRuntime, register_tool_search


def _tool(name: str, description: str, handler=None) -> Tool:
    async def default_handler(arguments, ctx=None):
        return {"name": name, "arguments": arguments}

    return Tool(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
        },
        handler=handler or default_handler,
    )


def _names(schemas: list[dict]) -> set[str]:
    return {
        str(schema.get("function", {}).get("name", ""))
        for schema in schemas
        if schema.get("function", {}).get("name")
    }


def _ctx(*, session_key: str, tools: list[dict]) -> BeforeReasoningCtx:
    user_id = 1 if session_key == "session-a" else 2
    return BeforeReasoningCtx(
        session=Session(user_id=user_id, chat_id=user_id, session_key=session_key),
        memories=[],
        messages=[{"role": "user", "content": "帮我安排日程"}],
        tools=tools,
        session_key=session_key,
        content="帮我安排日程",
        turn_id=f"turn-{session_key}",
    )


def test_initial_projection_stays_small_with_one_hundred_extended_tools():
    registry = ToolRegistry(initial_max_tools=6)
    register_tool_search(registry)
    for index in range(100):
        registry.register(
            _tool(f"extended_{index}", f"扩展能力{index}"),
            tier="extended",
            search_hint=f"能力{index}",
        )

    visible = registry.get_initial_schemas(query="普通聊天", session_key="session-a")

    assert _names(visible) == {"tool_search"}
    assert len(registry.get_schemas()) == 101


def test_relevant_core_tools_are_preloaded_within_schema_budget():
    registry = ToolRegistry(initial_max_tools=3)
    register_tool_search(registry)
    registry.register(
        _tool("web_search", "搜索互联网最新公开信息"),
        tier="core",
        search_hint="联网搜索 最新新闻 价格",
        source_name="web",
    )
    registry.register(
        _tool("recall_memory", "检索长期记忆"),
        tier="core",
        search_hint="我的偏好 回忆 记忆",
        source_name="memory",
    )

    visible = registry.get_initial_schemas(
        query="帮我搜索今天的最新新闻",
        session_key="session-a",
    )

    assert _names(visible) == {"tool_search", "web_search"}


def test_catalog_search_matches_name_description_and_hint():
    registry = ToolRegistry()
    register_tool_search(registry)
    registry.register(
        _tool("calendar_lookup", "读取日历中的会议安排"),
        tier="extended",
        search_hint="日程 会议 空闲时间",
        source_type="plugin",
        source_name="calendar",
    )

    by_name = registry.search("calendar_lookup")
    by_description = registry.search("查询会议日程")

    assert by_name[0]["name"] == "calendar_lookup"
    assert by_description[0]["name"] == "calendar_lookup"
    assert by_description[0]["tier"] == "extended"
    assert by_description[0]["source_type"] == "plugin"


async def test_tool_search_unlocks_only_the_current_turn():
    registry = ToolRegistry()
    register_tool_search(registry)
    registry.register(
        _tool("calendar_lookup", "读取日历中的会议安排"),
        tier="extended",
        search_hint="日程 会议",
    )
    initial_a = registry.get_initial_schemas(query="普通聊天", session_key="session-a")
    initial_b = registry.get_initial_schemas(query="普通聊天", session_key="session-b")
    ctx_a = _ctx(session_key="session-a", tools=list(initial_a))
    ctx_b = _ctx(session_key="session-b", tools=list(initial_b))

    raw = await registry.execute(
        "tool_search",
        {"query": "select:calendar_lookup"},
        ctx_a,
    )

    assert '"unlocked": ["calendar_lookup"]' in raw
    assert "calendar_lookup" in _names(ctx_a.tools)
    assert "calendar_lookup" not in _names(ctx_b.tools)
    assert "calendar_lookup" not in _names(
        registry.get_initial_schemas(query="普通聊天", session_key="session-b")
    )


async def test_hidden_tool_direct_call_is_denied_before_handler_execution():
    handler = AsyncMock(return_value="should-not-run")
    registry = ToolRegistry()
    register_tool_search(registry)
    registry.register(
        _tool("calendar_lookup", "读取日历", handler=handler),
        tier="extended",
    )
    runtime = ToolRuntime(registry=registry)

    result = await runtime.execute_call(
        call_id="call-hidden",
        tool_name="calendar_lookup",
        raw_arguments={},
        allowed_tool_names=frozenset({"tool_search"}),
    )

    assert result.status == "denied"
    assert result.error_code == "tool_locked"
    assert "tool_search" in result.message
    handler.assert_not_awaited()


def test_used_tool_enters_only_its_session_lru_for_next_turn():
    registry = ToolRegistry(session_lru_size=2)
    register_tool_search(registry)
    registry.register(
        _tool("calendar_lookup", "读取日历"),
        tier="extended",
        preloadable=True,
    )

    registry.remember_tool_use("session-a", "calendar_lookup")

    assert "calendar_lookup" in _names(
        registry.get_initial_schemas(query="普通聊天", session_key="session-a")
    )
    assert "calendar_lookup" not in _names(
        registry.get_initial_schemas(query="普通聊天", session_key="session-b")
    )


async def test_reasoner_search_then_calls_newly_unlocked_schema():
    with patch("agent.pipeline.reasoner.AsyncOpenAI") as client_type:
        client = AsyncMock()
        first = MagicMock()
        first.choices = [
            _choice(
                "",
                [_tool_call("search-1", "tool_search", '{"query":"select:calendar_lookup"}')],
                "tool_calls",
            )
        ]
        second = MagicMock()
        second.choices = [
            _choice(
                "",
                [_tool_call("calendar-1", "calendar_lookup", '{"query":"明天"}')],
                "tool_calls",
            )
        ]
        third = MagicMock()
        third.choices = [_choice("明天上午十点有项目会议。", [], "stop")]
        client.chat.completions.create = AsyncMock(side_effect=[first, second, third])
        client_type.return_value = client

        registry = ToolRegistry()
        register_tool_search(registry)
        registry.register(
            _tool("calendar_lookup", "读取日历中的会议安排"),
            tier="extended",
            search_hint="日历 日程 会议",
        )
        initial = registry.get_initial_schemas(query="帮我安排日程", session_key="session-a")
        ctx = _ctx(session_key="session-a", tools=initial)
        reasoner = Reasoner(tool_registry=registry)

        result = await reasoner.run_turn(ctx)

        request_tools = [
            _names(call.kwargs["tools"])
            for call in client.chat.completions.create.await_args_list
        ]
        assert request_tools[0] == {"tool_search"}
        assert "calendar_lookup" in request_tools[1]
        assert result.content == "明天上午十点有项目会议。"
        assert [item["function"]["name"] for item in result.tool_calls] == [
            "tool_search",
            "calendar_lookup",
        ]
        assert "calendar_lookup" in registry.discovery.get_preloaded("session-a")


def _tool_call(call_id: str, name: str, arguments: str):
    call = MagicMock()
    call.id = call_id
    call.type = "function"
    call.function.name = name
    call.function.arguments = arguments
    return call


def _choice(content: str, tool_calls: list, finish_reason: str):
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = tool_calls
    choice.finish_reason = finish_reason
    return choice
