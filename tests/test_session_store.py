import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["DATABASE_PATH"] = tempfile.mktemp(suffix=".db")

import persistence.database as database
from config.settings import settings
from persistence.database import init_db
from persistence.session_store import SessionStore


def _reset_thread_connection() -> None:
    if database._local is None:
        return
    conn = getattr(database._local, "conn", None)
    if conn is not None:
        conn.close()
    database._local = None


def test_search_messages_returns_source_refs() -> None:
    init_db()
    store = SessionStore()
    store.save(
        42,
        100,
        [
            {"role": "user", "content": "我是个程序员，最常用 Python"},
            {"role": "assistant", "content": "好的，我记住了。"},
            {"role": "user", "content": "我最近在学 Rust"},
        ],
    )

    messages, total = store.search_messages(
        "Python 程序员",
        user_id=42,
        role="user",
        limit=10,
    )

    assert total == 1
    assert len(messages) == 1
    assert messages[0]["seq"] == 0
    assert messages[0]["source_ref"] == "session:42:100#msg:0"
    print("test_search_messages_returns_source_refs: PASS")


def test_fetch_messages_by_message_ref_with_context() -> None:
    init_db()
    store = SessionStore()
    store.save(
        42,
        101,
        [
            {"role": "user", "content": "第一条"},
            {"role": "assistant", "content": "第二条"},
            {"role": "user", "content": "第三条 Python"},
        ],
    )

    messages, matched = store.fetch_messages(42, 101, seq=2, context=1)

    assert matched == 1
    assert [m["seq"] for m in messages] == [1, 2]
    assert messages[-1]["in_source_ref"] is True
    assert messages[-1]["source_ref"] == "session:42:101#msg:2"
    print("test_fetch_messages_by_message_ref_with_context: PASS")


def test_fetch_messages_by_message_range_ref() -> None:
    init_db()
    store = SessionStore()
    store.save(
        42,
        102,
        [
            {"role": "user", "content": "第一条"},
            {"role": "assistant", "content": "第二条"},
            {"role": "user", "content": "第三条 Python"},
            {"role": "assistant", "content": "第四条"},
        ],
    )

    messages, matched = store.fetch_messages(42, 102, seq=1, seq_end=2)

    assert matched == 2
    assert [m["seq"] for m in messages] == [1, 2]
    assert [m["in_source_ref"] for m in messages] == [True, True]
    print("test_fetch_messages_by_message_range_ref: PASS")


def test_load_state_preserves_last_consolidated() -> None:
    init_db()
    store = SessionStore()
    persisted_messages = [
        {"role": "user", "content": "第一条"},
        {"role": "assistant", "content": "第二条"},
        {"role": "user", "content": "第三条"},
    ]
    store.save(
        42,
        103,
        persisted_messages,
        last_consolidated=2,
    )

    state = store.load_state(42, 103)

    assert state is not None
    messages, last_consolidated = state
    assert messages == persisted_messages
    assert last_consolidated == 2
    assert store.load(42, 103) == persisted_messages

    updated_messages = persisted_messages + [{"role": "assistant", "content": "第四条"}]
    store.save(42, 103, updated_messages)
    updated_state = store.load_state(42, 103)

    assert updated_state is not None
    assert updated_state[0] == updated_messages
    assert updated_state[1] == 2
    print("test_load_state_preserves_last_consolidated: PASS")


def test_init_db_migrates_last_consolidated_column() -> None:
    original_db_path = settings.DATABASE_PATH
    migrated_db_path = tempfile.mktemp(suffix=".db")
    _reset_thread_connection()
    settings.DATABASE_PATH = migrated_db_path
    try:
        conn = sqlite3.connect(migrated_db_path)
        conn.execute(
            """
            CREATE TABLE conversation_sessions (
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                messages_json TEXT NOT NULL DEFAULT '[]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, chat_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO conversation_sessions (user_id, chat_id, messages_json)
            VALUES (?, ?, ?)
            """,
            (7, 8, '[{"role":"user","content":"旧库"}]'),
        )
        conn.commit()
        conn.close()

        init_db()
        migrated = sqlite3.connect(migrated_db_path)
        columns = {
            str(row[1])
            for row in migrated.execute("PRAGMA table_info(conversation_sessions)")
        }
        migrated.close()

        assert "last_consolidated" in columns
        state = SessionStore().load_state(7, 8)
        assert state is not None
        assert state[1] == 0
    finally:
        _reset_thread_connection()
        settings.DATABASE_PATH = original_db_path
    print("test_init_db_migrates_last_consolidated_column: PASS")


if __name__ == "__main__":
    test_search_messages_returns_source_refs()
    test_fetch_messages_by_message_ref_with_context()
    test_fetch_messages_by_message_range_ref()
    test_load_state_preserves_last_consolidated()
    test_init_db_migrates_last_consolidated_column()
    print("\nAll session store tests passed!")
