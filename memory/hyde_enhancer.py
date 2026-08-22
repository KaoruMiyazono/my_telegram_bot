"""
HyDE（Hypothetical Document Embeddings）检索增强

严格复刻 akashic-agent memory2/hyde_enhancer.py：
  类结构、方法签名、prompt、流程完全对齐。

流程：
  1. augment() 内部并行：raw 检索 + LLM 生成假想记忆
  2. hypothesis 就绪 → 第二次检索
  3. union dedup：raw 全保留 + hyde 追加独有条目
  4. 任何步骤失败 → 降级返回 raw 结果，used_hyde=False
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from openai import AsyncOpenAI

from config.settings import settings

logger = logging.getLogger(__name__)


#  memory系统里面的检索增强模块 本质上就是用大模型生成了可能的回答，增加检索效率
@dataclass
class HyDEAugmentResult:
    """augment() 的返回值（对齐 akashic HyDEAugmentResult）"""
    items: list[Any]                # 合并后的最终结果
    used_hyde: bool                 # HyDE 是否实际追加了新条目
    hypothesis: str | None          # LLM 生成的假想文本
    raw_hits: list[Any] = field(default_factory=list)


class HyDEEnhancer:
    """HyDE 检索增强器（对齐 akashic HyDEEnhancer）"""

    HYPOTHESIS_MAX_TOKENS = 80
    DEFAULT_TIMEOUT_S = 3.0

    def __init__(
        self,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        prompt_builder: Callable[[str], str] | None = None,
    ) -> None:
        self._timeout_s = max(0.5, float(timeout_s))
        self._prompt_builder = prompt_builder or self._build_default_prompt

    #  输入时用户的问题，输出时生成的假想记忆条目
    #  比如 输入是 "我想知道昨天的天气怎么样"，输出可能是 昨天天气是晴天
    async def generate_hypothesis(self, query: str) -> str | None:
        """
        生成假想记忆条目。失败/超时返回 None，调用方降级为原始检索。

        关键 prompt 约束（对齐 akashic）：
        - 生成肯定式条目，描述如果该记忆存在会记录什么事实
        - 第三人称（"用户..."），简洁事实陈述
        - 只输出文本，不解释
        """
        prompt = self._prompt_builder(query)
        try:
            client = AsyncOpenAI(
                api_key=settings.DEEPSEEK_API_KEY,
                base_url=settings.DEEPSEEK_BASE_URL,
                timeout=5.0,
            )
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model=settings.LLM_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=self.HYPOTHESIS_MAX_TOKENS,
                    temperature=0.1,
                ),
                timeout=self._timeout_s,
            )
            text = (resp.choices[0].message.content or "").strip()
            return text if text else None
        except Exception as e:
            logger.debug("HyDE hypothesis generation failed: %s", e)
            return None

    @staticmethod
    def _build_default_prompt(query: str) -> str:
        """对齐 akashic _build_default_prompt"""
        return (
            "你是个人助手的记忆系统。根据用户提问，生成一条"
            "**如果该信息存在于记忆数据库中会长什么样**的假想条目。\n"
            "规则：\n"
            "- 始终生成肯定式条目，描述**如果该记忆存在会记录什么事实**，不要否定该事件的存在\n"
            '- 第三人称（"用户..."），与数据库条目语体一致（简洁的事实陈述）\n'
            "- 只输出那一条文本，不要解释，不要回答问题本身\n\n"
            f"用户提问：{query}\n"
            "假想记忆条目："
        )

    async def augment(
        self,
        *,
        raw_query: str,
        retrieve_fn: Callable[[str], Awaitable[list[Any]]],
        top_k: int,
    ) -> HyDEAugmentResult:
        """
        双路检索 + union dedup（对齐 akashic augment 方法签名）。

        raw 结果完整保留，hyde 只追加 raw 中不存在的独有条目。
        """
        # 1. 并行：raw 检索 + hypothesis 生成 raw检索就是去数据库检索
        raw_task = asyncio.create_task(retrieve_fn(raw_query))
        hyp_task = asyncio.create_task(self.generate_hypothesis(raw_query))
        raw_hits, hypothesis = await asyncio.gather(raw_task, hyp_task)

        if not hypothesis:
            logger.debug("HyDE: no hypothesis, using raw results only")
            return HyDEAugmentResult(
                items=raw_hits, used_hyde=False, hypothesis=None, raw_hits=raw_hits,
            )

        # 2. hypothesis 就绪后，第二次检索
        try:
            hyde_hits = await retrieve_fn(hypothesis)
        except Exception as e:
            logger.debug("HyDE retrieve failed: %s", e)
            return HyDEAugmentResult(
                items=raw_hits, used_hyde=False, hypothesis=hypothesis, raw_hits=raw_hits,
            )

        # 3. Union dedup（对齐 akashic _union_dedup）
        merged = _union_dedup(raw_hits, hyde_hits)
        used_hyde = len(merged) > len(raw_hits)
        logger.info(
            "HyDE: raw=%d hyde=%d merged=%d used_hyde=%s hypothesis=%r",
            len(raw_hits), len(hyde_hits), len(merged),
            used_hyde, hypothesis[:60],
        )
        return HyDEAugmentResult(
            items=merged, used_hyde=used_hyde, hypothesis=hypothesis, raw_hits=raw_hits,
        )


def _union_dedup(raw: list[Any], hyde: list[Any]) -> list[Any]:
    """对齐 akashic _union_dedup：raw 全保留，hyde 追加独有条目"""
    seen_ids: set[str] = set()
    result: list[Any] = []
    for item in raw:
        item_id = str(getattr(item, "id", "") or "")
        if item_id:
            seen_ids.add(item_id)
        result.append(item)
    for item in hyde:
        item_id = str(getattr(item, "id", "") or "")
        if item_id and item_id in seen_ids:
            continue
        result.append(item)
        if item_id:
            seen_ids.add(item_id)
    return result
