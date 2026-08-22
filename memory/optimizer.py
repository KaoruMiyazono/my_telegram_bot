from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from openai import AsyncOpenAI

from config.settings import settings
from memory.bootstrap import default_markdown_memory_root
from memory.markdown_store import MarkdownMemoryStore
from memory.markdown_vector_sync import MarkdownVectorSync, MarkdownVectorSyncResult

logger = logging.getLogger(__name__)


class MemoryOptimizerBusy(RuntimeError):
    pass


@dataclass(frozen=True)
class TextResponse:
    content: str


class TextProvider(Protocol):
    async def chat(
        self,
        *,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        model: str,
        max_tokens: int,
    ) -> TextResponse: ...


@dataclass(frozen=True)
class MemoryOptimizerResult:
    user_id: int
    status: str
    pending_chars: int = 0
    memory_before_chars: int = 0
    memory_after_chars: int = 0
    self_updated: bool = False
    vector_parsed: int = 0
    vector_inserted: int = 0
    vector_skipped: int = 0
    vector_error: str = ""
    error: str = ""


class OpenAITextProvider:
    def __init__(self) -> None:
        self._client = AsyncOpenAI(
            api_key=settings.DEEPSEEK_API_KEY,
            base_url=settings.DEEPSEEK_BASE_URL,
            timeout=90.0,
        )

    async def chat(
        self,
        *,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        model: str,
        max_tokens: int,
    ) -> TextResponse:
        resp = await self._client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=0.1,
        )
        return TextResponse(content=(resp.choices[0].message.content or "").strip())


_MERGE_SYSTEM = (
    "你是一个用户长期记忆整理器。你的工作不是概括对话，"
    "而是把 PENDING.md 中值得长期保留的事实合并进紧凑的 MEMORY.md。"
)

_MERGE_PROMPT = """\
今日日期：{today}

请把「待合并事实」合并到「现有长期记忆」中，输出新的完整 MEMORY.md。

规则：
- 只保留 6 个月后仍会影响回复质量的长期信息。
- 合并重复事实，只保留最终版本。
- correction 要直接反映到最终内容，不保留旧值到新值的流水账。
- 不要保留短期状态、临时情绪、一次性任务过程、过期数字。
- 不要生成工具调用规则、pipeline 规则或 eval 规则。
- PENDING 中的 tag 只辅助分类，不要原样输出 tag。
- 输出必须紧凑，后续会全文注入 prompt。

推荐输出结构：
# Long-term Memory

## User Facts
- ...

## User Preferences
- ...

## Requested Long-term Memory
- ...

## Assistant Operation Context
- ...

没有内容的 section 可以省略。不要输出代码块，不要解释。

现有长期记忆：
{memory}

待合并事实：
{pending}
"""

_SELF_SYSTEM = (
    "你是一个助手自我模型整理器。SELF.md 不是用户档案，"
    "只描述助手定位、对当前用户的稳定理解和长期关系边界。"
)

_SELF_PROMPT = """\
请根据当前 SELF.md 和待合并事实，输出新的完整 SELF.md。

规则：
- SELF.md 不是 MEMORY.md，不要写用户事实清单、偏好清单、账号、设备、动态事件。
- 只有当待合并事实确实改变助手对用户的长期理解或互动关系时，才少量吸收。
- 不要新增流水账、历史记录、工具规范、SOP。
- 如果没有足够高价值的新信息，基本保留当前 SELF.md。
- 不要输出代码块，不要解释。

推荐输出结构：
# Self Model

## Persona
- ...

## Understanding Of User
- ...

## Relationship
- ...

当前 SELF.md：
{self_content}

待合并事实：
{pending}
"""


