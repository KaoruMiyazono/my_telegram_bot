from __future__ import annotations

from pathlib import Path

from config.settings import settings
from memory.embedder import Embedder
from memory.engine import DefaultMemoryEngine
from memory.markdown_store import MarkdownMemoryRuntime, MarkdownMemoryStore
from memory.runtime import MemoryRuntime
from memory.store import MemoryStore
from persistence.session_store import SessionStore


def default_markdown_memory_root() -> Path:
    return Path(settings.DATABASE_PATH).parent / "markdown_memory"


def build_memory_runtime(
    *,
    embedder: Embedder,
    memory_store: MemoryStore,
    session_store: SessionStore,
    markdown_root: Path | None = None,
) -> MemoryRuntime:
    markdown_store = MarkdownMemoryStore(markdown_root or default_markdown_memory_root())
    engine = DefaultMemoryEngine(
        store=memory_store,
        embedder=embedder,
        session_store=session_store,
    )
    return MemoryRuntime(
        markdown=MarkdownMemoryRuntime(store=markdown_store),
        engine=engine,
    )
