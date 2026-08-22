from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from typing import Protocol

from agent.core.types import MemoryItem
from agent.prompting import PromptSectionMeta, PromptSectionRender, SectionCache


@dataclass
class TurnContext:
    memories: list[MemoryItem]
    user_id: int | None = None
    retrieved_memory_block: str = ""
    benchmark_mode: bool = False


class PromptBlock(Protocol):
    priority: int
    label: str
    is_static: bool

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        ...

    def cache_signature(self, ctx: TurnContext) -> str | None:
        ...


class AssistantBasePromptBlock:
    priority = 10
    label = "assistant_base"
    is_static = True

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        return "你是一个友好的 AI 助手。"

    def cache_signature(self, ctx: TurnContext) -> str | None:
        return "assistant_base:v1"


class RetrievedMemoryPromptBlock:
    priority = 55
    label = "retrieved_memory"
    is_static = False

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        block = (ctx.retrieved_memory_block or "").strip()
        return block or None

    def cache_signature(self, ctx: TurnContext) -> str | None:
        return None


class RecentContextPromptBlock:
    priority = 45
    label = "recent_context"
    is_static = False

    def __init__(self, read_recent_context: Callable[[int], str] | None = None) -> None:
        self._read_recent_context = read_recent_context

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        if self._read_recent_context is None or ctx.user_id is None:
            return None
        content = self._read_recent_context(ctx.user_id)
        stable = _strip_recent_turns(content)
        return stable or None

    def cache_signature(self, ctx: TurnContext) -> str | None:
        return None


class SelfModelPromptBlock:
    priority = 30
    label = "self_model"
    is_static = False

    def __init__(self, read_self: Callable[[int], str] | None = None) -> None:
        self._read_self = read_self

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        if self._read_self is None or ctx.user_id is None:
            return None
        content = _strip_empty_markdown_profile(
            self._read_self(ctx.user_id),
            empty_titles={"# Self Model"},
        )
        if not content:
            return None
        return f"## Self Model\n\n{content}"

    def cache_signature(self, ctx: TurnContext) -> str | None:
        return None


class LongTermMemoryPromptBlock:
    priority = 35
    label = "long_term_memory"
    is_static = False

    def __init__(self, read_long_term: Callable[[int], str] | None = None) -> None:
        self._read_long_term = read_long_term

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        if self._read_long_term is None or ctx.user_id is None:
            return None
        content = _strip_empty_markdown_profile(
            self._read_long_term(ctx.user_id),
            empty_titles={"# Long-term Memory"},
        )
        if not content:
            return None
        return f"## Long-term Memory\n\n{content}"

    def cache_signature(self, ctx: TurnContext) -> str | None:
        return None


class SourceRefProtocolPromptBlock:
    priority = 60
    label = "source_ref_protocol"
    is_static = True

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        if not ctx.memories:
            return None
        return (
            "💡 每条记忆末尾的 [↗ session:...] 是 source_ref。\n"
            '   历史事实、原话、时间线、身份、偏好更新等需要证据时，调用 fetch_messages(source_ref="session:...")\n'
            '   记忆不够时，调用 recall_memory(query="你想找的内容")\n'
            "   recall_memory 的 status=active 表示当前有效事实；status=superseded 表示已被替代的历史事实。"
        )

    def cache_signature(self, ctx: TurnContext) -> str | None:
        return "source_ref_protocol:v2"


class BenchmarkPromptBlock:
    priority = 80
    label = "benchmark_memory_protocol"
    is_static = True

    def __init__(self, content: str) -> None:
        self._content = content

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        return self._content if ctx.benchmark_mode else None

    def cache_signature(self, ctx: TurnContext) -> str | None:
        return "benchmark_memory_protocol:v1"


@dataclass(frozen=True)
class SystemPromptBuildResult:
    system_sections: list[PromptSectionRender]
    system_prompt: str
    debug_breakdown: list[PromptSectionMeta]


class SystemPromptBuilder:
    def __init__(
        self,
        blocks: list[PromptBlock],
        cache: SectionCache | None = None,
    ) -> None:
        self._blocks = sorted(blocks, key=lambda block: block.priority)
        self._cache = cache or SectionCache()

    def build(
        self,
        ctx: TurnContext,
        *,
        system_sections_top: list[PromptSectionRender] | None = None,
        system_sections_bottom: list[PromptSectionRender] | None = None,
        disabled_sections: set[str] | None = None,
    ) -> SystemPromptBuildResult:
        disabled = disabled_sections or set()
        cache_scope = "telegram-bot-mvp"
        rendered_sections: list[PromptSectionRender] = []
        debug: list[PromptSectionMeta] = []

        for block in self._blocks:
            if block.label in disabled:
                continue
            cache_hit = False
            rendered: str | None = None
            signature = block.cache_signature(ctx) if block.is_static else None
            if signature:
                rendered = self._cache.get(cache_scope, block.label, signature)
                cache_hit = rendered is not None
            if rendered is None:
                rendered = block.render(ctx, cached_signature=signature)
                if rendered and signature:
                    self._cache.set(cache_scope, block.label, signature, rendered)
            if rendered:
                section = PromptSectionRender(
                    name=block.label,
                    content=rendered,
                    is_static=block.is_static,
                    cache_hit=cache_hit,
                )
                rendered_sections.append(section)
                debug.append(_section_meta(section))

        top = [section for section in system_sections_top or [] if section.name not in disabled]
        bottom = [
            section for section in system_sections_bottom or []
            if section.name not in disabled
        ]
        system_sections = [*top, *rendered_sections, *bottom]
        return SystemPromptBuildResult(
            system_sections=system_sections,
            system_prompt="\n\n---\n\n".join(section.content for section in system_sections),
            debug_breakdown=[*[_section_meta(s) for s in top], *debug, *[_section_meta(s) for s in bottom]],
        )


def default_system_prompt_builder(
    benchmark_prompt: str,
    *,
    self_model_reader: Callable[[int], str] | None = None,
    long_term_memory_reader: Callable[[int], str] | None = None,
    recent_context_reader: Callable[[int], str] | None = None,
) -> SystemPromptBuilder:
    return SystemPromptBuilder(
        [
            AssistantBasePromptBlock(),
            SelfModelPromptBlock(self_model_reader),
            LongTermMemoryPromptBlock(long_term_memory_reader),
            RecentContextPromptBlock(recent_context_reader),
            RetrievedMemoryPromptBlock(),
            SourceRefProtocolPromptBlock(),
            BenchmarkPromptBlock(benchmark_prompt),
        ]
    )


def _section_meta(section: PromptSectionRender) -> PromptSectionMeta:
    return PromptSectionMeta(
        name=section.name,
        chars=len(section.content),
        est_tokens=max(1, len(section.content) // 3),
        is_static=section.is_static,
        cache_hit=section.cache_hit,
    )


def _strip_recent_turns(content: str) -> str:
    text = (content or "").strip()
    if not text:
        return ""
    marker = "\n## Recent Turns"
    cut = text.find(marker)
    if cut != -1:
        text = text[:cut].strip()
    if text == "# Recent Context":
        return ""
    return text


def _strip_empty_markdown_profile(content: str, *, empty_titles: set[str]) -> str:
    text = (content or "").strip()
    if not text:
        return ""
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            kept.append("")
            continue
        if stripped in empty_titles:
            continue
        kept.append(line.rstrip())
    cleaned = "\n".join(kept).strip()
    if not cleaned:
        return ""
    return cleaned
