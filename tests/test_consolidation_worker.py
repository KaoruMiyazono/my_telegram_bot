"""Test consolidation worker: should_consolidate, window selection."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.core.types import Session
from agent.pipeline.consolidation_worker import ConsolidationWorker, _build_window_source_ref


def test_should_consolidate_empty():
    """Empty session: no consolidation."""
    w = ConsolidationWorker(keep_count=10, min_new_messages=6)
    session = Session(user_id=1, chat_id=1, messages=[], last_consolidated=0)
    assert not w.should_consolidate(session)
    print("test_should_consolidate_empty: PASS")


def test_should_consolidate_too_few():
    """Not enough messages: no consolidation."""
    w = ConsolidationWorker(keep_count=10, min_new_messages=6)
    session = Session(
        user_id=1, chat_id=1,
        messages=[{"role": "user", "content": f"msg {i}"} for i in range(8)],
        last_consolidated=0,
    )
    assert not w.should_consolidate(session)
    print("test_should_consolidate_too_few: PASS")


def test_should_consolidate_enough():
    """Enough messages (>keep_count + min_new): consolidation triggers."""
    w = ConsolidationWorker(keep_count=10, min_new_messages=6)
    session = Session(
        user_id=1, chat_id=1,
        messages=[{"role": "user", "content": f"msg {i}"} for i in range(20)],
        last_consolidated=0,
    )
    assert w.should_consolidate(session)
    print("test_should_consolidate_enough: PASS")


def test_should_consolidate_already_done():
    """All messages already consolidated: no consolidation."""
    w = ConsolidationWorker(keep_count=10, min_new_messages=6)
    session = Session(
        user_id=1, chat_id=1,
        messages=[{"role": "user", "content": f"msg {i}"} for i in range(20)],
        last_consolidated=10,  # already consolidated up to 10 (keep=10)
    )
    assert not w.should_consolidate(session)
    print("test_should_consolidate_already_done: PASS")


def test_consolidation_window():
    """Window returns correct un-consolidated messages."""
    w = ConsolidationWorker(keep_count=10, min_new_messages=6)
    session = Session(
        user_id=1, chat_id=1,
        messages=[{"role": "user", "content": f"msg {i}"} for i in range(30)],
        last_consolidated=0,
    )
    window = w.get_consolidation_window(session)
    # total=30, keep=10, so consolidate up to 20
    assert len(window) == 20
    assert window[0]["content"] == "msg 0"
    assert window[-1]["content"] == "msg 19"
    print("test_consolidation_window: PASS")


def test_consolidation_window_partial():
    """Partial consolidation: window starts from last_consolidated."""
    w = ConsolidationWorker(keep_count=10, min_new_messages=6)
    session = Session(
        user_id=1, chat_id=1,
        messages=[{"role": "user", "content": f"msg {i}"} for i in range(30)],
        last_consolidated=10,
    )
    window = w.get_consolidation_window(session)
    # total=30, keep=10, last_consolidated=10 → window = msg[10:20]
    assert len(window) == 10
    assert window[0]["content"] == "msg 10"
    assert window[-1]["content"] == "msg 19"
    print("test_consolidation_window_partial: PASS")


def test_build_window_source_ref():
    source_ref = _build_window_source_ref(user_id=1, chat_id=2, start=3, end=7)
    assert source_ref == "session:1:2#msg:3-7"
    print("test_build_window_source_ref: PASS")


def test_consolidate_advances_to_original_window_when_session_grows():
    """Async consolidation must not skip messages appended while LLM is running."""
    w = ConsolidationWorker(keep_count=10, min_new_messages=6)
    session = Session(
        user_id=1,
        chat_id=1,
        messages=[{"role": "user", "content": f"msg {i}"} for i in range(20)],
        last_consolidated=0,
    )

    async def run():
        async def fake_extract(_conversation: str):
            session.messages.extend(
                {"role": "user", "content": f"late {i}"} for i in range(10)
            )
            return []

        w._llm_extract = fake_extract
        await w.consolidate(session, store=None, user_id=1, chat_id=1)

    asyncio.run(run())

    assert len(session.messages) == 30
    assert session.last_consolidated == 10
    print("test_consolidate_advances_to_original_window_when_session_grows: PASS")


if __name__ == "__main__":
    test_should_consolidate_empty()
    test_should_consolidate_too_few()
    test_should_consolidate_enough()
    test_should_consolidate_already_done()
    test_consolidation_window()
    test_consolidation_window_partial()
    test_build_window_source_ref()
    test_consolidate_advances_to_original_window_when_session_grows()
    print("\nAll consolidation tests passed!")
