"""
Consolidation 窗口期处理：用 LLM 从原始对话中提炼结构化摘要

复刻 akashic ConsolidationService：
  每个 conversation window → LLM 提取 event/preference/profile 摘要
  → 存入向量库，附带 source_ref 可追溯

这才是作者说的"在记忆提取那里调整prompt"的战场。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from config.settings import settings

logger = logging.getLogger(__name__)

_EXTRACTION_PROMPT = """从以下用户与 AI 助手的对话中，提取值得长期记住的信息。
每条信息用一行输出，格式：
  [类型] 内容

类型只能是以下之一：
  preference   — 用户的偏好、喜好、习惯
  identity     — 用户的身份、职业、技能
  event        — 用户提到的事件、行为、状态变化
  fact         — 其他值得记住的客观事实

规则：
- 只提取用户明确表达的信息，不要推测
- 如果对话涉及信息更新（如"以前喜欢X，现在喜欢Y"），提取最新的
- 不要提取闲聊、问候、AI 自己的回复
- 每条一条，不要编号
- 没有可提取的内容就返回空

对话：
{conversation}

提取结果（没有就返回空）："""


async def _llm_extract(client: AsyncOpenAI, model: str, conversation: str) -> list[dict[str, str]]:
    """调 LLM 提取结构化摘要"""
    if not conversation.strip():
        return []

    prompt = _EXTRACTION_PROMPT.format(conversation=conversation[:3000])

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.1,
        )
        content = resp.choices[0].message.content or ""
    except Exception as e:
        logger.warning("LLM extraction failed: %s", e)
        return []

    summaries: list[dict[str, str]] = []
    for line in content.strip().split("\n"):
        line = line.strip()
        if not line or not line.startswith("["):
            continue

        # 解析 "[preference] 用户喜欢喝拿铁"
        end_bracket = line.find("]")
        if end_bracket == -1:
            continue

        mem_type = line[1:end_bracket].strip()
        summary = line[end_bracket + 1:].strip()

        if mem_type in ("preference", "identity", "event", "fact") and summary:
            summaries.append({"summary": summary, "memory_type": mem_type})

    return summaries


async def consolidate_sessions(
    store: Any,
    seeded: dict[str, list[str]],
    user_id: int,
) -> int:
    """对已播种的原始消息做 LLM consolidation"""
    mock_path = Path(__file__).parent.parent / "data" / "evaluation" / "mock_conversations.jsonl"
    if not mock_path.exists():
        return 0

    client = AsyncOpenAI(
        api_key=settings.DEEPSEEK_API_KEY,
        base_url=settings.DEEPSEEK_BASE_URL,
        timeout=30.0,
    )
    model = settings.LLM_MODEL
    total_new = 0

    with open(mock_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            conv = json.loads(line)
            session_id = conv.get("session_id", "")
            messages = conv.get("messages", [])

            # 拼成对话文本
            conversation = "\n".join(
                f"[{m['role']}]: {m['content']}" for m in messages
            )

            summaries = await _llm_extract(client, model, conversation)
            for s in summaries:
                try:
                    await store.upsert_item(
                        memory_type=s["memory_type"],
                        summary=s["summary"],
                        user_id=user_id,
                        source_ref=f"session:{user_id}:{session_id[:8]}",
                    )
                    total_new += 1
                    logger.info("LLM extracted: [%s] %s", s["memory_type"], s["summary"][:60])
                except Exception as e:
                    logger.error("Consolidation upsert failed: %s", e)

    logger.info("LLM consolidation done: %d new summaries", total_new)
    return total_new
