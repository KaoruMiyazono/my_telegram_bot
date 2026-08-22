import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["TG_BOT_TOKEN"] = "test_token"
os.environ["DEEPSEEK_API_KEY"] = "test_deepseek_key"
os.environ["ALIYUN_DASHSCOPE_API_KEY"] = "test_aliyun_key"
os.environ["DATABASE_PATH"] = tempfile.mktemp(suffix=".db")
os.environ["DEEPSEEK_BASE_URL"] = "https://api.test.com"
os.environ["LLM_MODEL"] = "test-model"

from agent.core.types import BeforeReasoningCtx, MemoryItem, Session
from agent.pipeline.reasoner import Reasoner
from agent.prompting import PromptSectionRender
from agent.tools import Tool, ToolRegistry
from agent.tools.memory import register_memory_tools
from memory.engine import DefaultMemoryEngine


def _mock_choice(content: str, tool_calls=None, finish_reason="stop"):
    """Helper to create a mock choice."""
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = tool_calls or []
    choice.finish_reason = finish_reason
    return choice


def _mock_tool_call(id: str, name: str, arguments: str):
    """Helper to create a mock tool call."""
    tc = MagicMock()
    tc.id = id
    tc.type = "function"
    tc.function.name = name
    tc.function.arguments = arguments
    return tc


def _make_ctx(user_id=1, chat_id=1, messages=None, memories=None, tools=None):
    """Helper to create a BeforeReasoningCtx."""
    if messages is None:
        messages = [{"role": "user", "content": "你好"}]
    return BeforeReasoningCtx(
        session=Session(user_id=user_id, chat_id=chat_id, messages=[]),
        memories=memories or [],
        messages=messages,
        tools=tools or [],
    )


def _runtime_payload(raw: str) -> dict:
    payload = json.loads(raw)
    assert isinstance(payload, dict)
    return payload


async def _no_aux_queries(_query: str) -> list[str]:
    return []


def _make_memory_reasoner(
    *,
    store=None,
    embedder=None,
    session_store=None,
) -> tuple[Reasoner, ToolRegistry]:
    """Build the P5b path: Reasoner -> ToolRegistry -> MemoryEngine."""
    engine = DefaultMemoryEngine(
        store=store or AsyncMock(),
        embedder=embedder or AsyncMock(),
        session_store=session_store or MagicMock(),
        aux_query_builder=_no_aux_queries,
    )
    registry = ToolRegistry()
    register_memory_tools(registry, engine)
    return Reasoner(tool_registry=registry), registry


async def test_simple_response():
    """Test simple response without tool calls."""
    with patch("agent.pipeline.reasoner.AsyncOpenAI") as MockClient:
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.choices = [_mock_choice("你好！我是你的助手。")]
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        MockClient.return_value = mock_client

        reasoner = Reasoner()
        ctx = _make_ctx(messages=[{"role": "user", "content": "你好"}])

        result = await reasoner.run_turn(ctx)

        assert result.content == "你好！我是你的助手。"
        assert result.tool_calls == []
        assert result.finish_reason == "stop"
        print("test_simple_response: PASS")


async def test_recall_memory_tool():
    """Test recall_memory tool call. 需要 store + embedder mock。"""
    mock_store = AsyncMock()
    mock_embedder = AsyncMock()
    mock_embedder.embed = AsyncMock(return_value=[0.1] * 256)
    mock_session_store = MagicMock()
    mock_session_store.fetch_messages.return_value = (
        [
            {
                "role": "user",
                "content": "我是程序员",
                "seq": 0,
                "source_ref": "session:1:1#msg:0",
                "in_source_ref": True,
            }
        ],
        1,
    )

    from uuid import uuid4
    mock_item = MemoryItem(
        id=uuid4(),
        user_id=1,
        memory_type="profile",
        summary="用户是程序员",
        embedding=[0.1] * 256,
        status="active",
        source_ref="session:1:1",
    )
    mock_store.vector_search = AsyncMock(return_value=[mock_item])
    mock_store.keyword_search = AsyncMock(return_value=[])

    with patch("agent.pipeline.reasoner.AsyncOpenAI") as MockClient:
        mock_client = AsyncMock()

        # First response: recall_memory tool call
        response1 = MagicMock()
        response1.choices = [_mock_choice(
            "",
            tool_calls=[_mock_tool_call(
                "call_1", "recall_memory",
                '{"query":"用户的职业是什么","memory_type":"profile","limit":5}'
            )],
            finish_reason="tool_calls"
        )]

        # Guard fetches source_ref before the second model call.
        response2 = MagicMock()
        response2.choices = [_mock_choice("根据原文，你是程序员。")]

        mock_client.chat.completions.create = AsyncMock(
            side_effect=[response1, response2]
        )
        MockClient.return_value = mock_client

        reasoner, registry = _make_memory_reasoner(
            store=mock_store,
            embedder=mock_embedder,
            session_store=mock_session_store,
        )
        ctx = _make_ctx(
            messages=[{"role": "user", "content": "我是什么职业"}],
            tools=registry.get_schemas(),
        )

        result = await reasoner.run_turn(ctx)

        assert result.content == "根据原文，你是程序员。"
        assert [c["function"]["name"] for c in result.tool_calls] == [
            "recall_memory",
            "fetch_messages",
        ]
        assert result.tool_calls[0]["function"]["name"] == "recall_memory"
        assert result.tool_calls[1]["guard"] == "source_ref_requires_fetch"
        mock_store.vector_search.assert_called_once()
        assert mock_store.vector_search.call_args.kwargs["user_id"] == 1
        mock_session_store.fetch_messages.assert_called_once()
        print("test_recall_memory_tool: PASS")