class MemoryOptimizer:
    def __init__(
        self,
        memory: MarkdownMemoryStore,
        provider: TextProvider,
        model: str,
        *,
        max_tokens: int = 4096,
        vector_sync: MarkdownVectorSync | None = None,
    ) -> None:
        self._memory = memory
        self._provider = provider
        self._model = model
        self._max_tokens = max_tokens
        self._vector_sync = vector_sync
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._lock.locked()

    async def optimize(self, user_id: int) -> MemoryOptimizerResult:
        if self._lock.locked():
            raise MemoryOptimizerBusy("memory optimizer 正在运行")
        async with self._lock:
            return await self._optimize(user_id)

    async def _optimize(self, user_id: int) -> MemoryOptimizerResult:
        pending = self._memory.snapshot_pending(user_id)
        current_memory = self._memory.read_long_term(user_id).strip()
        current_self = self._memory.read_self(user_id).strip()

        if not pending:
            self._memory.commit_pending_snapshot(user_id)
            if _effective_markdown_content(current_memory) and self._vector_sync is not None:
                vector_result, vector_error = await self._sync_vector_safe(user_id)
                return MemoryOptimizerResult(
                    user_id=user_id,
                    status="synced",
                    memory_before_chars=len(current_memory),
                    memory_after_chars=len(current_memory),
                    vector_parsed=vector_result.parsed_count,
                    vector_inserted=vector_result.inserted_count,
                    vector_skipped=vector_result.skipped_count,
                    vector_error=vector_error,
                )
            return MemoryOptimizerResult(user_id=user_id, status="skipped")

        try:
            merged_memory = await self._merge_memory(current_memory, pending)
            if not merged_memory:
                self._memory.rollback_pending_snapshot(user_id)
                return MemoryOptimizerResult(
                    user_id=user_id,
                    status="rolled_back",
                    pending_chars=len(pending),
                    memory_before_chars=len(current_memory),
                    error="empty_memory_merge",
                )

            if current_memory:
                self._memory.backup_long_term(user_id)
            self._memory.write_long_term(user_id, merged_memory)

            self_updated = False
            if pending:
                updated_self = await self._update_self(current_self, pending)
                if updated_self:
                    self._memory.write_self(user_id, updated_self)
                    self_updated = True

            self._memory.commit_pending_snapshot(user_id)
            vector_result, vector_error = await self._sync_vector_safe(user_id)
            return MemoryOptimizerResult(
                user_id=user_id,
                status="merged",
                pending_chars=len(pending),
                memory_before_chars=len(current_memory),
                memory_after_chars=len(merged_memory),
                self_updated=self_updated,
                vector_parsed=vector_result.parsed_count,
                vector_inserted=vector_result.inserted_count,
                vector_skipped=vector_result.skipped_count,
                vector_error=vector_error,
            )
        except Exception as exc:
            logger.exception("[memory_optimizer] optimize failed user_id=%s", user_id)
            self._memory.rollback_pending_snapshot(user_id)
            return MemoryOptimizerResult(
                user_id=user_id,
                status="rolled_back",
                pending_chars=len(pending),
                memory_before_chars=len(current_memory),
                error=str(exc),
            )

    async def _sync_vector_safe(self, user_id: int) -> tuple[MarkdownVectorSyncResult, str]:
        if self._vector_sync is None:
            return MarkdownVectorSyncResult(user_id=user_id), ""
        try:
            return (
                await self._vector_sync.sync_user(markdown=self._memory, user_id=user_id),
                "",
            )
        except Exception as exc:
            logger.exception("[memory_optimizer] vector sync failed user_id=%s", user_id)
            return MarkdownVectorSyncResult(user_id=user_id), str(exc)

    async def _merge_memory(self, memory: str, pending: str) -> str:
        prompt = _MERGE_PROMPT.format(
            today=datetime.now().strftime("%Y-%m-%d"),
            memory=memory if _effective_markdown_content(memory) else "（空）",
            pending=pending or "（无新内容）",
        )
        return await self._request_text_response(
            system_content=_MERGE_SYSTEM,
            user_content=prompt,
            max_tokens=self._max_tokens,
        )

    async def _update_self(self, self_content: str, pending: str) -> str:
        prompt = _SELF_PROMPT.format(
            self_content=self_content or "# Self Model\n\n",
            pending=pending or "（无新内容）",
        )
        updated = await self._request_text_response(
            system_content=_SELF_SYSTEM,
            user_content=prompt,
            max_tokens=2048,
        )
        if not _is_valid_self_model(updated, pending):
            logger.warning("[memory_optimizer] SELF.md update rejected by guard")
            return ""
        return updated

    async def _request_text_response(
        self,
        *,
        system_content: str,
        user_content: str,
        max_tokens: int,
    ) -> str:
        resp = await self._provider.chat(
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            tools=[],
            model=self._model,
            max_tokens=max_tokens,
        )
        return _strip_code_fence(resp.content or "").strip()


