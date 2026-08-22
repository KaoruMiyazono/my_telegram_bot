import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["TG_BOT_TOKEN"] = "test_token"
os.environ["DEEPSEEK_API_KEY"] = "test_deepseek_key"
os.environ["ALIYUN_DASHSCOPE_API_KEY"] = "test_aliyun_key"
os.environ["DATABASE_PATH"] = tempfile.mktemp(suffix=".db")
os.environ["DEEPSEEK_BASE_URL"] = "https://api.test.com"
os.environ["LLM_MODEL"] = "test-model"

from eval.replay_runner import (
    _EVAL_CHAT_ID,
    _EVAL_QA_CHAT_ID,
    _EVAL_SOURCE_REF,
    _build_parser,
    _clear_eval_state,
    _create_pipeline,
    _load_replay_conversations,
    _tool_policy_for_case,
    _validate_eval_args,
)
from evaluation.dataset_builder import EvalCase, QuestionType, DistanceType
from agent.core.types import BeforeTurnCtx, InboundMessage, Session
from agent.pipeline.reasoner import Reasoner
from memory.bootstrap import default_markdown_memory_root
from memory.embedder import Embedder
from memory.store import MemoryStore
from persistence.database import init_db


async def test_replay_pipeline_uses_real_reasoner() -> None:
    """Replay eval should wire the real agent loop, not a mock reasoner."""
    init_db()
    _clear_eval_state()
    embedder = Embedder()
    store = MemoryStore(embedder)
    pipeline = await _create_pipeline(store=store, embedder=embedder)

    assert isinstance(pipeline.reasoner, Reasoner)
    assert pipeline.before_reasoning.benchmark_mode is True
    assert getattr(pipeline, "plugin_manager", None) is not None
    await pipeline.plugin_manager.terminate_all()
    print("test_replay_pipeline_uses_real_reasoner: PASS")


async def test_replay_pipeline_loads_passive_plugin_modules() -> None:
    """Replay eval should use the same passive phase/plugin surface as production."""
    init_db()
    _clear_eval_state()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        plugin_dir = root / "plugins" / "01_eval_probe"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.py").write_text(
            '''
from agent.plugins import Plugin


class HintModule:
    slot = "eval_probe.before_reasoning_hint"
    requires = ("before_reasoning.emit",)

    async def run(self, frame):
        frame.slots["reasoning:extra_hint:eval_probe"] = "eval-plugin-hint"
        return frame


class PromptModule:
    slot = "eval_probe.prompt_section"
    requires = ("prompt_render.emit",)

    async def run(self, frame):
        frame.slots["prompt:section_bottom:eval_probe"] = "## Eval Plugin\\neval-plugin-section"
        return frame


class EvalProbe(Plugin):
    name = "eval_probe"

    def before_reasoning_modules(self):
        return [HintModule()]

    def prompt_render_modules(self):
        return [PromptModule()]
''',
            encoding="utf-8",
        )
        old_cwd = Path.cwd()
        try:
            os.chdir(root)
            embedder = Embedder()
            store = MemoryStore(embedder)
            pipeline = await _create_pipeline(store=store, embedder=embedder)
            assert pipeline.plugin_manager.loaded_count == 1
            ctx = await pipeline.before_reasoning.build_ctx(
                BeforeTurnCtx(
                    inbound_message=InboundMessage(
                        user_id=1,
                        chat_id=2,
                        content="hello",
                    ),
                    session=Session(user_id=1, chat_id=2),
                    retrieved_memories=[],
                    content="hello",
                    session_key="1:2",
                    chat_id="2",
                )
            )
            system_prompt = ctx.messages[0]["content"]
            assert "eval-plugin-hint" in system_prompt
            assert "eval-plugin-section" in system_prompt
            await pipeline.plugin_manager.terminate_all()
        finally:
            os.chdir(old_cwd)
    print("test_replay_pipeline_loads_passive_plugin_modules: PASS")


