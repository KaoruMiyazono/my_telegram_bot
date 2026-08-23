"""Persistent session context compaction for the production provider path.

The SQLite session transcript remains authoritative.  This module projects an
immutable summary generation plus a recent raw tail into the model request; it
never rewrites or deletes ``conversation_sessions.messages_json``.
"""

from __future__ import annotations

import json
import math
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Sequence

from agent.runtime.context_budget import estimate_context_tokens
from persistence.session_compaction_store import (
    SessionCompaction,
    SessionCompactionStore,
    get_session_compaction_store,
)


SUMMARY_FORMAT_VERSION = 1
SUMMARY_HEADINGS = (
    "## Goal",
    "## Constraints & Preferences",
    "## Progress",
    "### Done",
    "### In Progress",
    "### Blocked",
    "## Key Decisions",
    "## Next Steps",
    "## Critical Context",
)
SUMMARY_PROMPT = """更新当前长对话的上下文压缩摘要。

摘要只替代已经完成的旧Session交互；原始messages仍由数据库完整保留。只记录输入里明确出现的事实，不猜测，不把计划写成已完成。

必须严格使用以下标题，不得增加或省略标题：
## Goal
## Constraints & Preferences
## Progress
### Done
### In Progress
### Blocked
## Key Decisions
## Next Steps
## Critical Context

保留用户约束与偏好、文件路径、符号、命令、错误、数值、工具外部效果和验证结果。省略寒暄、重复探索、tool_call_id和协议噪音。只输出摘要正文。"""

CompactionTrigger = Literal["soft_limit", "context_overflow"]
SummaryBuilder = Callable[
    [str, int],
    Awaitable[tuple[str, dict[str, Any] | None]],
]


class SessionContextCompactionError(RuntimeError):
    """The assembled payload cannot be compacted without breaking invariants."""


@dataclass(frozen=True)
class SessionCompactionConfig:
    context_window: int = 64_000
    output_reserve: int = 4_096
    soft_limit_ratio: float = 0.74
    keep_recent_tokens: int = 20_000
    summary_max_tokens: int = 8_192

    def __post_init__(self) -> None:
        if self.context_window <= 0:
            raise ValueError("context_window必须为正整数")
        if self.output_reserve < 0 or self.output_reserve >= self.context_window:
            raise ValueError("output_reserve必须位于[0, context_window)内")
        if not 0 < self.soft_limit_ratio < 1:
            raise ValueError("soft_limit_ratio必须位于(0, 1)内")
        if self.keep_recent_tokens <= 0:
            raise ValueError("keep_recent_tokens必须为正整数")
        if self.summary_max_tokens <= 0:
            raise ValueError("summary_max_tokens必须为正整数")

    @property
    def soft_limit_tokens(self) -> int:
        return math.floor(self.context_window * self.soft_limit_ratio)

    @property
    def hard_input_tokens(self) -> int:
        return self.context_window - self.output_reserve


@dataclass(frozen=True)
class CommittedInteraction:
    source_from_seq: int
    consolidated_through_seq: int
    source_message_ids: tuple[str, ...]
    messages: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class PreparedSessionContext:
    estimated_tokens: int
    compacted: bool
    checkpoint: SessionCompaction | None
    trace: dict[str, Any]


