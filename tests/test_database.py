import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Set test environment variables
os.environ["TG_BOT_TOKEN"] = "test_token"
os.environ["DEEPSEEK_API_KEY"] = "test_deepseek_key"
os.environ["ALIYUN_DASHSCOPE_API_KEY"] = "test_aliyun_key"
os.environ["DATABASE_PATH"] = tempfile.mktemp(suffix=".db")

from persistence.database import init_db, get_connection


def test_init_db():
    """Test database initialization creates all required tables."""
    init_db()

    # Query sqlite_master to verify tables exist
    db_path = os.environ["DATABASE_PATH"]
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]

    conn.close()

    # Verify all three tables exist
    expected = {"memory_items", "vec_items", "memory_replacements"}
    actual = set(tables)

    assert expected.issubset(actual), f"Missing tables: {expected - actual}"
    print(f"Tables created: {sorted(tables)}")
    print("test_init_db: PASS")


def test_get_connection():
    """Test get_connection returns usable connection."""
    init_db()
    conn = get_connection()

    # Verify connection works
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM memory_items")
    count = cursor.fetchone()[0]
    assert count == 0  # Empty table

    print("test_get_connection: PASS")


def test_vec_table_exists():
    """Test vec_items virtual table was created."""
    init_db()
    conn = get_connection()

    cursor = conn.cursor()
    # Check vec_items structure
    cursor.execute("PRAGMA table_info(vec_items)")
    columns = [row[1] for row in cursor.fetchall()]

    assert "embedding_id" in columns
    assert "embedding" in columns

    print("test_vec_table_exists: PASS")


if __name__ == "__main__":
    test_init_db()
    test_get_connection()
    test_vec_table_exists()
    print("\nAll database tests passed!")
