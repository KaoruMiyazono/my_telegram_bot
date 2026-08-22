"""
评估指标实现

三个核心指标（参考 akashic-agent 的长记忆评估体系）：

1. Token F1 — SQuAD 风格的词级 F1，衡量回答与标准答案的词汇重叠度
2. Exact Match — 标准化后精确匹配，衡量回答是否一字不差
3. LLM-as-Judge — 语义正确性判断（当前用规则模拟，预留真实 LLM 接口）

三者组合使用原因：
  - EM 太严格：Bot 说"你喜欢拿铁" vs 标准"你喜欢喝拿铁"就判错了
  - Token F1 缓解但不够：两句话用词完全不同但语义相同时 F1=0
  - LLM-as-Judge 兜底：能判断"我推荐来杯拿铁"和"你喜欢拿铁"本质相同

指标分层策略（按 rigor 降序）：
  exact_match > token_f1 > llm_judge
  先用 EM 精确筛，再用 F1 模糊匹配，最后 LLM 做语义兜底
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any

logger = logging.getLogger(__name__)

# ── LLM Judge 的 prompt 模板（预留真实接口时使用） ─────────────────────────

_JUDGE_PROMPT = """\
You are a strict judge for a RAG memory benchmark.

The gold answer describes the correct answer the bot should give.
The predicted answer is what the bot actually said.

Question: {question}
Gold answer: {gold}
Predicted answer: {predicted}

Judge strictly: the predicted answer is correct only if it is semantically
equivalent to the gold answer — same meaning, same facts. Minor wording
differences are fine. Missing key information or wrong facts = incorrect.