async def test_memory_tool_requires_registry():
    """Memory tools are not hard-coded in Reasoner after P5b."""
    with patch("agent.pipeline.reasoner.AsyncOpenAI"):
        reasoner = Reasoner()

    raw = await reasoner._execute_tool(
        "recall_memory",
        {"query": "用户是谁"},
        _make_ctx(),
    )
    payload = _runtime_payload(raw)

    assert payload["ok"] is False
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "tool_lookup"
    assert payload["error"]["message"] == "Unknown tool: recall_memory"
    print("test_memory_tool_requires_registry: PASS")


async def test_tool_argument_parse_failure_returns_envelope():
    """Bad tool-call JSON is returned to the model as a runtime envelope."""
    with patch("agent.pipeline.reasoner.AsyncOpenAI") as mock_openai:
        mock_client = AsyncMock()
        mock_openai.return_value = mock_client
        tool_response = MagicMock()
        tool_response.choices = [
            _mock_choice(
                "",
                tool_calls=[_mock_tool_call("call_1", "echo", "{bad")],
                finish_reason="tool_calls",
            )
        ]
        final_response = MagicMock()
        final_response.choices = [_mock_choice("参数有误，我已停止工具调用。")]
        mock_client.chat.completions.create = AsyncMock(
            side_effect=[tool_response, final_response]
        )

        registry = ToolRegistry()
        registry.register(
            Tool(
                name="echo",
                description="echo",
                parameters={"type": "object", "properties": {}},
                handler=lambda args, ctx: "ok",
            )
        )
        reasoner = Reasoner(tool_registry=registry)
        result = await reasoner.run_turn(
            _make_ctx(tools=registry.get_schemas())
        )

        assert result.content == "参数有误，我已停止工具调用。"
        assert result.tool_calls[0]["status"] == "error"
        assert result.tool_calls[0]["error_code"] == "argument_parse"
        payload = _runtime_payload(result.tool_calls[0]["result"])
        assert payload["ok"] is False
        assert payload["error"]["code"] == "argument_parse"
        print("test_tool_argument_parse_failure_returns_envelope: PASS")


async def test_recall_then_fetch_messages_tool_chain():
    """Test Akashic-style multi-step memory tools across LLM iterations."""
    mock_store = AsyncMock()
    mock_embedder = AsyncMock()
    mock_embedder.embed = AsyncMock(return_value=[0.1] * 256)
    mock_session_store = MagicMock()
    mock_session_store.load.return_value = [
        {"role": "user", "content": "我是个程序员，最常用 Python"},
        {"role": "assistant", "content": "好的，我记住了。"},
    ]
    mock_session_store.fetch_messages.return_value = (
        [
            {
                "role": "user",
                "content": "我是个程序员，最常用 Python",
                "seq": 0,
                "source_ref": "session:1:1#msg:0",
                "in_source_ref": True,
            }
        ],
        1,
    )

    from uuid import uuid4
    mock_item = MemoryItem(
        id=uuid4(),
        user_id=1,
        memory_type="profile",
        summary="用户是程序员，常用 Python。",
        embedding=[0.1] * 256,
        status="active",
        source_ref="session:1:1",
    )
    mock_store.vector_search = AsyncMock(return_value=[mock_item])
    mock_store.keyword_search = AsyncMock(return_value=[])

    with patch("agent.pipeline.reasoner.AsyncOpenAI") as MockClient:
        mock_client = AsyncMock()

        response1 = MagicMock()
        response1.choices = [_mock_choice(
            "",
            tool_calls=[_mock_tool_call(
                "call_1", "recall_memory",
                '{"query":"用户职业和技术栈","memory_type":"profile","limit":5}'
            )],
            finish_reason="tool_calls",
        )]

        response2 = MagicMock()
        response2.choices = [_mock_choice("你是程序员，主要用 Python。")]

        mock_client.chat.completions.create = AsyncMock(
            side_effect=[response1, response2]
        )
        MockClient.return_value = mock_client

        reasoner, registry = _make_memory_reasoner(
            store=mock_store,
            embedder=mock_embedder,
            session_store=mock_session_store,
        )
        ctx = _make_ctx(
            messages=[{"role": "user", "content": "我是什么职业，用什么语言？"}],
            tools=registry.get_schemas({"recall_memory", "fetch_messages"}),
        )

        result = await reasoner.run_turn(ctx)

        assert result.content == "你是程序员，主要用 Python。"
        assert [c["function"]["name"] for c in result.tool_calls] == [
            "recall_memory",
            "fetch_messages",
        ]
        assert result.tool_calls[1]["guard"] == "source_ref_requires_fetch"
        assert mock_client.chat.completions.create.call_count == 2
        for call in mock_client.chat.completions.create.call_args_list:
            assert call.kwargs["tools"] == ctx.tools
        mock_session_store.fetch_messages.assert_called_once()
        print("test_recall_then_fetch_messages_tool_chain: PASS")