class SessionContextCompactor:
    """Apply one Context Gate before each business provider request."""

    def __init__(
        self,
        *,
        user_id: int,
        chat_id: int,
        session_messages: Sequence[dict[str, Any]],
        model: str,
        summary_builder: SummaryBuilder,
        config: SessionCompactionConfig,
        store: SessionCompactionStore | None = None,
    ) -> None:
        self.user_id = int(user_id)
        self.chat_id = int(chat_id)
        self.model = str(model)
        self.config = config
        self._summary_builder = summary_builder
        self._store = store or get_session_compaction_store()
        self._raw_session_messages = deepcopy(list(session_messages))
        # A brand-new session cannot have a compaction generation. Avoid a
        # needless database read on its first turn (also keeps unit use simple).
        self._active = (
            self._store.get_active(self.user_id, self.chat_id)
            if self._raw_session_messages
            else None
        )
        self._projection_initialized = False
        self._prefix_count = 1
        self._history_end = 1 + len(self._raw_session_messages)

    async def prepare(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]],
        trigger: CompactionTrigger = "soft_limit",
        force: bool = False,
    ) -> PreparedSessionContext:
        """Project an active checkpoint and compact when soft/hard budget is hit."""

        raw_tokens = estimate_context_tokens(messages, tools)
        self._initialize_projection(messages)
        estimated = estimate_context_tokens(messages, tools)
        boundary_hit = (
            estimated >= self.config.soft_limit_tokens
            or estimated >= self.config.hard_input_tokens
        )
        if not force and not boundary_hit:
            active_generation = self._active.generation if self._active else 0
            projected = active_generation > 0 and estimated < raw_tokens
            return PreparedSessionContext(
                estimated_tokens=estimated,
                compacted=projected,
                checkpoint=None,
                trace=self._trace(
                    trigger="active_checkpoint" if projected else "within_budget",
                    compacted=projected,
                    tokens_before=raw_tokens,
                    tokens_after=estimated,
                    generation=active_generation,
                    selected_units=0,
                    retained_units=0,
                ),
            )

        history_items = self._projected_history_items(messages)
        units = _build_interactions(
            history_items,
            user_id=self.user_id,
            chat_id=self.chat_id,
        )
        selected, retained = _select_interactions(
            units,
            keep_recent_tokens=self.config.keep_recent_tokens,
        )
        summary_input = _summary_input(
            previous_summary=self._active.summary if self._active else "",
            selected=selected,
        )
        summary, usage = await self._summary_builder(
            summary_input,
            self.config.summary_max_tokens,
        )
        _validate_summary(summary)

        parent = self._active.generation if self._active else self._store.get_head(
            self.user_id,
            self.chat_id,
        )
        generation = parent + 1
        source_ids = list(self._active.source_message_ids if self._active else ())
        for unit in selected:
            for message_id in unit.source_message_ids:
                if message_id not in source_ids:
                    source_ids.append(message_id)
        source_from_seq = (
            self._active.source_from_seq
            if self._active is not None
            else selected[0].source_from_seq
        )
        retained_tail = tuple(
            {
                "seq": seq,
                "message": deepcopy(message),
            }
            for unit in retained
            for seq, message in _unit_items(unit)
        )
        tail = deepcopy(messages[self._history_end :])
        rebuilt = [
            deepcopy(messages[0]),
            _compaction_message(
                summary,
                generation=generation,
                source_ref=_source_ref(self.user_id, self.chat_id, generation),
            ),
            *[deepcopy(item["message"]) for item in retained_tail],
            *tail,
        ]
        after = estimate_context_tokens(rebuilt, tools)
        if (
            after >= self.config.soft_limit_tokens
            or after >= self.config.hard_input_tokens
        ):
            raise SessionContextCompactionError(
                "session_compaction_insufficient "
                f"estimated={after} soft={self.config.soft_limit_tokens} "
                f"hard={self.config.hard_input_tokens}"
            )

        checkpoint = SessionCompaction(
            user_id=self.user_id,
            chat_id=self.chat_id,
            generation=generation,
            parent_generation=parent,
            summary=summary,
            source_ref=_source_ref(self.user_id, self.chat_id, generation),
            source_from_seq=source_from_seq,
            consolidated_through_seq=selected[-1].consolidated_through_seq,
            source_message_ids=tuple(source_ids),
            retained_tail=retained_tail,
            trigger=trigger,
            context_window=self.config.context_window,
            soft_limit_tokens=self.config.soft_limit_tokens,
            hard_input_tokens=self.config.hard_input_tokens,
            keep_recent_tokens=self.config.keep_recent_tokens,
            estimated_tokens_before=estimated,
            estimated_tokens_after=after,
            model=self.model,
            summary_usage=usage,
        )
        self._store.commit(
            checkpoint,
            expected_parent_generation=parent,
        )
        self._active = checkpoint
        messages[:] = rebuilt
        self._history_end = 2 + len(retained_tail)
        return PreparedSessionContext(
            estimated_tokens=after,
            compacted=True,
            checkpoint=checkpoint,
            trace=self._trace(
                trigger=trigger,
                compacted=True,
                tokens_before=estimated,
                tokens_after=after,
                generation=generation,
                selected_units=len(selected),
                retained_units=len(retained),
            ),
        )

    def _initialize_projection(self, messages: list[dict[str, Any]]) -> None:
        if self._projection_initialized:
            return
        if not messages:
            raise SessionContextCompactionError("provider payload不能为空")
        self._prefix_count = 1 if messages[0].get("role") == "system" else 0
        if self._prefix_count == 0 and self._raw_session_messages:
            raise SessionContextCompactionError("provider payload缺少受保护System消息")
        expected = self._raw_session_messages
        start = self._prefix_count
        if messages[start : start + len(expected)] != expected:
            raise SessionContextCompactionError(
                "provider payload与Session原始历史边界不一致"
            )
        self._projection_initialized = True
        if self._active is None:
            self._history_end = start + len(expected)
            return

        tail_items = _active_tail_with_new_messages(
            self._active,
            self._raw_session_messages,
        )
        current = deepcopy(messages[start + len(expected) :])
        messages[:] = [
            deepcopy(messages[0]),
            _compaction_message(
                self._active.summary,
                generation=self._active.generation,
                source_ref=self._active.source_ref,
            ),
            *[deepcopy(item["message"]) for item in tail_items],
            *current,
        ]
        self._history_end = 2 + len(tail_items)

    def _projected_history_items(
        self,
        messages: Sequence[dict[str, Any]],
    ) -> list[tuple[int, dict[str, Any]]]:
        if self._active is None:
            return [
                (seq, deepcopy(message))
                for seq, message in enumerate(self._raw_session_messages)
            ]
        return [
            (int(item["seq"]), deepcopy(item["message"]))
            for item in _active_tail_with_new_messages(
                self._active,
                self._raw_session_messages,
            )
        ]

    def _trace(
        self,
        *,
        trigger: str,
        compacted: bool,
        tokens_before: int,
        tokens_after: int,
        generation: int,
        selected_units: int,
        retained_units: int,
    ) -> dict[str, Any]:
        return {
            "level": "ledger" if compacted else "L0",
            "strategy": "session_compaction_ledger",
            "trigger": trigger,
            "compacted": compacted,
            "generation": generation,
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "context_window": self.config.context_window,
            "soft_limit_tokens": self.config.soft_limit_tokens,
            "hard_input_tokens": self.config.hard_input_tokens,
            "keep_recent_tokens": self.config.keep_recent_tokens,
            "selected_units": selected_units,
            "retained_units": retained_units,
            "source_ref": (
                _source_ref(self.user_id, self.chat_id, generation)
                if generation > 0
                else ""
            ),
        }


