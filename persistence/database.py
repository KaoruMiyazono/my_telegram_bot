import sqlite3
import threading
from pathlib import Path
from typing import Final

import sqlite_vec

from config.settings import settings

# Ensure each thread has its own connection
_local: threading.local = None
_lock: threading.Lock = threading.Lock()

# SQLite Database
# │
# ├── memory_items
# │       |
# │       | 存 AI memory 内容
# │
# ├── vec_items
# │       |
# │       | 存 embedding 向量，用于搜索
# │
# ├── memory_replacements
# │       |
# │       | 记录旧 memory 被新 memory 替换
# │
# └── conversation_sessions
#         |
#         | 保存聊天 session

TABLE_SCHEMA: Final = """
-- Core memory storage
CREATE TABLE IF NOT EXISTS memory_items (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    memory_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    embedding BLOB,
    status TEXT NOT NULL DEFAULT 'active',
    source_ref TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Vector index for similarity search (sqlite-vec virtual table)
CREATE VIRTUAL TABLE IF NOT EXISTS vec_items USING vec0(
    embedding_id TEXT PRIMARY KEY,
    embedding FLOAT[1024]
);

-- Memory replacement tracking
CREATE TABLE IF NOT EXISTS memory_replacements (
    old_id TEXT NOT NULL,
    new_id TEXT NOT NULL,
    replaced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (old_id, new_id)
);

-- Session persistence (对齐 akashic sessions.db)
CREATE TABLE IF NOT EXISTS conversation_sessions (
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    messages_json TEXT NOT NULL DEFAULT '[]',
    last_consolidated INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, chat_id)
);

-- Durable async runtime queue and idempotency ledger
CREATE TABLE IF NOT EXISTS runtime_messages (
    id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('inbound', 'outbound')),
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    dedupe_key TEXT UNIQUE,
    payload_json TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    leased_until TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_runtime_messages_session
ON runtime_messages(session_key, created_at);

CREATE INDEX IF NOT EXISTS idx_runtime_messages_recovery
ON runtime_messages(direction, status, created_at);
"""


#  检查 conversation_sessions 表是否存在 last_consolidated 列，如果不存在则添加该列
#  为什么需要这个
#  因为在旧版本的数据库中，conversation_sessions 表可能没有 last_consolidated 列，而新版本的代码需要这个列来存储会话的最后合并时间。
def _ensure_conversation_session_columns(conn: sqlite3.Connection) -> None:
    """Apply lightweight migrations for existing conversation_sessions tables."""
    rows = conn.execute("PRAGMA table_info(conversation_sessions)").fetchall()
    existing = {str(row[1]) for row in rows}
    if "last_consolidated" not in existing:
        conn.execute(
            "ALTER TABLE conversation_sessions "
            "ADD COLUMN last_consolidated INTEGER NOT NULL DEFAULT 0"
        )


def init_db() -> None:
    """Initialize database with schema."""
    #  创建记忆文件夹，记忆都存储在 memory.db文件中
    db_path = Path(settings.DATABASE_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # 创建数据库连接对象
    conn = sqlite3.connect(str(db_path))
    #  启动开启拓展的模式，加载sqlite_vec拓展，因为默认情况下sqlite3不支持向量索引
    conn.enable_load_extension(True)
    #  sqlvec 让 SQLite 具备存储和搜索向量（embedding）的能力，把 SQLite 变成一个轻量级向量数据库。
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    #  执行以争端sql脚本，脚本作用是 创建表格，创建向量索引，创建记忆替换表格，创建会话表格
    conn.executescript(TABLE_SCHEMA)
    _ensure_conversation_session_columns(conn)
    #  提交事务并关闭连接
    conn.commit()
    conn.close()


def get_connection() -> sqlite3.Connection:
    """Get thread-local database connection with vec extension loaded."""
    global _local
    if _local is None:
        _local = threading.local()

    conn = getattr(_local, "conn", None)
    if conn is None:
        with _lock:
            conn = sqlite3.connect(settings.DATABASE_PATH)
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            _local.conn = conn
    return conn