def _effective_markdown_content(text: str) -> str:
    lines: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in {"# Long-term Memory", "# Self Model"}:
            continue
        lines.append(stripped)
    return "\n".join(lines).strip()


def _strip_code_fence(text: str) -> str:
    value = (text or "").strip()
    if not value.startswith("```"):
        return value
    value = value.split("\n", 1)[-1]
    return value.rsplit("```", 1)[0].strip()


def _is_valid_self_model(content: str, pending: str) -> bool:
    text = (content or "").strip()
    if not text:
        return False
    if "## User Facts" in text or "## User Preferences" in text:
        return False
    if "## 用户事实" in text or "## 用户偏好" in text:
        return False
    if "[identity]" in text or "[preference]" in text:
        return False
    pending_facts = _pending_fact_texts(pending)
    for fact in pending_facts:
        if len(fact) >= 8 and fact in text:
            return False
    return True


def _pending_fact_texts(pending: str) -> list[str]:
    facts: list[str] = []
    for line in (pending or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("- ["):
            continue
        _, _, fact = stripped.partition("] ")
        fact = fact.strip()
        if fact:
            facts.append(fact)
    return facts


def _iter_user_ids(store: MarkdownMemoryStore) -> list[int]:
    users_dir = store.root / "users"
    if not users_dir.exists():
        return []
    ids: list[int] = []
    for path in users_dir.iterdir():
        if path.is_dir() and path.name.isdigit():
            ids.append(int(path.name))
    return sorted(ids)


async def _run_cli(args: argparse.Namespace) -> int:
    root = Path(args.markdown_root) if args.markdown_root else default_markdown_memory_root()
    store = MarkdownMemoryStore(root)
    vector_sync = _build_vector_sync() if args.sync_vector or args.sync_vector_only else None
    optimizer = None
    if not args.sync_vector_only:
        optimizer = MemoryOptimizer(
            store,
            OpenAITextProvider(),
            args.model or settings.LLM_MODEL,
            max_tokens=args.max_tokens,
            vector_sync=vector_sync,
        )
    user_ids = _iter_user_ids(store) if args.all_users else [int(args.user_id)]
    if not user_ids:
        print("No markdown memory users found.")
        return 0
    for user_id in user_ids:
        if args.sync_vector_only:
            if vector_sync is None:
                raise RuntimeError("vector sync is not initialized")
            result = await vector_sync.sync_user(markdown=store, user_id=user_id)
            print(
                f"user={result.user_id} status=synced "
                f"vector={result.parsed_count} parsed/"
                f"{result.inserted_count} inserted/"
                f"{result.skipped_count} skipped"
            )
            continue
        if optimizer is None:
            raise RuntimeError("memory optimizer is not initialized")
        result = await optimizer.optimize(user_id)
        print(
            f"user={result.user_id} status={result.status} "
            f"pending_chars={result.pending_chars} "
            f"memory={result.memory_before_chars}->{result.memory_after_chars} "
            f"self_updated={result.self_updated}"
            f" vector={result.vector_parsed} parsed/"
            f"{result.vector_inserted} inserted/"
            f"{result.vector_skipped} skipped"
            + (f" vector_error={result.vector_error}" if result.vector_error else "")
            + (f" error={result.error}" if result.error else "")
        )
    return 0


def _build_vector_sync() -> MarkdownVectorSync:
    from memory.embedder import Embedder
    from memory.store import MemoryStore
    from persistence.database import init_db

    init_db()
    return MarkdownVectorSync(MemoryStore(Embedder()))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Merge PENDING.md into MEMORY.md/SELF.md.")
    parser.add_argument("--user-id", type=int, help="Optimize one user workspace.")
    parser.add_argument("--all-users", action="store_true", help="Optimize every numeric user workspace.")
    parser.add_argument("--markdown-root", help="Override markdown memory root.")
    parser.add_argument("--model", help="Override LLM model.")
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument(
        "--sync-vector",
        action="store_true",
        help="After a successful merge, sync MEMORY.md bullets into MemoryStore.",
    )
    parser.add_argument(
        "--sync-vector-only",
        action="store_true",
        help="Only sync existing MEMORY.md bullets into MemoryStore; do not call the LLM.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.all_users and args.user_id is None:
        parser.error("--user-id is required unless --all-users is set")
    return asyncio.run(_run_cli(args))


if __name__ == "__main__":
    raise SystemExit(main())
