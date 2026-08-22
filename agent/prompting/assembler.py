from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptSectionRender:
    name: str
    content: str
    is_static: bool
    cache_hit: bool = False


@dataclass(frozen=True)
class PromptSectionMeta:
    name: str
    chars: int
    est_tokens: int
    is_static: bool
    cache_hit: bool


class SectionCache:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str, str], str] = {}

    def get(self, scope: str, section_name: str, signature: str) -> str | None:
        return self._data.get((scope, section_name, signature))

    def set(self, scope: str, section_name: str, signature: str, content: str) -> None:
        self._data[(scope, section_name, signature)] = content
