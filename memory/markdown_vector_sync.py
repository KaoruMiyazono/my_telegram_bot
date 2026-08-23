from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from memory.markdown_store import MarkdownMemoryStore


@dataclass(frozen=True)
class MarkdownMemoryEntry:
    memory_type: str
    summary: str
    source_ref: str = ""
    topic_key: str = ""


@dataclass(frozen=True)
class MarkdownVectorSyncResult:
    user_id: int
    parsed_count: int = 0
    inserted_count: int = 0
    skipped_count: int = 0


class MarkdownVectorSync:
    """Sync optimized Markdown profile bullets into the vector memory layer."""

    def __init__(self, memory_store: Any) -> None:
        self._memory_store = memory_store

    async def sync_user(
        self,
        *,
        markdown: MarkdownMemoryStore,
        user_id: int,
    ) -> MarkdownVectorSyncResult:
        entries = parse_memory_markdown(markdown.read_long_term(user_id))
        if not entries:
            return MarkdownVectorSyncResult(user_id=user_id)

        reconcile = getattr(self._memory_store, "reconcile_optimized", None)
        if callable(reconcile):
            result = await reconcile(user_id=user_id, entries=entries)
            return MarkdownVectorSyncResult(
                user_id=user_id,
                parsed_count=len(entries),
                inserted_count=int(result.get("inserted", 0)),
                skipped_count=int(result.get("skipped", 0)),
            )

        existing = {
            (str(item.memory_type), _normalize_summary(str(item.summary)))
            for item in self._memory_store.list_memories(
                user_id=user_id,
                memory_types=["profile", "preference", "procedure", "event", "fact"],
                include_superseded=False,
                limit=200,
            )
        }

        inserted = 0
        skipped = 0
        for entry in entries:
            key = (entry.memory_type, _normalize_summary(entry.summary))
            if key in existing:
                skipped += 1
                continue
            await self._memory_store.upsert_item(
                memory_type=entry.memory_type,
                summary=entry.summary,
                user_id=user_id,
                source_ref=entry.source_ref or None,
            )
            existing.add(key)
            inserted += 1

        return MarkdownVectorSyncResult(
            user_id=user_id,
            parsed_count=len(entries),
            inserted_count=inserted,
            skipped_count=skipped,
        )


def parse_memory_markdown(content: str) -> list[MarkdownMemoryEntry]:
    entries: list[MarkdownMemoryEntry] = []
    current_section = ""
    seen: set[tuple[str, str]] = set()

    for raw_line in (content or "").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            heading_level = len(stripped) - len(stripped.lstrip("#"))
            current_section = stripped.lstrip("#").strip() if heading_level >= 2 else ""
            continue
        summary, source_ref = _parse_bullet(stripped)
        if not summary or _is_placeholder(summary):
            continue
        memory_type = _section_to_memory_type(current_section)
        key = (memory_type, _normalize_summary(summary))
        if key in seen:
            continue
        seen.add(key)
        entries.append(MarkdownMemoryEntry(
            memory_type=memory_type,
            summary=summary,
            source_ref=source_ref,
            topic_key=_infer_topic_key(memory_type, summary),
        ))

    return entries


def _parse_bullet(line: str) -> tuple[str, str]:
    if not line.startswith(("- ", "* ")):
        return "", ""
    value = line[2:].strip()
    match = re.search(r"\s*\[↗([^\]]+)\]\s*$", value)
    source_ref = match.group(1).strip() if match else ""
    value = re.sub(r"\s*\[↗[^\]]+\]\s*$", "", value).strip()
    return value, source_ref


def _infer_topic_key(memory_type: str, summary: str) -> str:
    """Use a conservative topic key so obvious corrections form a lineage."""
    text = _normalize_summary(summary)
    groups = (
        ("device.phone", ("iphone", "android", "手机", "安卓")),
        ("work.company", ("公司", "就职", "入职", "employer")),
        ("work.role", ("职业", "工程师", "岗位", "职位")),
        ("location.home", ("居住", "住在", "城市", "搬到")),
        ("drink.preference", ("咖啡", "拿铁", "茶", "饮品")),
    )
    for topic, tokens in groups:
        if any(token in text for token in tokens):
            return f"{memory_type}:{topic}"
    return f"{memory_type}:fact:{text}"


def _section_to_memory_type(section: str) -> str:
    value = section.lower()
    if any(token in value for token in ("preference", "preferences", "偏好")):
        return "preference"
    if any(
        token in value
        for token in (
            "operation",
            "procedure",
            "procedures",
            "context",
            "操作",
            "流程",
            "规则",
        )
    ):
        return "procedure"
    if any(token in value for token in ("event", "events", "history", "事件", "历史")):
        return "event"
    if any(
        token in value
        for token in (
            "requested",
            "long-term",
            "long term",
            "remember",
            "要求",
            "记住",
        )
    ):
        return "fact"
    return "profile"


def _is_placeholder(value: str) -> bool:
    stripped = value.strip().strip("。.")
    return stripped.lower() in {
        "",
        "...",
        "none",
        "n/a",
        "null",
        "无",
        "暂无",
        "空",
        "（空）",
        "(empty)",
    }


def _normalize_summary(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())
