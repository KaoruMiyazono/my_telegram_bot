from __future__ import annotations

from dataclasses import dataclass

from memory.engine import MemoryEngine
from memory.markdown_store import MarkdownMemoryRuntime


@dataclass
class MemoryRuntime:
    markdown: MarkdownMemoryRuntime
    engine: MemoryEngine
