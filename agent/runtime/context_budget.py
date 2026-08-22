"""Non-destructive context budgeting for model requests.

The session and database remain authoritative.  This module only creates a
temporary projection for one LLM call, following Akashic ADR-0002.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Iterable, Sequence

from agent.prompting import PromptSectionRender


_URL_RE = re.compile(r"https?://[^\s\]\[<>()\"']+")
_IMPORTANT_KEYS = {
    "ok",
    "status",
    "error",
    "code",
    "message",
    "url",
    "urls",
    "title",
    "source_ref",
    "source_refs",
    "tool_name",
    "matched_count",
    "query",
}


class ContextLevel(IntEnum):
    COMPLETE = 0
    COMPRESS_TOOL_RESULTS = 1
    SUMMARIZE_OLD_HISTORY = 2
    PRUNE_MEMORY_AND_TOOLS = 3
    MINIMAL_SAFE = 4


class ContextBudgetExceeded(RuntimeError):
    """The protected system core and current request cannot fit safely."""


@dataclass(frozen=True)
class ContextBudgetConfig:
    context_window: int = 64_000
    output_reserve: int = 4_096
    recent_history_messages: int = 6
    tool_result_chars_l1: int = 2_000
    tool_result_chars_l3: int = 800
    tool_result_chars_l4: int = 480
    summary_chars_l2: int = 2_400
    summary_chars_l3: int = 900

    def __post_init__(self) -> None:
        if self.context_window <= 0:
            raise ValueError("context_window must be positive")
        if self.output_reserve < 0:
            raise ValueError("output_reserve cannot be negative")
        if self.output_reserve >= self.context_window:
            raise ValueError("output_reserve must be smaller than context_window")
        if self.recent_history_messages < 0:
            raise ValueError("recent_history_messages cannot be negative")


@dataclass(frozen=True)
class ContextChange:
    action: str
    block: str
    tokens_before: int
    tokens_after: int
    digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "block": self.block,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class ContextBudgetTrace:
    level: ContextLevel
    reason: str
    tokens_before: int
    tokens_after: int
    context_window: int
    output_reserve: int
    input_budget: int
    system_tokens: int
    current_turn_tokens: int
    variable_budget: int
    evidence_integrity_affected: bool
    stable_system_digest: str
    changes: tuple[ContextChange, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": f"L{int(self.level)}",
            "reason": self.reason,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "context_window": self.context_window,
            "output_reserve": self.output_reserve,
            "input_budget": self.input_budget,
            "system_tokens": self.system_tokens,
            "current_turn_tokens": self.current_turn_tokens,
            "variable_budget": self.variable_budget,
            "evidence_integrity_affected": self.evidence_integrity_affected,
            "stable_system_digest": self.stable_system_digest,
            "changes": [change.to_dict() for change in self.changes],
        }


@dataclass(frozen=True)
class ContextProjection:
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    trace: ContextBudgetTrace


class ContextBudget:
    """Create the smallest required model view without mutating source data."""

    def __init__(self, config: ContextBudgetConfig | None = None) -> None:
        self.config = config or ContextBudgetConfig()

    def project(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] = (),
        prompt_sections: Sequence[PromptSectionRender] = (),
        source_ref: str = "",
        min_level: ContextLevel = ContextLevel.COMPLETE,
    ) -> ContextProjection:
        original_messages = deepcopy(list(messages))
        original_tools = deepcopy(list(tools))
        tokens_before = estimate_context_tokens(original_messages, original_tools)
        input_budget = self.config.context_window - self.config.output_reserve
        stable_content = _stable_system_content(original_messages, prompt_sections)
        stable_digest = _digest(stable_content)

        candidates: list[tuple[ContextLevel, list[dict[str, Any]], list[dict[str, Any]], list[ContextChange], bool]] = []
        candidates.append((ContextLevel.COMPLETE, original_messages, original_tools, [], False))

        l1_messages, l1_changes = _compress_tool_results(
            original_messages,
            max_chars=self.config.tool_result_chars_l1,
            action="compress_tool_result",
        )
        candidates.append((ContextLevel.COMPRESS_TOOL_RESULTS, l1_messages, original_tools, l1_changes, False))

        l2_messages, l2_changes = _summarize_old_history(
            l1_messages,
            keep_recent=self.config.recent_history_messages,
            max_chars=self.config.summary_chars_l2,
            source_ref=source_ref,
        )
        candidates.append((ContextLevel.SUMMARIZE_OLD_HISTORY, l2_messages, original_tools, [*l1_changes, *l2_changes], False))

        l3_messages, l3_changes = _build_l3_projection(
            l2_messages,
            prompt_sections=prompt_sections,
            query=_current_user_content(original_messages),
            tool_max_chars=self.config.tool_result_chars_l3,
            summary_max_chars=self.config.summary_chars_l3,
        )
        candidates.append((ContextLevel.PRUNE_MEMORY_AND_TOOLS, l3_messages, original_tools, [*l1_changes, *l2_changes, *l3_changes], True))

        l4_messages, l4_changes = _build_minimal_projection(
            original_messages,
            prompt_sections=prompt_sections,
            tool_max_chars=self.config.tool_result_chars_l4,
        )
        candidates.append((ContextLevel.MINIMAL_SAFE, l4_messages, [], l4_changes, True))

        for level, projected_messages, projected_tools, changes, evidence_affected in candidates:
            if level < min_level:
                continue
            tokens_after = estimate_context_tokens(projected_messages, projected_tools)
            if tokens_after > input_budget:
                continue
            system_tokens = _system_tokens(projected_messages)
            current_turn_tokens = estimate_message_tokens(_current_user_message(projected_messages))
            variable_budget = max(0, input_budget - system_tokens - current_turn_tokens)
            reason = "within_budget" if level == ContextLevel.COMPLETE else "context_budget_exceeded"
            return ContextProjection(
                messages=projected_messages,
                tools=projected_tools,
                trace=ContextBudgetTrace(
                    level=level,
                    reason=reason,
                    tokens_before=tokens_before,
                    tokens_after=tokens_after,
                    context_window=self.config.context_window,
                    output_reserve=self.config.output_reserve,
                    input_budget=input_budget,
                    system_tokens=system_tokens,
                    current_turn_tokens=current_turn_tokens,
                    variable_budget=variable_budget,
                    evidence_integrity_affected=evidence_affected,
                    stable_system_digest=stable_digest,
                    changes=tuple(changes),
                ),
            )

        raise ContextBudgetExceeded(
            "Protected system instructions and current request exceed the configured input budget"
        )


def estimate_context_tokens(
    messages: Iterable[dict[str, Any]],
    tools: Iterable[dict[str, Any]] = (),
) -> int:
    """Deterministic conservative estimate used before provider calls."""

    payload_chars = 0
    message_count = 0
    for message in messages:
        payload_chars += len(json.dumps(message, ensure_ascii=False, default=str))
        message_count += 1
    tool_payload = json.dumps(list(tools), ensure_ascii=False, default=str)
    return max(1, (payload_chars + len(tool_payload) + 2) // 3 + message_count * 4)


def estimate_message_tokens(message: dict[str, Any] | None) -> int:
    if not message:
        return 0
    return estimate_context_tokens([message], [])


def _compress_tool_results(
    messages: Sequence[dict[str, Any]],
    *,
    max_chars: int,
    action: str,
) -> tuple[list[dict[str, Any]], list[ContextChange]]:
    projected = deepcopy(list(messages))
    changes: list[ContextChange] = []
    for index, message in enumerate(projected):
        if message.get("role") != "tool":
            continue
        content = str(message.get("content") or "")
        if len(content) <= max_chars:
            continue
        compressed = _compact_tool_content(content, max_chars=max_chars)
        message["content"] = compressed
        changes.append(
            ContextChange(
                action=action,
                block=f"tool:{message.get('tool_call_id') or index}",
                tokens_before=max(1, len(content) // 3),
                tokens_after=max(1, len(compressed) // 3),
                digest=_digest(content),
            )
        )
    return projected, changes


def _compact_tool_content(content: str, *, max_chars: int) -> str:
    digest = _digest(content)
    urls = list(dict.fromkeys(_URL_RE.findall(content)))[:10]
    preserved: dict[str, Any] = {}
    original_chars = len(content)
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        previous_preserved = payload.get("preserved")
        if payload.get("_context_compacted") is True and isinstance(
            previous_preserved,
            dict,
        ):
            preserved.update(
                {
                    str(key): _bounded_json_value(value)
                    for key, value in previous_preserved.items()
                }
            )
            previous_urls = payload.get("urls")
            if isinstance(previous_urls, list):
                urls = list(
                    dict.fromkeys(
                        [*urls, *(str(url) for url in previous_urls if url)]
                    )
                )[:10]
            previous_chars = payload.get("original_chars")
            if isinstance(previous_chars, int) and previous_chars > 0:
                original_chars = previous_chars
            previous_digest = str(payload.get("sha256") or "").strip()
            if previous_digest:
                digest = previous_digest
        for key, value in payload.items():
            if key in _IMPORTANT_KEYS:
                preserved[key] = _bounded_json_value(value)
        error = payload.get("error")
        if error is not None:
            preserved["error"] = _bounded_json_value(error)
    compacted: dict[str, Any] = {
        "_context_compacted": True,
        "original_chars": original_chars,
        "sha256": digest,
        "preserved": preserved,
    }
    if urls:
        compacted["urls"] = urls
    important_lines = [
        line.strip()
        for line in content.splitlines()
        if "error" in line.lower() or "错误" in line or "source_ref" in line
    ]
    base_size = len(json.dumps(compacted, ensure_ascii=False, separators=(",", ":")))
    excerpt_budget = max(0, max_chars - base_size - 24)
    excerpt_source = "\n".join(important_lines[:8]) or content
    if excerpt_budget:
        compacted["key_excerpt"] = excerpt_source[:excerpt_budget]
    rendered = json.dumps(compacted, ensure_ascii=False, separators=(",", ":"), default=str)
    return rendered


def _bounded_json_value(value: Any, *, max_chars: int = 600) -> Any:
    if isinstance(value, str):
        return value if len(value) <= max_chars else value[:max_chars] + "…"
    if isinstance(value, dict):
        return {
            str(key): _bounded_json_value(item, max_chars=max_chars)
            for key, item in list(value.items())[:20]
        }
    if isinstance(value, list):
        return [_bounded_json_value(item, max_chars=max_chars) for item in value[:20]]
    return value


def _summarize_old_history(
    messages: Sequence[dict[str, Any]],
    *,
    keep_recent: int,
    max_chars: int,
    source_ref: str,
) -> tuple[list[dict[str, Any]], list[ContextChange]]:
    projected = deepcopy(list(messages))
    current_index = _last_user_index(projected)
    if current_index <= 1:
        return projected, []
    history = projected[1:current_index]
    split = max(0, len(history) - keep_recent)
    older = history[:split]
    retained = history[split:]
    if not older:
        return projected, []
    summary = _history_summary(older, max_chars=max_chars, source_ref=source_ref)
    summary_message = {"role": "system", "content": summary}
    result = [projected[0], summary_message, *retained, *projected[current_index:]]
    change = ContextChange(
        action="summarize_old_history",
        block=f"history:0-{split - 1}",
        tokens_before=estimate_context_tokens(older),
        tokens_after=estimate_message_tokens(summary_message),
        digest=_digest(json.dumps(older, ensure_ascii=False, default=str)),
    )
    return result, [change]


def _history_summary(
    messages: Sequence[dict[str, Any]],
    *,
    max_chars: int,
    source_ref: str,
) -> str:
    raw = json.dumps(list(messages), ensure_ascii=False, default=str, sort_keys=True)
    version = _digest(raw)
    lines = [
        f"# Conversation Summary v1/{version}",
        "This is a temporary model projection; the original messages remain authoritative.",
    ]
    if source_ref:
        lines.append(f"Original evidence: {source_ref} (use fetch_messages to retrieve it).")
    for index, message in enumerate(messages):
        role = str(message.get("role") or "unknown")
        content = str(message.get("content") or "").replace("\n", " ").strip()
        if content:
            lines.append(f"- [{index}:{role}] {content[:260]}")
    return "\n".join(lines)[:max_chars]


def _build_l3_projection(
    messages: Sequence[dict[str, Any]],
    *,
    prompt_sections: Sequence[PromptSectionRender],
    query: str,
    tool_max_chars: int,
    summary_max_chars: int,
) -> tuple[list[dict[str, Any]], list[ContextChange]]:
    projected = deepcopy(list(messages))
    changes: list[ContextChange] = []
    if projected and projected[0].get("role") == "system" and prompt_sections:
        old = str(projected[0].get("content") or "")
        valid_sections = [
            section for section in prompt_sections
            if isinstance(section, PromptSectionRender)
        ]
        static_sections = [section for section in valid_sections if section.is_static]
        dynamic_sections = [section for section in valid_sections if not section.is_static]
        ranked = sorted(dynamic_sections, key=lambda section: _section_score(section.content, query), reverse=True)
        selected = [section for section in ranked if _section_score(section.content, query) > 0][:2]
        content = _join_sections([*static_sections, *selected])
        projected[0]["content"] = content
        if content != old:
            changes.append(
                ContextChange(
                    action="prune_dynamic_system_sections",
                    block="system:dynamic_memory",
                    tokens_before=max(1, len(old) // 3),
                    tokens_after=max(1, len(content) // 3),
                    digest=_digest(old),
                )
            )
    current_index = _last_user_index(projected)
    summary_indices = [
        index
        for index, message in enumerate(projected[:current_index])
        if message.get("role") == "system" and index > 0
    ]
    for index in summary_indices:
        content = str(projected[index].get("content") or "")
        if len(content) > summary_max_chars:
            projected[index]["content"] = content[:summary_max_chars]
            changes.append(
                ContextChange(
                    action="prune_history_summary",
                    block=f"message:{index}",
                    tokens_before=max(1, len(content) // 3),
                    tokens_after=max(1, summary_max_chars // 3),
                    digest=_digest(content),
                )
            )
    projected, tool_changes = _compress_tool_results(
        projected,
        max_chars=tool_max_chars,
        action="prune_tool_result",
    )
    changes.extend(tool_changes)
    return projected, changes


def _build_minimal_projection(
    messages: Sequence[dict[str, Any]],
    *,
    prompt_sections: Sequence[PromptSectionRender],
    tool_max_chars: int,
) -> tuple[list[dict[str, Any]], list[ContextChange]]:
    original = deepcopy(list(messages))
    if not original:
        return [], []
    static_content = _stable_system_content(original, prompt_sections)
    minimal: list[dict[str, Any]] = []
    if static_content:
        minimal.append({"role": "system", "content": static_content})
    current_index = _last_user_index(original)
    if current_index >= 0:
        minimal.append(original[current_index])
    current_tail = original[current_index + 1 :] if current_index >= 0 else []
    for batch in _tool_batches(current_tail):
        if _batch_is_necessary_evidence(batch):
            minimal.extend(batch)
    minimal, tool_changes = _compress_tool_results(
        minimal,
        max_chars=tool_max_chars,
        action="minimal_evidence_compression",
    )
    removed_tokens = max(0, estimate_context_tokens(original) - estimate_context_tokens(minimal))
    changes = [
        ContextChange(
            action="minimal_safe_projection",
            block="history:memory:optional_tools",
            tokens_before=estimate_context_tokens(original),
            tokens_after=estimate_context_tokens(minimal),
            digest=_digest(json.dumps(original, ensure_ascii=False, default=str)),
        )
    ] if removed_tokens else []
    changes.extend(tool_changes)
    return minimal, changes


def _tool_batches(messages: Sequence[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            if current:
                batches.append(current)
            current = [message]
        elif role == "tool" and current:
            current.append(message)
        elif current:
            batches.append(current)
            current = []
    if current:
        batches.append(current)
    return batches


def _batch_is_necessary_evidence(batch: Sequence[dict[str, Any]]) -> bool:
    text = json.dumps(list(batch), ensure_ascii=False, default=str).lower()
    return any(marker in text for marker in ("source_ref", "http://", "https://", '"error"', "错误"))


def _stable_system_content(
    messages: Sequence[dict[str, Any]],
    prompt_sections: Sequence[PromptSectionRender],
) -> str:
    static = [
        section for section in prompt_sections
        if isinstance(section, PromptSectionRender) and section.is_static
    ]
    if static:
        return _join_sections(static)
    if messages and messages[0].get("role") == "system":
        return str(messages[0].get("content") or "")
    return ""


def _join_sections(sections: Sequence[PromptSectionRender]) -> str:
    return "\n\n---\n\n".join(section.content for section in sections if section.content)


def _section_score(content: str, query: str) -> int:
    query_terms = set(re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}", query.lower()))
    lowered = content.lower()
    score = sum(1 for term in query_terms if term in lowered)
    if "source_ref" in lowered:
        score += 2
    return score


def _system_tokens(messages: Sequence[dict[str, Any]]) -> int:
    return sum(estimate_message_tokens(message) for message in messages if message.get("role") == "system")


def _last_user_index(messages: Sequence[dict[str, Any]]) -> int:
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return index
    return -1


def _current_user_message(messages: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    index = _last_user_index(messages)
    return messages[index] if index >= 0 else None


def _current_user_content(messages: Sequence[dict[str, Any]]) -> str:
    message = _current_user_message(messages)
    return str(message.get("content") or "") if message else ""


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