async def test_guard_fetches_after_search_before_final_answer():
    """Guard fetches source_refs from search results before accepting final answer."""
    mock_session_store = MagicMock()
    mock_session_store.search_messages.return_value = (
        [
            {
                "role": "user",
                "content": "最近我戒了咖啡，改喝茶了",
                "seq": 2,
                "source_ref": "session:1:1#msg:2",
            }
        ],
        1,
    )
    mock_session_store.fetch_messages.return_value = (
        [
            {
                "role": "user",
                "content": "最近我戒了咖啡，改喝茶了",
                "seq": 2,
                "source_ref": "session:1:1#msg:2",
                "in_source_ref": True,
            }
        ],
        1,
    )

    with patch("agent.pipeline.reasoner.AsyncOpenAI") as MockClient:
        mock_client = AsyncMock()
        response1 = MagicMock()
        response1.choices = [_mock_choice(
            "",
            tool_calls=[_mock_tool_call(
                "call_1",
                "search_messages",
                '{"query":"戒了咖啡 改喝茶"}',
            )],
            finish_reason="tool_calls",
        )]
        response2 = MagicMock()
        response2.choices = [_mock_choice("原文显示你已经戒了咖啡，现在改喝茶了。")]
        mock_client.chat.completions.create = AsyncMock(
            side_effect=[response1, response2]
        )
        MockClient.return_value = mock_client

        reasoner, registry = _make_memory_reasoner(session_store=mock_session_store)
        ctx = _make_ctx(
            messages=[{"role": "user", "content": "我现在还喝咖啡吗？"}],
            tools=registry.get_schemas(),
        )

        result = await reasoner.run_turn(ctx)

        assert result.content == "原文显示你已经戒了咖啡，现在改喝茶了。"
        assert [c["function"]["name"] for c in result.tool_calls] == [
            "search_messages",
            "fetch_messages",
        ]
        assert result.tool_calls[1]["guard"] == "source_ref_requires_fetch"
        fetch_args = json.loads(result.tool_calls[1]["function"]["arguments"])
        assert fetch_args["source_refs"] == ["session:1:1#msg:2"]
        assert mock_client.chat.completions.create.call_count == 2
        mock_session_store.fetch_messages.assert_called_once()
        print("test_guard_fetches_after_search_before_final_answer: PASS")


async def test_guard_searches_after_recall_for_update_question():
    """Update/history questions should cross-check recall with raw message search."""
    mock_store = AsyncMock()
    mock_embedder = AsyncMock()
    mock_embedder.embed = AsyncMock(return_value=[0.1] * 256)
    mock_session_store = MagicMock()
    mock_session_store.search_messages.return_value = (
        [
            {
                "role": "user",
                "content": "最近我戒了咖啡，改喝茶了",
                "seq": 2,
                "source_ref": "session:1:1#msg:2",
            }
        ],
        1,
    )
    mock_session_store.fetch_messages.return_value = (
        [
            {
                "role": "user",
                "content": "最近我戒了咖啡，改喝茶了",
                "seq": 2,
                "source_ref": "session:1:1#msg:2",
                "in_source_ref": True,
            }
        ],
        1,
    )

    from uuid import uuid4
    mock_item = MemoryItem(
        id=uuid4(),
        user_id=1,
        memory_type="preference",
        summary="用户已经戒掉咖啡，改喝茶了。",
        embedding=[0.1] * 256,
        status="active",
        source_ref="session:1:1#msg:2",
    )
    mock_store.vector_search = AsyncMock(return_value=[mock_item])
    mock_store.keyword_search = AsyncMock(return_value=[])

    with patch("agent.pipeline.reasoner.AsyncOpenAI") as MockClient:
        mock_client = AsyncMock()
        response1 = MagicMock()
        response1.choices = [_mock_choice(
            "",
            tool_calls=[_mock_tool_call(
                "call_1",
                "recall_memory",
                '{"query":"用户的饮品偏好以及后来的更新","memory_type":"preference"}',
            )],
            finish_reason="tool_calls",
        )]
        response2 = MagicMock()
        response2.choices = [_mock_choice("你现在喝茶，不再喝咖啡。")]
        mock_client.chat.completions.create = AsyncMock(
            side_effect=[response1, response2]
        )
        MockClient.return_value = mock_client

        reasoner, registry = _make_memory_reasoner(
            store=mock_store,
            embedder=mock_embedder,
            session_store=mock_session_store,
        )
        ctx = _make_ctx(
            messages=[{"role": "user", "content": "我现在还喝咖啡吗？"}],
            tools=registry.get_schemas(),
        )

        result = await reasoner.run_turn(ctx)

        assert result.content == "你现在喝茶，不再喝咖啡。"
        assert [c["function"]["name"] for c in result.tool_calls] == [
            "recall_memory",
            "search_messages",
            "fetch_messages",
        ]
        assert result.tool_calls[1]["guard"] == "recall_requires_raw_search"
        assert result.tool_calls[2]["guard"] == "source_ref_requires_fetch"
        search_args = json.loads(result.tool_calls[1]["function"]["arguments"])
        assert "咖啡" in search_args["query"]
        assert "茶" in search_args["query"]
        mock_session_store.search_messages.assert_called_once()
        mock_session_store.fetch_messages.assert_called_once()
        print("test_guard_searches_after_recall_for_update_question: PASS")