Reply with exactly one word: yes or no."""

# ── 文本标准化 ────────────────────────────────────────────────────────────

def _normalise(text: str) -> str:
    """标准化文本：小写、去标点（含全角）、合并空白、去 CJK 间空格"""
    text = text.lower()
    # 去掉所有标点符号（ASCII + 中文全角），只保留字母数字和空白
    text = re.sub(r"[^\w\s]", "", text, flags=re.UNICODE)
    # CJK 字符之间的空格去掉（中文不应该有词间空格）
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _tokenise(text: str) -> list[str]:
    """分词：标准化后按空白切分（英文）/ 按字符切分（含中文时混合策略）"""
    normalised = _normalise(text)
    # 如果包含中文字符，用字符级分词；否则按空格分词
    if re.search(r"[\u4e00-\u9fff]", normalised):
        return _char_tokenise(normalised)
    return normalised.split()


def _char_tokenise(text: str) -> list[str]:
    """中文混合分词：中文按单字，英文/数字按连续片段"""
    tokens = []
    for part in re.findall(r"[\u4e00-\u9fff]|[a-z0-9]+", text):
        tokens.append(part)
    return tokens


# ── Token F1 ───────────────────────────────────────────────────────────────

def token_f1(pred: str, gold: str) -> float:
    """
    SQuAD 风格的词级 F1。

    计算预测回答与标准答案之间的 token 级 precision/recall/F1。
    Counter 交集意味着同一个词出现多次也只按较小次数计算。

    Args:
        pred: Bot 的实际回答
        gold: 标准答案

    Returns:
        F1 分数 in [0.0, 1.0]
    """
    pred_tokens = _tokenise(pred)
    gold_tokens = _tokenise(gold)

    if not pred_tokens or not gold_tokens:
        return float(pred_tokens == gold_tokens)

    common = Counter(pred_tokens) & Counter(gold_tokens)
    num_same = sum(common.values())

    if num_same == 0:
        return 0.0

    precision = num_same / len(pred_tokens)
    recall = num_same / len(gold_tokens)
    return 2 * precision * recall / (precision + recall)


# ── Exact Match ────────────────────────────────────────────────────────────

def exact_match(pred: str, gold: str) -> bool:
    """
    标准化后精确匹配。

    去掉大小写、标点、多余空白后，逐字符比较。
    适用于日期、数字、固定名称等需要精确一致的场景。

    Args:
        pred: Bot 的实际回答
        gold: 标准答案

    Returns:
        True 如果标准化后完全相同
    """
    return _normalise(pred) == _normalise(gold)


# ── LLM-as-Judge（规则模拟版） ────────────────────────────────────────────

# 规则模拟的阈值
_RULE_F1_THRESHOLD = 0.5       # Token F1 ≥ 此值视为语义相似
_RULE_KEYWORD_OVERLAP = 0.4    # 关键词覆盖率 ≥ 此值视为语义等价

def _rule_judge(pred: str, gold: str) -> bool:
    """
    用规则模拟 LLM 裁判（无需 API 调用）。

    两步判断：
    1. 如果 Token F1 已经很高（≥0.5），直接判正确
    2. 否则，提取 gold 中的关键词，检查 pred 中是否覆盖了足够多

    这不是精确的语义判断，但在答案较短、词汇重叠度高的场景下有效。
    第3.5步可替换为真实 LLM 调用。
    """
    if not pred or not pred.strip():
        return False
    if not gold or not gold.strip():
        return False

    f1 = token_f1(pred, gold)
    if f1 >= _RULE_F1_THRESHOLD:
        return True

    # 关键词覆盖检查
    gold_keywords = _extract_keywords(gold)
    if not gold_keywords:
        return False

    pred_normalised = _normalise(pred)
    matched = sum(1 for kw in gold_keywords if kw in pred_normalised)
    coverage = matched / len(gold_keywords)

    return coverage >= _RULE_KEYWORD_OVERLAP


def _extract_keywords(text: str) -> list[str]:
    """提取文本中的关键词（去停用词后的 token）"""
    tokens = _tokenise(text)
    # 基础停用词（中英文）
    stopwords = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been",
        "you", "your", "i", "my", "me", "he", "she", "it", "they",
        "to", "of", "in", "for", "on", "with", "at", "by", "from",
        "and", "or", "but", "not", "that", "this", "as", "so",
        "的", "了", "是", "在", "我", "你", "他", "她", "它", "们",
        "有", "和", "就", "都", "也", "不", "这", "那", "一个",
        "可以", "什么", "怎么", "为什么", "哪个",
    }
    return [
        t for t in tokens
        if t not in stopwords and (
            len(t) > 1 or re.match(r"[\u4e00-\u9fff]", t)  # CJK 单字保留
        )
    ]


async def judge_answer(
    *,
    question: str,
    gold: str,
    predicted: str,
    provider: Any | None = None,
    model: str = "",
) -> bool:
    """
    LLM-as-Judge：判断预测回答语义上是否等价于标准答案。

    当前使用规则模拟（_rule_judge），避免 API 调用。
    传入 provider 后可切换为真实 LLM 调用。

    Args:
        question: 用户问题
        gold: 标准答案
        predicted: Bot 的实际回答
        provider: 可选，真实 LLM provider（如 AsyncOpenAI 实例）
        model: 可选，LLM 模型名

    Returns:
        True 如果语义等价
    """
    if not predicted or not predicted.strip():
        return False

    # 如果有真实 provider，走 LLM 调用
    if provider is not None:
        return await _llm_judge(
            provider=provider,
            model=model,
            question=question,
            gold=gold,
            predicted=predicted,
        )

    # 否则用规则模拟
    return _rule_judge(predicted, gold)


async def _llm_judge(
    provider: Any,
    model: str,
    *,
    question: str,
    gold: str,
    predicted: str,
) -> bool:
    """
    真实 LLM 调用判断语义等价性。

    provider 需要实现 chat 方法，签名兼容 openai.AsyncOpenAI。
    """
    prompt = _JUDGE_PROMPT.format(
        question=question.strip(),
        gold=gold.strip(),
        predicted=predicted.strip(),
    )
    try:
        resp = await provider.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4,
        )
        content = resp.choices[0].message.content or ""
        verdict = content.strip().lower()
        return verdict.startswith("yes")
    except Exception as e:
        logger.warning("llm_judge failed: %s, falling back to rule", e)
        return _rule_judge(predicted, gold)


# ── 单用例评估 ─────────────────────────────────────────────────────────────

def evaluate_single(
    predicted_answer: str,
    gold_answer: str,
    question: str = "",
    *,
    provider: Any | None = None,
    model: str = "",
) -> dict[str, Any]:
    """
    对单个用例计算全部三个指标。

    Args:
        predicted_answer: Bot 的实际回答
        gold_answer: 标准答案
        question: 用户问题（LLM judge 需要）
        provider: 可选的 LLM provider
        model: 可选的 LLM 模型名

    Returns:
        {
            "token_f1": float,
            "exact_match": bool,
            "rule_judge_correct": bool | None,
            "llm_judge_correct": bool | None,  # 仅当 provider 不为 None
        }
    """
    import asyncio

    em = exact_match(predicted_answer, gold_answer)
    f1 = token_f1(predicted_answer, gold_answer)

    result: dict[str, Any] = {
        "token_f1": round(f1, 4),
        "exact_match": em,
    }

    if provider is not None:
        # 真实 LLM judge
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 在已有事件循环中，创建 task
                # 简化：同步调用 rule judge，异步 LLM judge 留给外部处理
                result["rule_judge_correct"] = _rule_judge(predicted_answer, gold_answer)
                result["llm_judge_correct"] = None  # 需要外部异步调用
            else:
                result["rule_judge_correct"] = _rule_judge(predicted_answer, gold_answer)
                result["llm_judge_correct"] = None
        except RuntimeError:
            result["rule_judge_correct"] = _rule_judge(predicted_answer, gold_answer)
    else:
        result["rule_judge_correct"] = _rule_judge(predicted_answer, gold_answer)

    return result


# ── 数据集级聚合 ───────────────────────────────────────────────────────────

def score_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    """
    聚合所有评估结果，输出 overall + by_type 分数。

    遵循 akashic-agent 的 score_results 模式：
      - overall: 全部用例的汇总指标
      - by_type: 按 question_type 分组的指标

    Args:
        results: 评估结果列表，每条包含:
            - question_id 或 case_id
            - question_type
            - gold_answer
            - predicted_answer
            - error (可选)
            - judge_correct (可选)

    Returns:
        {
            "overall": {"f1": float, "em": float, "judge_acc": float | None, "n": int, "errors": int},
            "by_type": {question_type: {"f1": float, "em": float, ...}},
        }
    """
    by_type: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        qt = r.get("question_type") or "unknown"
        by_type.setdefault(qt, []).append(r)

    def _agg(items: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(items)
        if n == 0:
            return {"f1": 0.0, "em": 0.0, "judge_acc": None, "n": 0, "errors": 0}

        errors = sum(1 for r in items if r.get("error"))

        f1s: list[float] = []
        ems: list[float] = []
        for r in items:
            if r.get("error"):
                f1s.append(0.0)
                ems.append(0.0)
            else:
                pred = str(r.get("predicted_answer") or "")
                gold = str(r.get("gold_answer") or "")
                f1s.append(token_f1(pred, gold))
                ems.append(1.0 if exact_match(pred, gold) else 0.0)

        # LLM judge 聚合（只有标记了 judge_correct 的用例参与）
        judged_items = [
            r for r in items
            if r.get("judge_correct") is not None and not r.get("error")
        ]
        judge_acc = None
        if judged_items:
            correct = sum(1 for r in judged_items if r["judge_correct"])
            judge_acc = round(correct / len(judged_items), 4)

        result: dict[str, Any] = {
            "f1": round(sum(f1s) / n, 4) if n else 0.0,
            "em": round(sum(ems) / n, 4) if n else 0.0,
            "n": n,
            "errors": errors,
        }
        if judge_acc is not None:
            result["judge_acc"] = judge_acc

        return result

    return {
        "overall": _agg(results),
        "by_type": {qt: _agg(items) for qt, items in sorted(by_type.items())},
    }


def format_score_report(scores: dict[str, Any]) -> str:
    """
    将 score_results 的输出格式化为可读报告。
    """
    lines = [
        "=" * 55,
        "RAG Evaluation Report",
        "=" * 55,
    ]

    overall = scores.get("overall", {})
    judge_str = ""
    if overall.get("judge_acc") is not None:
        judge_str = f"  Judge Acc:  {overall['judge_acc']:.2%}"

    lines.append("")
    lines.append("Overall")
    lines.append("-" * 40)
    lines.append(f"  Token F1:  {overall.get('f1', 0):.4f}")
    lines.append(f"  Exact Match: {overall.get('em', 0):.2%}")
    lines.append(f"  Cases:      {overall.get('n', 0)}")
    lines.append(f"  Errors:     {overall.get('errors', 0)}")
    if judge_str:
        lines.append(judge_str)

    by_type = scores.get("by_type", {})
    if by_type:
        lines.append("")
        lines.append("By Question Type")
        lines.append("-" * 40)
        for qt, agg in sorted(by_type.items()):
            lines.append(f"  [{qt}]")
            lines.append(f"    F1={agg.get('f1', 0):.4f}  EM={agg.get('em', 0):.2%}  n={agg.get('n', 0)}")

    lines.append("")
    lines.append("=" * 55)
    return "\n".join(lines)
