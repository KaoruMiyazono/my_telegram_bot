import asyncio
import tempfile
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.markdown_store import MarkdownMemoryStore
from memory.markdown_vector_sync import MarkdownVectorSync, parse_memory_markdown


class FakeMemoryStore:
    def __init__(self, existing=None) -> None:
        self.items = [
            SimpleNamespace(memory_type=memory_type, summary=summary)
            for memory_type, summary in (existing or [])
        ]
        self.upserts = []

    def list_memories(self, **kwargs):
        return list(self.items)

    async def upsert_item(self, **kwargs):
        self.upserts.append(kwargs)
        self.items.append(
            SimpleNamespace(
                memory_type=kwargs["memory_type"],
                summary=kwargs["summary"],
            )
        )
        return SimpleNamespace(**kwargs)


def test_parse_memory_markdown_maps_stable_sections() -> None:
    content = """# Long-term Memory

## User Facts
- 用户是后端工程师。

## User Preferences
- 用户喜欢直接回答。

## Requested Long-term Memory
- 用户要求长期记住项目路线。

## Assistant Operation Context
- 回答架构问题时先对齐 akashic-agent。
- none
"""

    entries = parse_memory_markdown(content)

    assert [(entry.memory_type, entry.summary) for entry in entries] == [
        ("profile", "用户是后端工程师。"),
        ("preference", "用户喜欢直接回答。"),
        ("fact", "用户要求长期记住项目路线。"),
        ("procedure", "回答架构问题时先对齐 akashic-agent。"),
    ]
    print("test_parse_memory_markdown_maps_stable_sections: PASS")


def test_parse_memory_markdown_top_level_bullets_default_to_profile() -> None:
    entries = parse_memory_markdown("# Long-term Memory\n\n- 用户是后端工程师。\n")

    assert [(entry.memory_type, entry.summary) for entry in entries] == [
        ("profile", "用户是后端工程师。"),
    ]
    print("test_parse_memory_markdown_top_level_bullets_default_to_profile: PASS")


async def test_sync_user_inserts_missing_bullets_without_source_ref() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        markdown = MarkdownMemoryStore(Path(tmp))
        markdown.write_long_term(
            42,
            """# Long-term Memory

## User Facts
- 用户是后端工程师。 [↗ session:42:7#msg:0-1]
- 用户是后端工程师。

## User Preferences
- 用户喜欢直接回答。

## Assistant Operation Context
- 架构设计先参考 akashic-agent。
""",
        )
        vector_store = FakeMemoryStore(existing=[("preference", "用户喜欢直接回答。")])
        sync = MarkdownVectorSync(vector_store)

        result = await sync.sync_user(markdown=markdown, user_id=42)

        assert result.parsed_count == 3
        assert result.inserted_count == 2
        assert result.skipped_count == 1
        assert [call["memory_type"] for call in vector_store.upserts] == [
            "profile",
            "procedure",
        ]
        assert all(call["user_id"] == 42 for call in vector_store.upserts)
        assert all(call["source_ref"] is None for call in vector_store.upserts)
        print("test_sync_user_inserts_missing_bullets_without_source_ref: PASS")


async def main() -> None:
    test_parse_memory_markdown_maps_stable_sections()
    test_parse_memory_markdown_top_level_bullets_default_to_profile()
    await test_sync_user_inserts_missing_bullets_without_source_ref()
    print("\nAll markdown vector sync tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