def _build_interactions(
    items: Sequence[tuple[int, dict[str, Any]]],
    *,
    user_id: int,
    chat_id: int,
) -> list[CommittedInteraction]:
    units: list[CommittedInteraction] = []
    current: list[tuple[int, dict[str, Any]]] = []
    for seq, message in items:
        role = str(message.get("role") or "")
        if role == "user":
            if current:
                raise SessionContextCompactionError(
                    "session_compaction_unclosed_interaction"
                )
            current = [(seq, message)]
            continue
        if not current:
            raise SessionContextCompactionError(
                "session_compaction_history_must_start_with_user"
            )
        current.append((seq, message))
        if role == "assistant":
            ids = tuple(
                f"session:{user_id}:{chat_id}#msg:{item_seq}"
                for item_seq, _ in current
            )
            units.append(
                CommittedInteraction(
                    source_from_seq=current[0][0],
                    consolidated_through_seq=current[-1][0],
                    source_message_ids=ids,
                    messages=tuple(deepcopy(item) for _, item in current),
                )
            )
            current = []
    if current:
        raise SessionContextCompactionError("session_compaction_unclosed_interaction")
    return units


def _select_interactions(
    units: Sequence[CommittedInteraction],
    *,
    keep_recent_tokens: int,
) -> tuple[list[CommittedInteraction], list[CommittedInteraction]]:
    if len(units) < 2:
        raise SessionContextCompactionError("session_compaction_no_closed_prefix")
    retained: list[CommittedInteraction] = []
    retained_tokens = 0
    for unit in reversed(units):
        retained.insert(0, unit)
        retained_tokens += estimate_context_tokens(unit.messages)
        if retained_tokens >= keep_recent_tokens:
            break
    if retained_tokens < keep_recent_tokens:
        raise SessionContextCompactionError(
            "session_compaction_no_valid_cut_before_keep_recent_target"
        )
    cut = len(units) - len(retained)
    if cut <= 0:
        raise SessionContextCompactionError("session_compaction_no_closed_prefix")
    return list(units[:cut]), retained


def _summary_input(
    *,
    previous_summary: str,
    selected: Sequence[CommittedInteraction],
) -> str:
    sections = [SUMMARY_PROMPT]
    if previous_summary:
        sections.extend(["\n\n[Previous compaction summary]\n", previous_summary])
    sections.append("\n\n[Closed history to consolidate]\n")
    for unit in selected:
        for message in unit.messages:
            sections.append(json.dumps(message, ensure_ascii=False, default=str))
            sections.append("\n")
    return "".join(sections)


def _validate_summary(summary: str) -> None:
    text = str(summary or "").strip()
    positions = [text.find(heading) for heading in SUMMARY_HEADINGS]
    if not text or any(position < 0 for position in positions):
        raise SessionContextCompactionError("session_compaction_summary_heading_missing")
    if positions != sorted(positions):
        raise SessionContextCompactionError("session_compaction_summary_heading_order_invalid")


def _compaction_message(
    summary: str,
    *,
    generation: int,
    source_ref: str,
) -> dict[str, Any]:
    return {
        "role": "system",
        "content": (
            "<session-context-compaction>\n"
            f"version={SUMMARY_FORMAT_VERSION}; generation={generation}; "
            f"source_ref={source_ref}\n"
            f"{summary.strip()}\n"
            "</session-context-compaction>"
        ),
    }


def _active_tail_with_new_messages(
    active: SessionCompaction,
    raw_messages: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    tail = [deepcopy(item) for item in active.retained_tail]
    last_seq = (
        max(int(item["seq"]) for item in tail)
        if tail
        else active.consolidated_through_seq
    )
    tail.extend(
        {
            "seq": seq,
            "message": deepcopy(raw_messages[seq]),
        }
        for seq in range(last_seq + 1, len(raw_messages))
    )
    return tail


def _unit_items(unit: CommittedInteraction) -> list[tuple[int, dict[str, Any]]]:
    return [
        (unit.source_from_seq + offset, deepcopy(message))
        for offset, message in enumerate(unit.messages)
    ]


def _source_ref(user_id: int, chat_id: int, generation: int) -> str:
    return f"session:{user_id}:{chat_id}#compaction:{generation}"
