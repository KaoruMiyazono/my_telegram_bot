"""
端到端测试：发 16 条消息到 pipeline → 验证 consolidation 触发并写入 DB

用法：python tests/test_consolidation_e2e.py

不 mock 任何东西，用真实 Embedder、真实 MemoryStore、真实 DeepSeek API。
就像你和 bot 在 Telegram 里聊了 16 轮一样。
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("test")

from agent.core.types import BeforeReasoningCtx, InboundMessage, Session
from agent.pipeline.passive_turn import PassiveTurnPipeline
from agent.pipeline.phases.after_reasoning import AfterReasoningPhase
from agent.pipeline.phases.after_turn import AfterTurnPhase
from agent.pipeline.phases.before_reasoning import BeforeReasoningPhase
from agent.pipeline.phases.before_turn import BeforeTurnPhase, _sessions
from agent.pipeline.reasoner import Reasoner
from agent.pipeline.consolidation_worker import ConsolidationWorker
from agent.tool_hooks import ToolExecutor
from agent.tools import ToolRegistry
from agent.tools.memory import register_memory_tools
from config.settings import settings
from memory.bootstrap import build_memory_runtime
from memory.embedder import Embedder
from memory.store import MemoryStore
from persistence.database import get_connection, init_db
from persistence.session_store import get_session_store
from agent.core.event_bus import EventBus


async def main():
    TEST_USER_ID = 9999
    TEST_CHAT_ID = 9999
    session_key = (TEST_USER_ID, TEST_CHAT_ID)

    # 清空旧数据
    conn = get_connection()
    conn.execute("DELETE FROM vec_items WHERE embedding_id IN (SELECT id FROM memory_items WHERE user_id = ?)", (TEST_USER_ID,))
    conn.execute("DELETE FROM memory_items WHERE user_id = ?", (TEST_USER_ID,))
    conn.execute("DELETE FROM conversation_sessions WHERE user_id = ? AND chat_id = ?", session_key)
    conn.commit()
    _sessions.pop(session_key, None)

    # 统计初始状态
    before_count = conn.execute(
        "SELECT COUNT(*) FROM memory_items WHERE user_id = ? AND status='active'",
        (TEST_USER_ID,),
    ).fetchone()[0]
    print(f"初始记忆数: {before_count}")

    # 创建真实组件
    embedder = Embedder()
    store = MemoryStore(embedder)
    session_store = get_session_store()
    memory_runtime = build_memory_runtime(
        embedder=embedder,
        memory_store=store,
        session_store=session_store,
    )
    event_bus = EventBus.get_instance()
    tool_registry = ToolRegistry()
    tool_executor = ToolExecutor()
    register_memory_tools(tool_registry, memory_runtime.engine)

    before_turn = BeforeTurnPhase(memory_engine=memory_runtime.engine)
    before_reasoning = BeforeReasoningPhase(
        tool_registry=tool_registry,
        self_model_reader=memory_runtime.markdown.store.read_self,
        long_term_memory_reader=memory_runtime.markdown.store.read_long_term,
        recent_context_reader=memory_runtime.markdown.store.read_recent_context,
    )
    reasoner = Reasoner(
        tool_registry=tool_registry,
        tool_executor=tool_executor,
        event_bus=event_bus,
    )
    after_reasoning = AfterReasoningPhase(store)
    after_turn = AfterTurnPhase(event_bus, None)

    consolidation = ConsolidationWorker(
        keep_count=10,
        min_new_messages=6,
        markdown_store=memory_runtime.markdown.store,
    )

    pipeline = PassiveTurnPipeline(
        before_turn=before_turn,
        before_reasoning=before_reasoning,
        reasoner=reasoner,
        after_reasoning=after_reasoning,
        after_turn=after_turn,
        store=store,
        consolidation_worker=consolidation,
        memory_runtime=memory_runtime,
    )

    # 模拟 18 轮对话（每轮 user + assistant = 2条，总共 36条）
    # 触发条件：total=36 > keep=10, new=36 > min_new=6 → consolidation 触发
    messages = [
        "你好，我是程序员，我叫张三。",
        "我平时喜欢用 Vim 写代码。",
        "我最喜欢的编程语言是 Python。",
        "我住在北京朝阳区。",
        "我喜欢喝手冲咖啡，尤其是埃塞俄比亚的豆子。",
        "我养了一只猫，叫咪咪，是橘猫。",
        "我每天早上7点起床跑步。",
        "我不喜欢太甜的食物。",
        "我最近在学 Rust 语言。",
        "我周末喜欢去爬山。",
        "我的工作是后端开发，主要做微服务。",
        "我有一台 MacBook Pro M3。",
        "我喜欢听爵士乐，尤其是 Miles Davis。",
        "我不喝奶茶，觉得太甜了。",
        "我最近在看《深入理解计算机系统》。",
        "我的梦想是写一个自己的操作系统。",
        "我平时用 VS Code，但终端用 iTerm2。",
        "我喜欢简洁的设计风格。",
    ]

    print(f"\n发送 {len(messages)} 条消息到 pipeline...\n")

    for i, content in enumerate(messages):
        msg = InboundMessage(
            user_id=TEST_USER_ID,
            chat_id=TEST_CHAT_ID,
            content=content,
        )
        result = await pipeline.execute(msg)
        print(f"  [{i+1:02d}] 用户: {content[:40]}...")
        print(f"       bot:  {result.content[:60]}...")
        await asyncio.sleep(0.3)  # 避免 API 限速

    # 等异步 consolidation 完成
    print("\n等待 consolidation 完成...")
    session = _sessions.get(session_key)
    for _ in range(30):
        if session and session.last_consolidated > 0:
            break
        await asyncio.sleep(0.5)
        session = _sessions.get(session_key)

    # 验证结果
    after_count = conn.execute(
        "SELECT COUNT(*) FROM memory_items WHERE user_id = ? AND status='active'",
        (TEST_USER_ID,),
    ).fetchone()[0]

    # 列出所有新增的记忆
    rows = conn.execute(
        "SELECT memory_type, summary FROM memory_items WHERE user_id = ? AND status='active' ORDER BY created_at",
        (TEST_USER_ID,),
    ).fetchall()

    print(f"\n{'='*60}")
    print(f"结果")
    print(f"{'='*60}")
    print(f"Session 消息数: {len(session.messages) if session else 'N/A'}")
    print(f"last_consolidated: {session.last_consolidated if session else 'N/A'}")
    print(f"记忆总数（前: {before_count} → 后: {after_count}）")

    if rows:
        print(f"\n写入的记忆 ({len(rows)} 条):")
        for mem_type, summary in rows:
            print(f"  [{mem_type}] {summary}")
    else:
        print("\n⚠️  没有写入记忆。")
        print("可能原因：")
        print("  1. consolidation LLM 提取失败（检查 API key / 代理）")
        print("  2. consolidate 的 LLM 调用路径走了真实 API 但返回格式不对")
        print("  3. 窗口条件未满足（检查 last_consolidated）")

    # 额外验证：手动调一次 recall 看能不能搜到
    print(f"\n手动检索测试...")
    import json
    recall_ctx = BeforeReasoningCtx(
        session=Session(user_id=TEST_USER_ID, chat_id=TEST_CHAT_ID, messages=[]),
        memories=[],
        messages=[],
        tools=tool_registry.get_schemas(),
    )
    recall_result = await tool_registry.execute(
        "recall_memory",
        {
            "query": "用户的职业是什么",
            "user_id": TEST_USER_ID,
            "limit": 5,
        },
        recall_ctx,
    )
    print(f"recall_memory('用户的职业是什么'):")
    import json
    parsed = json.loads(recall_result)
    if parsed.get("items"):
        for item in parsed["items"]:
            print(f"  [{item['memory_type']}] {item['summary']} (score={item['score']})")
    else:
        print("  (无结果)")


if __name__ == "__main__":
    asyncio.run(main())