async def test_guard_forces_recall_when_passive_memory_would_answer():
    """Injected passive memory should not replace an explicit recall step."""
    mock_store = AsyncMock()
    mock_embedder = AsyncMock()
    mock_embedder.embed = AsyncMock(return_value=[0.1] * 256)
    mock_session_store = MagicMock()
    mock_session_store.fetch_messages.return_value = (
        [
            {
                "role": "user",
                "content": "我喜欢喝咖啡，尤其是拿铁",
                "seq": 0,
                "source_ref": "session:1:1#msg:0",
                "in_source_ref": True,
            }
        ],
        1,
    )

    from uuid import uuid4
    mock_item = MemoryItem(
        id=uuid4(),
        user_id=1,
        memory_type="preference",
        summary="用户喜欢喝咖啡，尤其是拿铁。",
        embedding=[0.1] * 256,
        status="active",
        source_ref="session:1:1#msg:0",
    )
    mock_store.vector_search = AsyncMock(return_value=[mock_item])
    mock_store.keyword_search = AsyncMock(return_value=[])

    with patch("agent.pipeline.reasoner.AsyncOpenAI") as MockClient:
        mock_client = AsyncMock()
        response1 = MagicMock()
        response1.choices = [_mock_choice("推荐拿铁。")]
        response2 = MagicMock()
        response2.choices = [_mock_choice("来一杯拿铁吧！")]
        mock_client.chat.completions.create = AsyncMock(
            side_effect=[response1, response2]
        )
        MockClient.return_value = mock_client

        reasoner, registry = _make_memory_reasoner(
            store=mock_store,
            embedder=mock_embedder,
            session_store=mock_session_store,
        )
        ctx = _make_ctx(
            messages=[{"role": "user", "content": "我渴了，有什么推荐的吗？"}],
            memories=[mock_item],
            tools=registry.get_schemas(),
        )

        result = await reasoner.run_turn(ctx)

        assert result.content.startswith("根据你的喜好，")
        assert "拿铁" in result.content
        assert [c["function"]["name"] for c in result.tool_calls] == [
            "recall_memory",
            "fetch_messages",
        ]
        assert result.tool_calls[0]["guard"] == "passive_memory_requires_explicit_recall"
        assert result.tool_calls[1]["guard"] == "source_ref_requires_fetch"
        recall_args = json.loads(result.tool_calls[0]["function"]["arguments"])
        assert recall_args["memory_type"] == "preference"
        assert "我渴了" in recall_args["query"]
        mock_store.vector_search.assert_called_once()
        mock_session_store.search_messages.assert_not_called()
        mock_session_store.fetch_messages.assert_called_once()
        print("test_guard_forces_recall_when_passive_memory_would_answer: PASS")


async def test_final_answer_guard_uses_fetched_project_frameworks():
    """Recommendation answers should stay close to fetched project evidence."""
    mock_store = AsyncMock()
    mock_embedder = AsyncMock()
    mock_embedder.embed = AsyncMock(return_value=[0.1] * 256)
    mock_session_store = MagicMock()
    mock_session_store.search_messages.return_value = (
        [
            {
                "role": "user",
                "content": "我是个程序员，最常用的编程语言是 Python",
                "seq": 0,
                "source_ref": "session:1:1#msg:0",
            }
        ],
        1,
    )
    mock_session_store.fetch_messages.return_value = (
        [
            {
                "role": "user",
                "content": "我是个程序员，最常用的编程语言是 Python",
                "seq": 0,
                "source_ref": "session:1:1#msg:0",
                "in_source_ref": True,
            },
            {
                "role": "assistant",
                "content": "既然你用 Python，可以考虑用 Django 或者 FastAPI 做后端框架。",
                "seq": 1,
                "source_ref": "session:1:1#msg:1",
                "in_source_ref": False,
            },
        ],
        2,
    )

    from uuid import uuid4
    mock_item = MemoryItem(
        id=uuid4(),
        user_id=1,
        memory_type="profile",
        summary="用户是一名程序员，最常用的编程语言是 Python",
        embedding=[0.1] * 256,
        status="active",
        source_ref="session:1:1#msg:0-1",
    )
    mock_store.vector_search = AsyncMock(return_value=[mock_item])
    mock_store.keyword_search = AsyncMock(return_value=[])

    with patch("agent.pipeline.reasoner.AsyncOpenAI") as MockClient:
        mock_client = AsyncMock()
        response1 = MagicMock()
        response1.choices = [_mock_choice(
            "",
            tool_calls=[_mock_tool_call(
                "call_1",
                "recall_memory",
                '{"query":"用户的职业和常用编程语言技术栈","memory_type":"profile"}',
            )],
            finish_reason="tool_calls",
        )]
        response2 = MagicMock()
        response2.choices = [_mock_choice("可以做一个自动化数据分析看板。")]
        mock_client.chat.completions.create = AsyncMock(
            side_effect=[response1, response2]
        )
        MockClient.return_value = mock_client

        reasoner, registry = _make_memory_reasoner(
            store=mock_store,
            embedder=mock_embedder,
            session_store=mock_session_store,
        )
        ctx = _make_ctx(
            messages=[{"role": "user", "content": "给我一个项目建议，用我擅长的技术栈。"}],
            tools=registry.get_schemas(),
        )

        result = await reasoner.run_turn(ctx)

        assert result.content == "根据你擅长的技术栈，你可以用 Python 的 Django 或 FastAPI 框架来做后端。"
        assert [c["function"]["name"] for c in result.tool_calls] == [
            "recall_memory",
            "search_messages",
            "fetch_messages",
        ]
        assert result.tool_calls[1]["guard"] == "recall_requires_raw_search"
        assert result.tool_calls[2]["guard"] == "source_ref_requires_fetch"
        print("test_final_answer_guard_uses_fetched_project_frameworks: PASS")