def test_replay_parser_has_no_mock_mode() -> None:
    parser = _build_parser()

    args = parser.parse_args(["--set", "green", "--fresh", "--limit", "1"])
    assert args.set == "green"
    assert args.fresh is True
    assert args.same_session_qa is False
    assert not hasattr(args, "mock")
    print("test_replay_parser_has_no_mock_mode: PASS")


def test_replay_eval_requires_fresh() -> None:
    parser = _build_parser()
    args = parser.parse_args(["--set", "green", "--limit", "1"])

    try:
        _validate_eval_args(args)
    except SystemExit as exc:
        assert "--fresh" in str(exc)
    else:
        raise AssertionError("replay eval should require --fresh")
    print("test_replay_eval_requires_fresh: PASS")


def test_replay_eval_requires_invalidation() -> None:
    parser = _build_parser()
    args = parser.parse_args(["--set", "green", "--fresh", "--no-invalidation"])

    try:
        _validate_eval_args(args)
    except SystemExit as exc:
        assert "invalidation" in str(exc)
    else:
        raise AssertionError("replay eval should not allow --no-invalidation")
    print("test_replay_eval_requires_invalidation: PASS")


def test_replay_invalidation_source_ref_is_session_scoped() -> None:
    assert _EVAL_SOURCE_REF == "session:9000:9000"
    print("test_replay_invalidation_source_ref_is_session_scoped: PASS")


def test_replay_eval_isolates_qa_session_by_default() -> None:
    parser = _build_parser()
    isolated_args = parser.parse_args(["--set", "green", "--fresh"])
    same_session_args = parser.parse_args(["--set", "green", "--fresh", "--same-session-qa"])

    assert _EVAL_CHAT_ID == 9000
    assert _EVAL_QA_CHAT_ID == 9001
    assert isolated_args.same_session_qa is False
    assert same_session_args.same_session_qa is True
    print("test_replay_eval_isolates_qa_session_by_default: PASS")


def test_replay_conversations_loader_keeps_curated_dataset() -> None:
    conversations = _load_replay_conversations()

    assert conversations
    assert "messages" in conversations[0]
    print("test_replay_conversations_loader_keeps_curated_dataset: PASS")


def test_replay_fresh_clears_eval_markdown_context() -> None:
    init_db()
    user_root = default_markdown_memory_root() / "users" / "9000"
    memory_dir = user_root / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "RECENT_CONTEXT.md").write_text(
        "# Recent Context\n\n## Compression\n- stale\n",
        encoding="utf-8",
    )

    _clear_eval_state()

    assert not user_root.exists()
    print("test_replay_fresh_clears_eval_markdown_context: PASS")


def test_tool_policy_requires_recall_search_fetch_for_update() -> None:
    case = EvalCase(
        case_id="test-policy",
        question="我现在喝茶还是咖啡？",
        gold_answer="你现在喝茶",
        question_type=QuestionType.KNOWLEDGE_UPDATE,
        context_sessions=[],
        distance_type=DistanceType.SEMANTIC_SIMILARITY,
    )

    policy = _tool_policy_for_case(
        case,
        [
            {"function": {"name": "recall_memory"}},
            {"function": {"name": "search_messages"}},
        ],
    )

    assert policy["required"] == ["recall_memory", "fetch_messages", "search_messages"]
    assert policy["missing"] == ["fetch_messages"]
    assert policy["satisfied"] is False
    print("test_tool_policy_requires_recall_search_fetch_for_update: PASS")


async def main() -> None:
    test_replay_parser_has_no_mock_mode()
    test_replay_eval_requires_fresh()
    test_replay_eval_requires_invalidation()
    test_replay_invalidation_source_ref_is_session_scoped()
    test_replay_eval_isolates_qa_session_by_default()
    test_replay_conversations_loader_keeps_curated_dataset()
    test_replay_fresh_clears_eval_markdown_context()
    test_tool_policy_requires_recall_search_fetch_for_update()
    await test_replay_pipeline_uses_real_reasoner()
    await test_replay_pipeline_loads_passive_plugin_modules()
    print("\nAll replay runner tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
