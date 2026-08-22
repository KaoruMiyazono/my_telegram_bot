"""
Post-response memory invalidation.

This mirrors akashic-agent's memory2.post_response_worker shape:
  user correction -> extract invalidation topics -> retrieve candidates
  -> LLM verifies which old memories should be superseded.

Unlike consolidation, this runs per turn and only retires stale structured
memories. It does not extract new long-term memories.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from openai import AsyncOpenAI

from config.settings import settings
from memory.embedder import Embedder
from memory.store import MemoryStore

logger = logging.getLogger(__name__)


class InvalidationWorker:
    """Detect explicit user corrections and mark stale memories superseded."""

    CANDIDATE_K = 5
    TOKEN_BUDGET_PER_RUN = 1000
    TOKENS_EXTRACT_INVALIDATION = 128
    TOKENS_CHECK_INVALIDATE = 160
    STRUCTURED_MEMORY_TYPES = ["procedure", "preference", "profile", "event", "fact"]

    def __init__(
        self,
        store: MemoryStore,
        embedder: Embedder,
        *,
        model: str | None = None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._client = AsyncOpenAI(
            api_key=settings.DEEPSEEK_API_KEY,
            base_url=settings.DEEPSEEK_BASE_URL,
            timeout=60.0,
        )
        self._model = model or settings.LLM_MODEL

    async def close(self) -> None:
        await self._client.close()

    async def run(
        self,
        *,
        user_msg: str,
        agent_response: str,
        tool_calls: list[dict[str, Any]],
        user_id: int,
        chat_id: int,
        source_ref: str,
    ) -> list[str]:
        """Run one post-response invalidation pass. Returns superseded ids."""
        token_budget = self.TOKEN_BUDGET_PER_RUN
        protected_ids = self._collect_protected_ids(tool_calls)
        topics, token_budget = await self._extract_invalidation_topics(
            user_msg,
            token_budget,
        )
        if not topics:
            return []

        all_superseded: list[str] = []
        for topic in topics[:3]:
            candidates = await self._retrieve_candidates(
                topic=topic,
                user_id=user_id,
                protected_ids=protected_ids,
                current_source_ref=source_ref,
            )
            if not candidates:
                continue

            supersede_ids, token_budget = await self._check_invalidate(
                topic=topic,
                user_msg=user_msg,
                candidates=candidates,
                token_budget=token_budget,
            )
            supersede_ids = [
                item_id
                for item_id in supersede_ids
                if item_id not in protected_ids and item_id not in all_superseded
            ]
            if not supersede_ids:
                continue

            updated = self._store.mark_superseded_batch(supersede_ids, user_id=user_id)
            all_superseded.extend(updated)
            if updated:
                logger.info(
                    "post_response invalidation: user=%d chat=%d source_ref=%s superseded=%s topic=%r",
                    user_id,
                    chat_id,
                    source_ref,
                    updated,
                    topic,
                )

        return all_superseded

    @staticmethod
    def _collect_protected_ids(tool_calls: list[dict[str, Any]]) -> set[str]:
        """Protect ids written by this turn's memorize tool from supersede."""
        protected: set[str] = set()
        for call in tool_calls or []:
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict) or function.get("name") != "memorize":
                continue
            result = call.get("result")
            if not isinstance(result, str) or not result.strip():
                continue
            try:
                payload = json.loads(result)
                item_id = str(payload.get("item_id") or "").strip()
                if item_id:
                    protected.add(item_id)
                    continue
            except json.JSONDecodeError:
                pass
            match = re.search(r"item_id=([A-Za-z0-9:_-]{1,128})", result)
            if match:
                protected.add(match.group(1))
        return protected

    async def _retrieve_candidates(
        self,
        *,
        topic: str,
        user_id: int,
        protected_ids: set[str],
        current_source_ref: str,
    ) -> list[dict[str, str]]:
        query_vec = await self._embedder.embed(topic)
        vec_results = await self._store.vector_search(
            query_vec=query_vec,
            user_id=user_id,
            top_k=self.CANDIDATE_K,
            memory_types=self.STRUCTURED_MEMORY_TYPES,
        )
        keyword_results = await self._store.keyword_search(
            terms=topic,
            user_id=user_id,
            limit=self.CANDIDATE_K,
        )

        candidates: list[dict[str, str]] = []
        seen: set[str] = set()
        for mem in [*vec_results, *keyword_results]:
            item_id = str(mem.id)
            if item_id in seen or item_id in protected_ids:
                continue
            if _same_exact_message_window(mem.source_ref or "", current_source_ref):
                continue
            if mem.memory_type not in self.STRUCTURED_MEMORY_TYPES:
                continue
            seen.add(item_id)
            candidates.append(
                {
                    "id": item_id,
                    "memory_type": mem.memory_type,
                    "summary": mem.summary,
                    "source_ref": mem.source_ref or "",
                }
            )
            if len(candidates) >= self.CANDIDATE_K:
                break
        return candidates

    async def _extract_invalidation_topics(
        self,
        user_msg: str,
        token_budget: int,
    ) -> tuple[list[str], int]:
        ok, token_budget = self._consume_budget(
            token_budget,
            self.TOKENS_EXTRACT_INVALIDATION,
        )
        if not ok:
            return [], token_budget

        prompt = f"""判断用户消息是否在明确纠正、否定、废弃或更新一条旧记忆。

用户消息：{user_msg}

【必须同时满足才触发】
1. 用户表达了明确的否定/纠错/废弃/更新意图，例如“不对/不是/错了/记错了/其实/改成/改为/换成/迁到/更新一下/流程更新/以后/忘掉/不要再/过时/删除”
2. 新说法会替代一条关于用户偏好、身份、事实、事件或 agent 操作流程的旧记忆

【以下情况返回 []】
- 用户只是在提问或确认
- 用户只是在表达普通否定句或普通新事实，但没有替代旧记忆
- 用户语气不确定，例如“也许/可能/我猜”
- 否定对象是第三方事实，且和用户/agent 记忆无关

若触发，提取 1-3 个简短主题，用于检索应废弃的旧记忆。
例：
- “不对，我喜欢茶” -> ["用户的饮品偏好"]
- “你记错了，我不住北京了，现在在上海” -> ["用户居住地"]
- “流程更新：仓库迁到 pnpm 了，以后提交前跑 pnpm test” -> ["仓库提交前测试流程"]
- “以后不要再用 web_search 查 Steam 了” -> ["Steam 查询流程"]

只返回 JSON 数组，如 ["用户的饮品偏好"] 或 []。"""
        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=self.TOKENS_EXTRACT_INVALIDATION,
                temperature=0.0,
            )
            text = (resp.choices[0].message.content or "").strip()
            result = _loads_json_array(text)
            return result, token_budget
        except Exception as exc:
            logger.warning("extract_invalidation_topics failed: %s", exc)
            return [], token_budget

    async def _check_invalidate(
        self,
        *,
        topic: str,
        user_msg: str,
        candidates: list[dict[str, str]],
        token_budget: int,
    ) -> tuple[list[str], int]:
        ok, token_budget = self._consume_budget(
            token_budget,
            self.TOKENS_CHECK_INVALIDATE,
        )
        if not ok:
            return [], token_budget

        old_block = "\n".join(
            f"- id={c['id']} | type={c['memory_type']} | {c['summary']}"
            for c in candidates
        )
        prompt = f"""用户本轮明确纠正/否定/更新了旧记忆主题：“{topic}”。

用户原话：
{user_msg}

以下是数据库中召回的候选旧记忆：
{old_block}

判断哪些候选记忆应被标记为 superseded。

规则：
- 只有候选记忆确实与用户纠正的主题冲突、过时或被用户要求废弃时，输出其 id
- 用户的新说法本身、仍然正确的记忆、无关记忆都不要输出
- 不要因为语义相近就删除，必须能从用户原话看出旧记忆需要退休

只返回 JSON 数组，如 ["id1"] 或 []。"""
        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=self.TOKENS_CHECK_INVALIDATE,
                temperature=0.0,
            )
            valid_ids = {c["id"] for c in candidates}
            result = _loads_json_array((resp.choices[0].message.content or "").strip())
            return [item_id for item_id in result if item_id in valid_ids], token_budget
        except Exception as exc:
            logger.warning("check_invalidate failed: %s", exc)
            return [], token_budget

    @staticmethod
    def _consume_budget(remain: int, cost: int) -> tuple[bool, int]:
        if remain < cost:
            return False, remain
        return True, remain - cost


def _loads_json_array(text: str) -> list[str]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("invalid JSON array from invalidation LLM: %r", raw[:200])
        return []
    if not isinstance(parsed, list):
        return []
    result: list[str] = []
    for item in parsed:
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
    return result


def _same_exact_message_window(left: str, right: str) -> bool:
    left_ref = str(left or "").strip()
    right_ref = str(right or "").strip()
    if not left_ref or not right_ref:
        return False
    if "#msg:" not in left_ref or "#msg:" not in right_ref:
        return False
    return left_ref == right_ref