async def test_p8_long_term_prompt_still_forces_explicit_recall():
    """Injected MEMORY.md should not replace explicit recall/fetch evidence."""
    mock_store = AsyncMock()
    mock_embedder = AsyncMock()
    mock_embedder.embed = AsyncMock(return_value=[0.1] * 256)
    mock_session_store = MagicMock()
    mock_session_store.fetch_messages.return_value = (
        [
            {
                "role": "user",
                "content": "我喜欢喝咖啡，尤其是拿铁",
                "seq": 0,
                "source_ref": "session:1:1#msg:0",
                "in_source_ref": True,
            }
        ],
        1,
    )

    from uuid import uuid4
    mock_item = MemoryItem(
        id=uuid4(),
        user_id=1,
        memory_type="preference",
        summary="用户喜欢喝咖啡，尤其是拿铁。",
        embedding=[0.1] * 256,
        status="active",
        source_ref="session:1:1#msg:0",
    )
    mock_store.vector_search = AsyncMock(return_value=[mock_item])
    mock_store.keyword_search = AsyncMock(return_value=[])

    with patch("agent.pipeline.reasoner.AsyncOpenAI") as MockClient:
        mock_client = AsyncMock()
        response1 = MagicMock()
        response1.choices = [_mock_choice("你喜欢拿铁。")]
        response2 = MagicMock()
        response2.choices = [_mock_choice("你喜欢喝咖啡，尤其是拿铁。")]
        mock_client.chat.completions.create = AsyncMock(
            side_effect=[response1, response2]
        )
        MockClient.return_value = mock_client

        reasoner, registry = _make_memory_reasoner(
            store=mock_store,
            embedder=mock_embedder,
            session_store=mock_session_store,
        )
        ctx = _make_ctx(
            messages=[{"role": "user", "content": "我喜欢喝什么？"}],
            memories=[],
            tools=registry.get_schemas(),
        )
        ctx.prompt_sections = [
            PromptSectionRender(
                name="long_term_memory",
                content="## Long-term Memory\n\n- 用户喜欢拿铁。",
                is_static=False,
            )
        ]

        result = await reasoner.run_turn(ctx)

        assert result.content == "你喜欢喝咖啡，尤其是拿铁。"
        assert [c["function"]["name"] for c in result.tool_calls] == [
            "recall_memory",
            "fetch_messages",
        ]
        assert result.tool_calls[0]["guard"] == "passive_memory_requires_explicit_recall"
        assert result.tool_calls[1]["guard"] == "source_ref_requires_fetch"
        print("test_p8_long_term_prompt_still_forces_explicit_recall: PASS")


async def test_fetch_messages_accepts_source_refs_array():
    """Test Akashic-style source_refs argument works for fetch_messages."""
    mock_session_store = MagicMock()
    mock_session_store.fetch_messages.side_effect = [
        (
            [
                {
                    "role": "user",
                    "content": "我喜欢拿铁。",
                    "seq": 0,
                    "source_ref": "session:1:1#msg:0",
                    "in_source_ref": True,
                }
            ],
            1,
        ),
        (
            [
                {
                    "role": "user",
                    "content": "后来我改喝茶。",
                    "seq": 2,
                    "source_ref": "session:1:1#msg:2",
                    "in_source_ref": True,
                }
            ],
            1,
        ),
    ]

    _, registry = _make_memory_reasoner(session_store=mock_session_store)

    raw = await registry.execute(
        "fetch_messages",
        {
            "source_refs": ["session:1:1#msg:0", "session:1:1#msg:2"],
            "context": 1,
            "limit": 10,
        },
        _make_ctx(),
    )
    payload = json.loads(raw)

    assert payload["matched_count"] == 2
    assert payload["source_refs"] == ["session:1:1#msg:0", "session:1:1#msg:2"]
    assert [m["content"] for m in payload["messages"]] == [
        "我喜欢拿铁。",
        "后来我改喝茶。",
    ]
    assert mock_session_store.fetch_messages.call_count == 2
    print("test_fetch_messages_accepts_source_refs_array: PASS")


