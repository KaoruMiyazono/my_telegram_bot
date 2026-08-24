"""Durable, resumable executor for low-priority local maintenance work."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from config.settings import settings

from agent.runtime.mode_coordinator import ModeCoordinator

logger = logging.getLogger(__name__)

IdlePermission = Literal["local_read", "local_maintenance"]
IdleHandler = Callable[["IdleTaskContext"], "IdleTaskResult | None | Awaitable[IdleTaskResult | None]"]


@dataclass(frozen=True)
class IdleTask:
    task_id: str
    task_type: str
    session_key: str | None
    priority: int
    status: str
    checkpoint: dict[str, Any]
    attempts: int
    not_before: datetime
    trace_id: str
    last_error: str = ""


@dataclass(frozen=True)
class IdleTaskResult:
    checkpoint: dict[str, Any] = field(default_factory=dict)
    repeat_after_seconds: float | None = None


class IdleTaskContext:
    def __init__(self, task: IdleTask, save: Callable[[dict[str, Any]], None]) -> None:
        self.task = task
        self.checkpoint = dict(task.checkpoint)
        self._save = save

    def save_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        """Persist the latest safe resume point before starting the next unit."""
        self.checkpoint = dict(checkpoint)
        self._save(self.checkpoint)


class IdleTaskStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self._db = sqlite3.connect(str(path or settings.DATABASE_PATH))
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout = 5000")
        self._ensure_schema()

    def enqueue(
        self,
        task_type: str,
        *,
        session_key: str | None = None,
        priority: int = 100,
        checkpoint: dict[str, Any] | None = None,
        not_before: datetime | None = None,
        trace_id: str = "",
    ) -> IdleTask:
        task_id = f"idle:{uuid4().hex}"
        now = _now()
        due = _aware(not_before or now)
        self._db.execute(
            """INSERT INTO idle_tasks(
                task_id, task_type, session_key, priority, status,
                checkpoint_json, attempts, not_before, trace_id, last_error,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'queued', ?, 0, ?, ?, '', ?, ?)""",
            (
                task_id,
                task_type,
                session_key,
                int(priority),
                json.dumps(checkpoint or {}, ensure_ascii=False),
                due.isoformat(),
                trace_id,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        self._db.commit()
        task = self.get(task_id)
        assert task is not None
        return task

    def claim_next(self, now: datetime | None = None) -> IdleTask | None:
        current = _aware(now or _now()).isoformat()
        self._db.execute("BEGIN IMMEDIATE")
        row = self._db.execute(
            """SELECT task_id FROM idle_tasks
               WHERE status IN ('queued', 'paused') AND not_before <= ?
               ORDER BY priority ASC, created_at ASC LIMIT 1""",
            (current,),
        ).fetchone()
        if row is None:
            self._db.commit()
            return None
        task_id = str(row[0])
        self._db.execute(
            """UPDATE idle_tasks SET status='running', attempts=attempts+1,
               updated_at=? WHERE task_id=?""",
            (current, task_id),
        )
        self._db.commit()
        return self.get(task_id)

    def save_checkpoint(self, task_id: str, checkpoint: dict[str, Any]) -> None:
        self._db.execute(
            "UPDATE idle_tasks SET checkpoint_json=?, updated_at=? WHERE task_id=?",
            (json.dumps(checkpoint, ensure_ascii=False), _now().isoformat(), task_id),
        )
        self._db.commit()

    def pause(self, task_id: str, checkpoint: dict[str, Any]) -> None:
        self._settle(task_id, "paused", checkpoint=checkpoint)

    def complete(self, task_id: str, checkpoint: dict[str, Any]) -> None:
        self._settle(task_id, "done", checkpoint=checkpoint)

    def fail(self, task_id: str, error: str) -> None:
        self._settle(task_id, "failed", error=error)

    def recover_running(self) -> int:
        cursor = self._db.execute(
            """UPDATE idle_tasks SET status='paused', updated_at=?
               WHERE status='running'""",
            (_now().isoformat(),),
        )
        self._db.commit()
        return int(cursor.rowcount)

    def get(self, task_id: str) -> IdleTask | None:
        row = self._db.execute(
            "SELECT * FROM idle_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        return _task_from_row(row) if row is not None else None

    def list(self, *, status: str | None = None) -> list[IdleTask]:
        if status is None:
            rows = self._db.execute("SELECT * FROM idle_tasks ORDER BY created_at").fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM idle_tasks WHERE status=? ORDER BY created_at", (status,)
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    def close(self) -> None:
        self._db.close()

    def _settle(
        self,
        task_id: str,
        status: str,
        *,
        checkpoint: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        self._db.execute(
            """UPDATE idle_tasks SET status=?, checkpoint_json=COALESCE(?, checkpoint_json),
               last_error=?, updated_at=? WHERE task_id=?""",
            (
                status,
                json.dumps(checkpoint, ensure_ascii=False) if checkpoint is not None else None,
                error,
                _now().isoformat(),
                task_id,
            ),
        )
        self._db.commit()

    def _ensure_schema(self) -> None:
        self._db.executescript(
            """CREATE TABLE IF NOT EXISTS idle_tasks (
                task_id TEXT PRIMARY KEY,
                task_type TEXT NOT NULL,
                session_key TEXT,
                priority INTEGER NOT NULL DEFAULT 100,
                status TEXT NOT NULL CHECK(status IN (
                    'queued', 'running', 'paused', 'done', 'failed'
                )),
                checkpoint_json TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                not_before TEXT NOT NULL,
                trace_id TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_idle_tasks_ready
            ON idle_tasks(status, not_before, priority, created_at);"""
        )
        self._db.commit()


class IdleTaskRuntime:
    """Runs one local maintenance unit only while higher-priority modes are idle."""

    def __init__(
        self,
        store: IdleTaskStore,
        coordinator: ModeCoordinator,
        *,
        poll_seconds: float = 5.0,
    ) -> None:
        self.store = store
        self.coordinator = coordinator
        self.poll_seconds = max(0.01, float(poll_seconds))
        self._handlers: dict[str, tuple[IdleHandler, IdlePermission]] = {}
        self._worker: asyncio.Task[None] | None = None
        self._active_execution: asyncio.Task[None] | None = None
        self._active_task: IdleTask | None = None
        self._wake = asyncio.Event()
        self._stopping = False

    def register(
        self, task_type: str, handler: IdleHandler, *, permission: IdlePermission
    ) -> None:
        if permission not in {"local_read", "local_maintenance"}:
            raise ValueError("Idle tasks cannot send messages or mutate external services")
        self._handlers[task_type] = (handler, permission)

    def enqueue(self, task_type: str, **kwargs: Any) -> IdleTask:
        if task_type not in self._handlers:
            raise ValueError(f"Idle task handler is not registered: {task_type}")
        task = self.store.enqueue(task_type, **kwargs)
        self._wake.set()
        return task

    async def start(self) -> None:
        self._stopping = False
        self.store.recover_running()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="idle-task-runtime")

    async def close(self) -> None:
        self._stopping = True
        if self._active_execution is not None:
            self._active_execution.cancel()
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        self._worker = None
        self.store.close()

    async def pause_for_passive(self) -> None:
        task = self._active_execution
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def resume_after_passive(self) -> None:
        self._wake.set()

    async def run_once(self) -> IdleTask | None:
        if not self.coordinator.idle_allowed:
            return None
        task = self.store.claim_next()
        if task is None:
            return None
        registered = self._handlers.get(task.task_type)
        if registered is None:
            self.store.fail(task.task_id, "handler_not_registered")
            return task
        handler, _permission = registered
        self._active_task = task
        self._active_execution = asyncio.create_task(
            self._execute(task, handler), name=f"idle:{task.task_id}"
        )
        await asyncio.gather(self._active_execution, return_exceptions=True)
        self._active_execution = None
        self._active_task = None
        return task

    async def _execute(self, task: IdleTask, handler: IdleHandler) -> None:
        context = IdleTaskContext(
            task, lambda checkpoint: self.store.save_checkpoint(task.task_id, checkpoint)
        )
        try:
            result = handler(context)
            if inspect.isawaitable(result):
                result = await result
        except asyncio.CancelledError:
            self.store.pause(task.task_id, context.checkpoint)
            raise
        except Exception as exc:
            logger.exception("Idle task failed task_id=%s", task.task_id)
            self.store.fail(task.task_id, str(exc))
        else:
            final = result or IdleTaskResult(checkpoint=context.checkpoint)
            self.store.complete(task.task_id, final.checkpoint)
            if final.repeat_after_seconds is not None and not self._stopping:
                self.enqueue(
                    task.task_type,
                    session_key=task.session_key,
                    priority=task.priority,
                    checkpoint={},
                    not_before=_now() + timedelta(seconds=max(0.01, final.repeat_after_seconds)),
                    trace_id=task.trace_id,
                )

    async def _run(self) -> None:
        while not self._stopping:
            self._wake.clear()
            try:
                ran = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Idle task runtime iteration failed")
                ran = None
            if ran is not None:
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass


def _task_from_row(row: sqlite3.Row) -> IdleTask:
    return IdleTask(
        task_id=str(row["task_id"]),
        task_type=str(row["task_type"]),
        session_key=str(row["session_key"]) if row["session_key"] is not None else None,
        priority=int(row["priority"]),
        status=str(row["status"]),
        checkpoint=dict(json.loads(str(row["checkpoint_json"] or "{}"))),
        attempts=int(row["attempts"]),
        not_before=_aware(datetime.fromisoformat(str(row["not_before"]))),
        trace_id=str(row["trace_id"]),
        last_error=str(row["last_error"]),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
