import sqlite3
import threading
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from agent.core.types import MemoryItem
from config.settings import settings
from memory.embedder import Embedder
from persistence.database import get_connection, init_db

LONG_TERM_MEMORY_TYPES = ["profile", "preference", "procedure", "event", "fact"]


# 创建、搜索、查询、更新 AI Agent 的长期记忆。
class MemoryStore:
    def __init__(self, embedder: Embedder) -> None:
        #  这里的embedding工具是用来将文本转换为向量表示的，便于后续的向量搜索和相似度计算。 也就是之前的 embedding_model = OpenAIEmbeddingModel() 这个类的实例化对象
        self.embedder = embedder

    #  本质上就是将一条记忆插入到数据库中，并生成对应的向量表示（embedding），以便后续进行向量搜索和相似度计算。
    async def upsert_item(
        self,
        memory_type: str,
        summary: str,
        user_id: int,
        emotional_weight: int = 0,
        source_ref: str | None = None,
    ) -> MemoryItem:
        """Insert or update a memory item with embedding."""
        # Generate embedding
        #  生成embedding->通过uuid的到一个唯一的id->获取数据库连接->创建游标->插入到memory_items表格中->插入到vec_items表格中->提交事务->返回MemoryItem对象
        embedding = await self.embedder.embed(summary)
        item_id = uuid4()

        conn = get_connection()
        #  拿到游标对象，方便操作数据集
        cursor = conn.cursor()

        # Insert into memory_items 存二进制更高效
        cursor.execute(
            """
            INSERT INTO memory_items (id, user_id, memory_type, summary, embedding, status, source_ref)
            VALUES (?, ?, ?, ?, ?, 'active', ?)
            """,
            (str(item_id), user_id, memory_type, summary, _encode_embedding(embedding), source_ref),
        )

        # Insert into vec_items for vector search
        cursor.execute(
            """
            INSERT INTO vec_items (embedding_id, embedding)
            VALUES (?, ?)
            """,
            (str(item_id), _encode_embedding(embedding)),
        )

        conn.commit()

        return MemoryItem(
            id=item_id,
            user_id=user_id,
            memory_type=memory_type,
            summary=summary,
            embedding=embedding,
            status="active",
            source_ref=source_ref,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )

    async def reconcile_optimized(self, *, user_id: int, entries: list[Any]) -> dict[str, int]:
        """Atomically make vector memory mirror the optimizer's stable Markdown."""
        prepared: list[tuple[Any, list[float]]] = []
        for entry in entries:
            prepared.append((entry, await self.embedder.embed(str(entry.summary))))

        conn = get_connection()
        cursor = conn.cursor()
        inserted = 0
        skipped = 0
        superseded = 0
        invalidated = 0
        desired_keys = {
            (str(entry.memory_type), _normalize_summary(str(entry.summary)))
            for entry, _embedding in prepared
        }
        try:
            cursor.execute("BEGIN")
            active_rows = cursor.execute(
                """
                SELECT id, memory_type, summary, source_ref, topic_key
                FROM memory_items
                WHERE user_id = ? AND status = 'active' AND origin = 'optimizer'
                """,
                (user_id,),
            ).fetchall()
            active_by_key = {
                (str(row[1]), _normalize_summary(str(row[2]))): row for row in active_rows
            }
            active_by_topic = {
                str(row[4]): row for row in active_rows if str(row[4] or "").strip()
            }
            retained_ids: set[str] = set()

            for entry, embedding in prepared:
                key = (str(entry.memory_type), _normalize_summary(str(entry.summary)))
                existing = active_by_key.get(key)
                if existing is not None:
                    retained_ids.add(str(existing[0]))
                    skipped += 1
                    continue

                item_id = str(uuid4())
                topic_key = str(getattr(entry, "topic_key", "") or "").strip() or None
                source_ref = str(getattr(entry, "source_ref", "") or "").strip() or None
                encoded = _encode_embedding(embedding)
                cursor.execute(
                    """
                    INSERT INTO memory_items (
                        id, user_id, memory_type, summary, embedding, status,
                        source_ref, origin, topic_key
                    ) VALUES (?, ?, ?, ?, ?, 'active', ?, 'optimizer', ?)
                    """,
                    (item_id, user_id, entry.memory_type, entry.summary, encoded, source_ref, topic_key),
                )
                cursor.execute(
                    "INSERT INTO vec_items (embedding_id, embedding) VALUES (?, ?)",
                    (item_id, encoded),
                )
                inserted += 1
                retained_ids.add(item_id)

                old = active_by_topic.get(topic_key or "")
                if old is not None and str(old[0]) not in retained_ids:
                    cursor.execute(
                        "UPDATE memory_items SET status = 'superseded', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (str(old[0]),),
                    )
                    cursor.execute(
                        """
                        INSERT OR IGNORE INTO memory_replacements (
                            old_id, new_id, relation_type, topic_key, reason,
                            old_source_ref, new_source_ref
                        ) VALUES (?, ?, 'supersede', ?, ?, ?, ?)
                        """,
                        (
                            str(old[0]), item_id, topic_key,
                            "stable memory changed for the same topic",
                            old[3], source_ref,
                        ),
                    )
                    superseded += 1

            for row in active_rows:
                old_key = (str(row[1]), _normalize_summary(str(row[2])))
                old_id = str(row[0])
                if old_id in retained_ids or old_key in desired_keys:
                    continue
                if str(row[4] or "") in {
                    str(getattr(entry, "topic_key", "") or "") for entry, _ in prepared
                }:
                    continue
                cursor.execute(
                    "UPDATE memory_items SET status = 'invalidated', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (old_id,),
                )
                invalidated += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {
            "inserted": inserted,
            "skipped": skipped,
            "superseded": superseded,
            "invalidated": invalidated,
        }

    #  根据vector向量进行搜索，返回最相似的记忆项。 主要是利用sqlite-vec这个库进行向量搜索。
    async def vector_search(
        self,
        query_vec: list[float],
        user_id: int,
        top_k: int = 5,
        memory_types: list[str] | None = None,
        include_superseded: bool = False,
    ) -> list[MemoryItem]:
        """Search memories by vector similarity using sqlite-vec."""
        conn = get_connection()
        cursor = conn.cursor()

        query_bytes = _encode_embedding(query_vec)

        # Build type filter
        type_filter = ""
        params: list[Any] = []
        if memory_types:
            placeholders = ",".join(["?"] * len(memory_types))
            type_filter = f"AND mi.memory_type IN ({placeholders})"
            params.extend(memory_types)

        # sqlite-vec vector search using vec_distance_l2  将vec表格和memory_items表格进行连接，计算向量距离，并按距离排序，返回最相似的记忆项。
        #  会过滤 用户 id、记忆状态（active或superseded）、记忆类型（如果提供了memory_types参数），并限制返回的结果数量为top_k。
        sql = f"""
            SELECT
                mi.id, mi.user_id, mi.memory_type, mi.summary,
                mi.embedding, mi.status, mi.source_ref, mi.created_at, mi.updated_at,
                vec_distance_l2(v.embedding, ?) as distance
            FROM vec_items v
            JOIN memory_items mi ON v.embedding_id = mi.id
            WHERE mi.user_id = ?
                AND mi.status IN ({','.join(['?'] * (2 if include_superseded else 1))})
                {type_filter}
            ORDER BY distance
            LIMIT ?
        """

        statuses = ["active", "superseded"] if include_superseded else ["active"]
        params = [query_bytes, user_id] + statuses + params + [top_k]
        cursor.execute(sql, params)

        results = []
        for row in cursor.fetchall():
            results.append(
                MemoryItem(
                    #  封装成UUID对象，便于后续操作
                    id=UUID(row[0]),
                    user_id=row[1],
                    memory_type=row[2],
                    summary=row[3],
                    embedding=_decode_embedding(row[4]),
                    status=row[5],
                    source_ref=row[6],
                    created_at=datetime.fromisoformat(row[7]) if row[7] else datetime.utcnow(),
                    updated_at=datetime.fromisoformat(row[8]) if row[8] else datetime.utcnow(),
                )
            )

        return results

    #  按照关键词进行检索，和上面的按照embedding组织的检索不同，这里是直接在summary字段上进行模糊匹配，返回包含关键词的记忆项。 主要是利用sqlite的LIKE操作符进行模糊匹配。
    async def keyword_search(
        self,
        terms: str,
        user_id: int,
        limit: int = 3,
        memory_types: list[str] | None = None,
        include_superseded: bool = False,
    ) -> list[MemoryItem]:
        """Simple keyword search using LIKE."""
        conn = get_connection()
        cursor = conn.cursor()

        type_filter = ""
        params: list[Any] = [user_id]
        statuses = ["active", "superseded"] if include_superseded else ["active"]
        status_filter = ",".join(["?"] * len(statuses))
        params.extend(statuses)
        if memory_types:
            placeholders = ",".join(["?"] * len(memory_types))
            type_filter = f"AND memory_type IN ({placeholders})"
            params.extend(memory_types)
        params.extend([f"%{terms}%", limit])

        cursor.execute(
            f"""
            SELECT id, user_id, memory_type, summary, embedding, status, source_ref, created_at, updated_at
            FROM memory_items
            WHERE user_id = ? AND status IN ({status_filter}) {type_filter} AND summary LIKE ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            params,
        )

        results = []
        for row in cursor.fetchall():
            results.append(
                MemoryItem(
                    id=UUID(row[0]),
                    user_id=row[1],
                    memory_type=row[2],
                    summary=row[3],
                    embedding=_decode_embedding(row[4]),
                    status=row[5],
                    source_ref=row[6],
                    created_at=datetime.fromisoformat(row[7]) if row[7] else datetime.utcnow(),
                    updated_at=datetime.fromisoformat(row[8]) if row[8] else datetime.utcnow(),
                )
            )

        return results

    #  列出一个用户的所有记忆项，可以按类型、创建时间范围进行过滤，并可选择是否包含已被替代的记忆项。 主要是利用sqlite的查询功能进行数据筛选。
    #  其实就是数据库的查询
    def list_memories(
        self,
        *,
        user_id: int,
        memory_types: list[str] | None = None,
        created_start: datetime | None = None,
        created_end: datetime | None = None,
        include_superseded: bool = False,
        limit: int = 50,
    ) -> list[MemoryItem]:
        """List active memories with optional type and created_at filters."""
        conn = get_connection()
        cursor = conn.cursor()

        #  默认只查询active状态的记忆项，如果include_superseded为True，则同时查询active和superseded状态的记忆项。
        statuses = ["active", "superseded"] if include_superseded else ["active"]
        placeholders = ",".join(["?"] * len(statuses))
        clauses = ["user_id = ?", f"status IN ({placeholders})"]
        params: list[Any] = [user_id]
        params.extend(statuses)
        if memory_types:
            placeholders = ",".join(["?"] * len(memory_types))
            clauses.append(f"memory_type IN ({placeholders})")
            params.extend(memory_types)
        #  规定查询的开始和结束时间，如果提供了created_start和created_end参数，则会在查询条件中加入对应的时间范围过滤。
        if created_start is not None:
            clauses.append("created_at >= ?")
            params.append(created_start.isoformat(sep=" "))
        if created_end is not None:
            clauses.append("created_at < ?")
            params.append(created_end.isoformat(sep=" "))

        #  限制返回的结果数量，确保不会返回过多的记忆项。 这里的limit参数会被限制在1到200之间，避免一次性查询过多数据。
        params.append(max(1, min(int(limit), 200)))
        rows = cursor.execute(
            f"""
            SELECT id, user_id, memory_type, summary, embedding, status, source_ref, created_at, updated_at
            FROM memory_items
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

        return [
            MemoryItem(
                id=UUID(row[0]),
                user_id=row[1],
                memory_type=row[2],
                summary=row[3],
                embedding=_decode_embedding(row[4]),
                status=row[5],
                source_ref=row[6],
                created_at=datetime.fromisoformat(row[7]) if row[7] else datetime.utcnow(),
                updated_at=datetime.fromisoformat(row[8]) if row[8] else datetime.utcnow(),
            )
            for row in rows
        ]

    #  memory更新机制，主要是为了处理记忆的替代关系。当一个新的记忆项被创建时，可能会替代之前的一些旧记忆项。这个方法会将旧记忆项标记为“superseded”，并记录它们与新记忆项之间的替代关系。
    #  他没有操作新的记忆项，只是将旧的记忆项标记为“superseded”，并在memory_replacements表中记录它们与新记忆项之间的关系。这样可以在后续的查询中知道哪些记忆项已经被替代，以及它们的替代者是谁。
    async def supersede(
        self, old_ids: list[UUID], new_id: UUID, relation_type: str = "supersede"
    ) -> None:
        """Mark old memories as superseded and track the replacement."""
        conn = get_connection()
        cursor = conn.cursor()

        for old_id in old_ids:
            # Update status of old memory
            cursor.execute(
                """
                UPDATE memory_items
                SET status = 'superseded', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (str(old_id),),
            )

            # Track replacement relationship
            cursor.execute(
                """
                INSERT INTO memory_replacements (old_id, new_id)
                VALUES (?, ?)
                """,
                (str(old_id), str(new_id)),
            )

        conn.commit()

    #  上面的supersede方法是针对单个记忆项的替代操作，而mark_superseded_batch方法则是针对一批记忆项的替代操作。它会将指定的一批记忆项标记为“superseded”，并返回实际被更新的记忆项ID列表。这个方法主要用于批量处理记忆项的替代关系，确保在一次操作中可以同时处理多个记忆项。
    def mark_superseded_batch(
        self,
        ids: list[str | UUID],
        *,
        user_id: int | None = None,
    ) -> list[str]:
        """Mark active memories as superseded and return ids actually updated."""
        clean_ids = []
        seen = set()
        for raw in ids or []:
            item_id = str(raw).strip()
            if item_id and item_id not in seen:
                seen.add(item_id)
                clean_ids.append(item_id)
        if not clean_ids:
            return []

        conn = get_connection()
        cursor = conn.cursor()
        placeholders = ",".join(["?"] * len(clean_ids))
        params: list[Any] = clean_ids.copy()
        user_filter = ""
        if user_id is not None:
            user_filter = "AND user_id = ?"
            params.append(user_id)

        rows = cursor.execute(
            f"""
            SELECT id
            FROM memory_items
            WHERE id IN ({placeholders}) AND status = 'active' {user_filter}
            """,
            params,
        ).fetchall()
        updated_ids = [str(row[0]) for row in rows]
        if not updated_ids:
            return []

        update_placeholders = ",".join(["?"] * len(updated_ids))
        update_params: list[Any] = updated_ids.copy()
        if user_id is not None:
            update_params.append(user_id)

        cursor.execute(
            f"""
            UPDATE memory_items
            SET status = 'superseded', updated_at = CURRENT_TIMESTAMP
            WHERE id IN ({update_placeholders}) {user_filter}
            """,
            update_params,
        )
        conn.commit()
        return updated_ids


#  二进制转换
def _encode_embedding(vec: list[float]) -> bytes:
    """Encode float vector as bytes for sqlite-vec."""
    import struct

    return struct.pack(f"{len(vec)}f", *vec)


def _decode_embedding(data: bytes | None) -> list[float] | None:
    """Decode bytes to float vector."""
    if data is None:
        return None
    import struct

    return list(struct.unpack(f"{len(data) // 4}f", data))


def _normalize_summary(value: str) -> str:
    return " ".join(value.strip().lower().split())