async def test_search_messages_tool():
    """Test search_messages uses current session user and returns source refs."""
    mock_session_store = MagicMock()
    mock_session_store.search_messages.return_value = (
        [
            {
                "role": "user",
                "content": "我是个程序员，最常用 Python",
                "seq": 0,
                "source_ref": "session:1:1#msg:0",
            }
        ],
        1,
    )
    mock_session_store.fetch_messages.return_value = (
        [
            {
                "role": "user",
                "content": "我是个程序员，最常用 Python",
                "seq": 0,
                "source_ref": "session:1:1#msg:0",
                "in_source_ref": True,
            }
        ],
        1,
    )

    with patch("agent.pipeline.reasoner.AsyncOpenAI") as MockClient:
        mock_client = AsyncMock()

        response1 = MagicMock()
        response1.choices = [_mock_choice(
            "",
            tool_calls=[_mock_tool_call(
                "call_1", "search_messages",
                '{"query":"Python 程序员","role":"user","limit":5}'
            )],
            finish_reason="tool_calls",
        )]

        response2 = MagicMock()
        response2.choices = [_mock_choice("原文里你说过自己是程序员，常用 Python。")]

        mock_client.chat.completions.create = AsyncMock(
            side_effect=[response1, response2]
        )
        MockClient.return_value = mock_client

        reasoner, registry = _make_memory_reasoner(session_store=mock_session_store)
        ctx = _make_ctx(
            messages=[{"role": "user", "content": "我用什么语言？"}],
            tools=registry.get_schemas(),
        )

        result = await reasoner.run_turn(ctx)

        assert result.content == "原文里你说过自己是程序员，常用 Python。"
        assert [c["function"]["name"] for c in result.tool_calls] == [
            "search_messages",
            "fetch_messages",
        ]
        assert result.tool_calls[0]["function"]["name"] == "search_messages"
        assert result.tool_calls[1]["guard"] == "source_ref_requires_fetch"
        mock_session_store.search_messages.assert_called_once()
        mock_session_store.fetch_messages.assert_called_once()
        assert mock_session_store.search_messages.call_args.kwargs["user_id"] == 1
        assert mock_session_store.search_messages.call_args.kwargs["role"] == "user"
        print("test_search_messages_tool: PASS")


async def test_recall_memory_grep_mode_with_time_filter():
    """Test recall_memory grep mode lists time-filtered memories."""
    mock_store = MagicMock()
    mock_embedder = AsyncMock()
    mock_session_store = MagicMock()
    mock_session_store.fetch_messages.return_value = (
        [
            {
                "role": "user",
                "content": "今天聊了 Rust meetup。",
                "seq": 0,
                "source_ref": "session:1:1#msg:0",
                "in_source_ref": True,
            }
        ],
        1,
    )
    from uuid import uuid4
    mock_store.list_memories.return_value = [
        MemoryItem(
            id=uuid4(),
            user_id=1,
            memory_type="event",
            summary="用户今天聊了 Rust meetup。",
            embedding=None,
            status="active",
            source_ref="session:1:1#msg:0",
        )
    ]

    with patch("agent.pipeline.reasoner.AsyncOpenAI") as MockClient:
        mock_client = AsyncMock()
        response1 = MagicMock()
        response1.choices = [_mock_choice(
            "",
            tool_calls=[_mock_tool_call(
                "call_1", "recall_memory",
                '{"query":"用户今天做了什么","search_mode":"grep","time_filter":"today","limit":20}'
            )],
            finish_reason="tool_calls",
        )]
        response2 = MagicMock()
        response2.choices = [_mock_choice("原文显示你今天聊了 Rust meetup。")]
        mock_client.chat.completions.create = AsyncMock(
            side_effect=[response1, response2]
        )
        MockClient.return_value = mock_client

        reasoner, registry = _make_memory_reasoner(
            store=mock_store,
            embedder=mock_embedder,
            session_store=mock_session_store,
        )
        ctx = _make_ctx(
            messages=[{"role": "user", "content": "今天聊了什么？"}],
            tools=registry.get_schemas(),
        )

        result = await reasoner.run_turn(ctx)

        assert result.content == "原文显示你今天聊了 Rust meetup。"
        mock_store.list_memories.assert_called_once()
        assert mock_store.list_memories.call_args.kwargs["memory_types"] == ["event"]
        assert mock_store.list_memories.call_args.kwargs["user_id"] == 1
        mock_session_store.fetch_messages.assert_called_once()
        print("test_recall_memory_grep_mode_with_time_filter: PASS")


