from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class MarkdownMemoryRuntime:
    store: "MarkdownMemoryStore"


#  设计，每个用户都有一个自己的markdown格式的记忆文件，沉淀出人类的长期记忆，类似于akashic的概念。每个用户的记忆文件夹中包含以下文件：
#  - MEMORY.md：存储用户的长期记忆内容。
#  - SELF.md：存储用户的自我模型信息。
#  - HISTORY.md：存储用户的历史记录。
#  - PENDING.md：存储用户的待处理记忆内容。
#  - RECENT_CONTEXT.md：存储用户的最近上下文信息。
#  - PROACTIVE_CONTEXT.md：存储用户的主动上下文信息。
class MarkdownMemoryStore:
    """Per-user Akashic-style Markdown memory file layer."""

    def __init__(self, root: Path) -> None:
        self.root = root
    #  初始化这些.md在用户的文件夹中，如果不存在就创建它们，并写入默认内容。并且创建一个sqlite数据库文件，用于记录每个source_ref和kind的写入时间，防止重复写入。
    def ensure_user(self, user_id: int) -> Path:
        base = self._user_root(user_id)
        memory_dir = base / "memory"
        journal_dir = memory_dir / "journal"
        journal_dir.mkdir(parents=True, exist_ok=True)
        _ensure_file(memory_dir / "MEMORY.md", "# Long-term Memory\n\n")
        _ensure_file(memory_dir / "SELF.md", "# Self Model\n\n")
        _ensure_file(memory_dir / "HISTORY.md", "# History\n\n")
        _ensure_file(memory_dir / "PENDING.md", "# Pending Memory\n\n")
        _ensure_file(memory_dir / "RECENT_CONTEXT.md", _default_recent_context())
        _ensure_file(base / "PROACTIVE_CONTEXT.md", "# Proactive Context\n\n")
        #  给每个用户创建一个小 SQLite 数据库，用来记录“哪些 memory 写入操作已经执行过”，防止重复写。
        self._ensure_writes_db(user_id)
        return base

    #  读memory文件夹下的MEMORY.md文件内容，返回字符串
    def read_long_term(self, user_id: int) -> str:
        self.ensure_user(user_id)
        return self._memory_file(user_id, "MEMORY.md").read_text(encoding="utf-8")

    #  写入memory文件夹下的MEMORY.md文件内容，覆盖原有内容
    def write_long_term(self, user_id: int, content: str) -> None:
        self.ensure_user(user_id)
        self._memory_file(user_id, "MEMORY.md").write_text(
            content.rstrip() + "\n",
            encoding="utf-8",
        )

    def read_self(self, user_id: int) -> str:
        self.ensure_user(user_id)
        return self._memory_file(user_id, "SELF.md").read_text(encoding="utf-8")

    def write_self(self, user_id: int, content: str) -> None:
        self.ensure_user(user_id)
        self._memory_file(user_id, "SELF.md").write_text(
            content.rstrip() + "\n",
            encoding="utf-8",
        )

    def read_recent_context(self, user_id: int) -> str:
        self.ensure_user(user_id)
        return self._memory_file(user_id, "RECENT_CONTEXT.md").read_text(
            encoding="utf-8"
        )

    def write_recent_context(self, user_id: int, content: str) -> None:
        self.ensure_user(user_id)
        self._memory_file(user_id, "RECENT_CONTEXT.md").write_text(
            content.rstrip() + "\n",
            encoding="utf-8",
        )

    def read_pending(self, user_id: int) -> str:
        self.ensure_user(user_id)
        return _clean_pending_text(
            self._memory_file(user_id, "PENDING.md").read_text(encoding="utf-8")
        )

    #  读出最后10条对话，并写入RECENT_CONTEXT.md文件中
    def write_recent_turns(
        self,
        *,
        user_id: int,
        messages: list[dict],
        keep_count: int = 10,
    ) -> None:
        self.ensure_user(user_id)
        existing = self.read_recent_context(user_id)
        recent_turns = _format_recent_turns(messages[-max(1, keep_count):])
        self.write_recent_context(
            user_id,
            _replace_recent_turns(existing, recent_turns),
        )

    def append_history_once(
        self,
        *,
        user_id: int,
        entries: Iterable[str],
        source_ref: str,
        kind: str = "history_entry",
    ) -> bool:
        clean_entries = [entry.strip() for entry in entries if entry.strip()]
        if not clean_entries:
            return False
        self.ensure_user(user_id)
        if not self._claim_write(user_id, source_ref=source_ref, kind=kind):
            return False
        #  防止重复写入
        marker = _marker(source_ref, kind)
        text = marker + "\n" + "\n".join(clean_entries) + "\n\n"
        with self._memory_file(user_id, "HISTORY.md").open("a", encoding="utf-8") as fh:
            fh.write(text)
        return True

    def append_pending_once(
        self,
        *,
        user_id: int,
        items: Iterable[str],
        source_ref: str,
        kind: str = "pending_items",
    ) -> bool:
        clean_items = [item.strip() for item in items if item.strip()]
        if not clean_items:
            return False
        self.ensure_user(user_id)
        if not self._claim_write(user_id, source_ref=source_ref, kind=kind):
            return False
        marker = _marker(source_ref, kind)
        text = marker + "\n" + "\n".join(clean_items) + "\n\n"
        with self._memory_file(user_id, "PENDING.md").open("a", encoding="utf-8") as fh:
            fh.write(text)
        return True

    #  每天的日记写入journal文件夹下的YYYY-MM-DD.md文件中，格式为markdown
    def append_journal(
        self,
        *,
        user_id: int,
        date: str,
        entries: Iterable[str],
        source_ref: str,
    ) -> bool:
        clean_entries = [entry.strip() for entry in entries if entry.strip()]
        if not clean_entries:
            return False
        self.ensure_user(user_id)
        kind = f"journal:{date}"
        if not self._claim_write(user_id, source_ref=source_ref, kind=kind):
            return False
        path = self._memory_dir(user_id) / "journal" / f"{date}.md"
        _ensure_file(path, f"# Journal {date}\n\n")
        marker = _marker(source_ref, kind)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(marker + "\n" + "\n".join(clean_entries) + "\n\n")
        return True

    #
    def snapshot_pending(self, user_id: int) -> str:
        self.ensure_user(user_id)
        pending = self._memory_file(user_id, "PENDING.md")
        snapshot = pending.with_suffix(".snapshot.md")
        if snapshot.exists():
            self.rollback_pending_snapshot(user_id)
        text = pending.read_text(encoding="utf-8")
        clean = _clean_pending_text(text)
        if not clean:
            pending.write_text("# Pending Memory\n\n", encoding="utf-8")
            return ""
        pending.rename(snapshot)
        pending.write_text("# Pending Memory\n\n", encoding="utf-8")
        return clean

    def commit_pending_snapshot(self, user_id: int) -> None:
        snapshot = self._memory_file(user_id, "PENDING.snapshot.md")
        if snapshot.exists():
            snapshot.unlink()

    def rollback_pending_snapshot(self, user_id: int) -> None:
        self.ensure_user(user_id)
        pending = self._memory_file(user_id, "PENDING.md")
        snapshot = self._memory_file(user_id, "PENDING.snapshot.md")
        if not snapshot.exists():
            return
        existing = pending.read_text(encoding="utf-8") if pending.exists() else ""
        restored = snapshot.read_text(encoding="utf-8").rstrip()
        extra = existing.strip()
        if extra and extra != "# Pending Memory":
            restored = restored + "\n\n" + extra
        pending.write_text(restored.rstrip() + "\n", encoding="utf-8")
        snapshot.unlink()

    def backup_long_term(self, user_id: int, backup_name: str = "MEMORY.bak.md") -> None:
        self.ensure_user(user_id)
        source = self._memory_file(user_id, "MEMORY.md")
        shutil.copyfile(source, source.with_name(backup_name))

    def _user_root(self, user_id: int) -> Path:
        return self.root / "users" / str(user_id)

    def _memory_dir(self, user_id: int) -> Path:
        return self._user_root(user_id) / "memory"

    def _memory_file(self, user_id: int, name: str) -> Path:
        return self._memory_dir(user_id) / name

    def _writes_db(self, user_id: int) -> Path:
        return self._memory_file(user_id, "consolidation_writes.db")

    def _ensure_writes_db(self, user_id: int) -> None:
        path = self._writes_db(user_id)
        conn = sqlite3.connect(path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS writes (
                source_ref TEXT NOT NULL,
                kind TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (source_ref, kind)
            )
            """
        )
        conn.commit()
        conn.close()

    def _claim_write(self, user_id: int, *, source_ref: str, kind: str) -> bool:
        self._ensure_writes_db(user_id)
        conn = sqlite3.connect(self._writes_db(user_id))
        try:
            conn.execute(
                "INSERT INTO writes (source_ref, kind, created_at) VALUES (?, ?, ?)",
                (source_ref, kind, datetime.utcnow().isoformat()),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()


def _ensure_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def _default_recent_context() -> str:
    return """# Recent Context

## Compression
until: none
- none

## Ongoing Threads
- none

## Recent Turns
<!-- a-preview = assistant reply preview only -->
- none
"""


def _format_recent_turns(messages: list[dict]) -> str:
    lines: list[str] = []
    for message in messages:
        role = str(message.get("role") or "").lower()
        content = str(message.get("content") or "").strip()
        if not content or role not in {"user", "assistant"}:
            continue
        if role == "assistant":
            lines.append(f"[a-preview] {content[:80]}")
        else:
            lines.append(f"[user] {content}")
    return "\n".join(lines).strip() or "- none"


def _replace_recent_turns(existing: str, recent_turns: str) -> str:
    block = (
        "## Recent Turns\n"
        "<!-- a-preview = assistant reply preview only -->\n"
        f"{recent_turns.strip() or '- none'}\n"
    )
    marker = "\n## Recent Turns\n"
    text = (existing or _default_recent_context()).strip()
    if marker not in text:
        return text.rstrip() + "\n\n" + block
    prefix, _old = text.split(marker, 1)
    return prefix.rstrip() + "\n\n" + block


def _marker(source_ref: str, kind: str) -> str:
    payload = json.dumps(source_ref, ensure_ascii=False)
    return f"<!-- consolidation:{payload}:{kind} -->"


def _clean_pending_text(text: str) -> str:
    lines: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "# Pending Memory":
            continue
        if stripped.startswith("<!-- consolidation:") and stripped.endswith("-->"):
            continue
        lines.append(line.rstrip())
    return "\n".join(lines).strip()