async def test_recall_memory_infers_profile_type():
    """When memory_type is omitted, profile-shaped queries should not search all types."""
    mock_store = AsyncMock()
    mock_embedder = AsyncMock()
    mock_embedder.embed = AsyncMock(return_value=[0.1] * 256)
    mock_store.vector_search = AsyncMock(return_value=[])
    mock_store.keyword_search = AsyncMock(return_value=[])

    _, registry = _make_memory_reasoner(
        store=mock_store,
        embedder=mock_embedder,
    )

    raw = await registry.execute(
        "recall_memory",
        {"query": "用户的职业和常用编程语言技术栈"},
        _make_ctx(),
    )
    payload = json.loads(raw)

    assert payload["applied_memory_types"] == ["profile"]
    assert mock_store.vector_search.call_args.kwargs["memory_types"] == ["profile"]
    assert mock_store.keyword_search.call_args.kwargs["memory_types"] == ["profile"]
    print("test_recall_memory_infers_profile_type: PASS")


async def test_recall_memory_ignores_zero_user_id():
    """Model-supplied user_id=0 should not escape the current session scope."""
    mock_store = AsyncMock()
    mock_embedder = AsyncMock()
    mock_embedder.embed = AsyncMock(return_value=[0.1] * 256)
    mock_store.vector_search = AsyncMock(return_value=[])
    mock_store.keyword_search = AsyncMock(return_value=[])
    _, registry = _make_memory_reasoner(
        store=mock_store,
        embedder=mock_embedder,
    )

    await registry.execute(
        "recall_memory",
        {"query": "用户喜欢什么咖啡", "user_id": 0},
        _make_ctx(user_id=42, chat_id=7),
    )

    assert mock_store.vector_search.call_args.kwargs["user_id"] == 42
    assert mock_store.keyword_search.call_args.kwargs["user_id"] == 42
    print("test_recall_memory_ignores_zero_user_id: PASS")


async def test_recall_memory_clamps_mismatched_user_id_to_session():
    """Model-supplied user_id must not cross the active session boundary."""
    mock_store = AsyncMock()
    mock_embedder = AsyncMock()
    mock_embedder.embed = AsyncMock(return_value=[0.1] * 256)
    mock_store.vector_search = AsyncMock(return_value=[])
    mock_store.keyword_search = AsyncMock(return_value=[])
    _, registry = _make_memory_reasoner(
        store=mock_store,
        embedder=mock_embedder,
    )

    await registry.execute(
        "recall_memory",
        {"query": "用户的职业", "user_id": 1},
        _make_ctx(user_id=9000, chat_id=9000),
    )

    assert mock_store.vector_search.call_args.kwargs["user_id"] == 9000
    assert mock_store.keyword_search.call_args.kwargs["user_id"] == 9000
    print("test_recall_memory_clamps_mismatched_user_id_to_session: PASS")


async def test_single_tool_call():
    """Test single memorize tool call. 需要 store + embedder mock。"""
    mock_store = AsyncMock()
    mock_embedder = AsyncMock()
    mock_embedder.embed = AsyncMock(return_value=[0.1] * 256)

    from uuid import uuid4
    mock_item = MemoryItem(
        id=uuid4(),
        user_id=1,
        memory_type="preference",
        summary="用户喜欢吃苹果",
        embedding=[0.1] * 256,
        status="active",
        source_ref="session:1:1",
    )
    mock_store.upsert_item = AsyncMock(return_value=mock_item)

    with patch("agent.pipeline.reasoner.AsyncOpenAI") as MockClient:
        mock_client = AsyncMock()

        # First response: memorize tool call
        response1 = MagicMock()
        response1.choices = [_mock_choice(
            "",
            tool_calls=[_mock_tool_call(
                "call_1", "memorize",
                '{"summary":"用户喜欢吃苹果","memory_type":"preference"}'
            )],
            finish_reason="tool_calls"
        )]

        # Second response: final answer
        response2 = MagicMock()
        response2.choices = [_mock_choice("好的，我已经记住了你喜欢苹果。")]

        mock_client.chat.completions.create = AsyncMock(
            side_effect=[response1, response2]
        )
        MockClient.return_value = mock_client

        reasoner, registry = _make_memory_reasoner(
            store=mock_store,
            embedder=mock_embedder,
        )
        ctx = _make_ctx(
            messages=[{"role": "user", "content": "记住我喜欢吃苹果"}],
            tools=registry.get_schemas(),
        )

        result = await reasoner.run_turn(ctx)

        assert result.content == "好的，我已经记住了你喜欢苹果。"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0]["function"]["name"] == "memorize"
        assert result.finish_reason == "stop"

        # Verify upsert_item was actually called
        mock_store.upsert_item.assert_called_once()
        call_kwargs = mock_store.upsert_item.call_args.kwargs
        assert call_kwargs["memory_type"] == "preference"
        assert call_kwargs["summary"] == "用户喜欢吃苹果"
        assert call_kwargs["user_id"] == 1
        assert call_kwargs["source_ref"] == "session:1:1"

        print("test_single_tool_call: PASS")


async def test_multiple_tool_calls():
    """Test multiple tool calls in one response."""
    mock_store = AsyncMock()
    mock_embedder = AsyncMock()
    mock_embedder.embed = AsyncMock(return_value=[0.1] * 256)

    from uuid import uuid4
    mock_store.upsert_item = AsyncMock(side_effect=[
        MemoryItem(
            id=uuid4(), user_id=1, memory_type="preference",
            summary="用户喜欢吃苹果", embedding=[0.1]*256,
            status="active", source_ref="session:1:1",
        ),
        MemoryItem(
            id=uuid4(), user_id=1, memory_type="profile",
            summary="用户名字叫张三", embedding=[0.1]*256,
            status="active", source_ref="session:1:1",
        ),
    ])

    with patch("agent.pipeline.reasoner.AsyncOpenAI") as MockClient:
        mock_client = AsyncMock()

        response1 = MagicMock()
        response1.choices = [_mock_choice(
            "",
            tool_calls=[
                _mock_tool_call(
                    "call_1", "memorize",
                    '{"summary":"用户喜欢吃苹果","memory_type":"preference"}'
                ),
                _mock_tool_call(
                    "call_2", "memorize",
                    '{"summary":"用户名字叫张三","memory_type":"profile"}'
                ),
            ],
            finish_reason="tool_calls"
        )]

        response2 = MagicMock()
        response2.choices = [_mock_choice("我已经记住了你的偏好和名字。")]

        mock_client.chat.completions.create = AsyncMock(
            side_effect=[response1, response2]
        )
        MockClient.return_value = mock_client

        reasoner, registry = _make_memory_reasoner(
            store=mock_store,
            embedder=mock_embedder,
        )
        ctx = _make_ctx(
            messages=[{"role": "user", "content": "记住我的信息"}],
            tools=registry.get_schemas(),
        )

        result = await reasoner.run_turn(ctx)

        assert "记住了" in result.content
        assert len(result.tool_calls) == 2
        assert mock_store.upsert_item.call_count == 2
        print("test_multiple_tool_calls: PASS")


async def test_max_iterations():
    """Test max iterations limit."""
    mock_store = AsyncMock()
    mock_embedder = AsyncMock()
    mock_embedder.embed = AsyncMock(return_value=[0.1] * 256)

    from uuid import uuid4
    mock_store.upsert_item = AsyncMock(return_value=MemoryItem(
        id=uuid4(), user_id=1, memory_type="profile",
        summary="test", embedding=[0.1]*256,
        status="active", source_ref="session:1:1",
    ))

    with patch("agent.pipeline.reasoner.AsyncOpenAI") as MockClient:
        mock_client = AsyncMock()

        # Keep returning tool calls to exceed max iterations
        tool_response = MagicMock()
        tool_response.choices = [_mock_choice(
            "",
            tool_calls=[_mock_tool_call(
                "call_x", "memorize",
                '{"summary":"test","memory_type":"profile"}'
            )],
            finish_reason="tool_calls"
        )]

        mock_client.chat.completions.create = AsyncMock(return_value=tool_response)
        MockClient.return_value = mock_client

        reasoner, registry = _make_memory_reasoner(
            store=mock_store,
            embedder=mock_embedder,
        )
        ctx = _make_ctx(
            messages=[{"role": "user", "content": "test"}],
            tools=registry.get_schemas(),
        )

        result = await reasoner.run_turn(ctx)

        assert result.finish_reason == "max_iterations"
        assert "处理请求时遇到问题" in result.content
        assert mock_client.chat.completions.create.call_count == 4
        print("test_max_iterations: PASS")


async def test_api_retry():
    """Test API error retry."""
    with patch("agent.pipeline.reasoner.AsyncOpenAI") as MockClient:
        mock_client = AsyncMock()

        # First call fails, second succeeds
        response = MagicMock()
        response.choices = [_mock_choice("重试成功")]

        mock_client.chat.completions.create = AsyncMock(
            side_effect=[Exception("API error"), response]
        )
        MockClient.return_value = mock_client

        reasoner = Reasoner()
        ctx = _make_ctx(messages=[{"role": "user", "content": "test"}])

        result = await reasoner.run_turn(ctx)

        assert result.content == "重试成功"
        assert mock_client.chat.completions.create.call_count == 2
        print("test_api_retry: PASS")


async def main():
    await test_simple_response()
    await test_recall_memory_tool()
    await test_memory_tool_requires_registry()
    await test_tool_argument_parse_failure_returns_envelope()
    await test_recall_then_fetch_messages_tool_chain()
    await test_guard_fetches_after_search_before_final_answer()
    await test_guard_searches_after_recall_for_update_question()
    await test_guard_forces_recall_when_passive_memory_would_answer()
    await test_final_answer_guard_uses_fetched_project_frameworks()
    await test_p8_long_term_prompt_still_forces_explicit_recall()
    await test_fetch_messages_accepts_source_refs_array()
    await test_search_messages_tool()
    await test_recall_memory_grep_mode_with_time_filter()
    await test_recall_memory_infers_profile_type()
    await test_recall_memory_ignores_zero_user_id()
    await test_recall_memory_clamps_mismatched_user_id_to_session()
    await test_single_tool_call()
    await test_multiple_tool_calls()
    await test_max_iterations()
    await test_api_retry()
    print("\nAll reasoner tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
