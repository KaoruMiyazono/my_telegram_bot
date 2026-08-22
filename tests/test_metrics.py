"""
测试评估指标：Token F1, Exact Match, LLM-as-Judge, score_results
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from evaluation.metrics import (
    token_f1,
    exact_match,
    _rule_judge,
    _normalise,
    _tokenise,
    _char_tokenise,
    _extract_keywords,
    evaluate_single,
    score_results,
    format_score_report,
)


# ── 文本标准化 ────────────────────────────────────────────────────────────

def test_normalise_english() -> None:
    assert _normalise("Hello, World!") == "hello world"
    assert _normalise("  What's   up?  ") == "whats up"
    print("✅ test_normalise_english")


def test_normalise_chinese() -> None:
    assert _normalise("你好，世界！") == "你好世界"
    assert _normalise("  我  喜欢  拿铁  ") == "我喜欢拿铁"
    print("✅ test_normalise_chinese")


def test_tokenise_english() -> None:
    assert _tokenise("hello world") == ["hello", "world"]
    assert _tokenise("What's up?") == ["whats", "up"]
    print("✅ test_tokenise_english")


def test_tokenise_chinese() -> None:
    tokens = _tokenise("你喜欢喝拿铁")
    # 字符级中文分词：每个 CJK 字单独
    assert "你" in tokens
    assert "拿" in tokens
    assert "铁" in tokens
    assert len(tokens) == 6  # 你喜 欢 喝 拿 铁
    print(f"✅ test_tokenise_chinese: {tokens}")


# ── Token F1 ───────────────────────────────────────────────────────────────

def test_token_f1_perfect_match() -> None:
    assert token_f1("你喜欢喝拿铁", "你喜欢喝拿铁") == 1.0
    print("✅ test_token_f1_perfect_match")


def test_token_f1_no_overlap() -> None:
    assert token_f1("咖啡好喝", "蛋糕很甜") == 0.0
    print("✅ test_token_f1_no_overlap")


def test_token_f1_partial_match() -> None:
    # "拿铁" 出现在两句中
    f1 = token_f1("你喜欢喝拿铁", "根据你的喜好推荐拿铁")
    assert 0.0 < f1 < 1.0, f"Expected partial F1, got {f1}"
    print(f"✅ test_token_f1_partial_match: F1={f1:.4f}")


def test_token_f1_english() -> None:
    # Same as SQuAD baseline
    f1 = token_f1("I like latte", "I really like latte coffee")
    assert 0.3 < f1 < 0.9, f"Expected English F1 in range, got {f1}"
    print(f"✅ test_token_f1_english: F1={f1:.4f}")


def test_token_f1_empty() -> None:
    assert token_f1("", "") == 1.0
    assert token_f1("hello", "") == 0.0
    assert token_f1("", "hello") == 0.0
    print("✅ test_token_f1_empty")


# ── Exact Match ────────────────────────────────────────────────────────────

def test_exact_match_identical() -> None:
    assert exact_match("Hello World", "Hello World") is True
    print("✅ test_exact_match_identical")


def test_exact_match_case_insensitive() -> None:
    assert exact_match("Hello World", "hello world") is True
    print("✅ test_exact_match_case_insensitive")


def test_exact_match_punctuation_insensitive() -> None:
    assert exact_match("Hello, World!", "hello world") is True
    print("✅ test_exact_match_punctuation_insensitive")


def test_exact_match_different() -> None:
    assert exact_match("Hello World", "Goodbye World") is False
    print("✅ test_exact_match_different")


def test_exact_match_chinese() -> None:
    assert exact_match("你喜欢喝拿铁", "你喜欢喝拿铁") is True
    assert exact_match("你喜欢喝拿铁", "你喜欢喝茶") is False
    print("✅ test_exact_match_chinese")


# ── Rule Judge ─────────────────────────────────────────────────────────────

def test_rule_judge_high_f1() -> None:
    # 高 F1 直接判对
    assert _rule_judge("你喜欢喝拿铁", "你喜欢拿铁") is True
    print("✅ test_rule_judge_high_f1")


def test_rule_judge_keyword_coverage() -> None:
    # F1 不高但关键词覆盖够
    assert _rule_judge("我推荐拿铁给你", "你喜欢喝拿铁") is True
    print("✅ test_rule_judge_keyword_coverage")


def test_rule_judge_wrong_facts() -> None:
    # 关键词不匹配
    assert _rule_judge("你应该喝茶", "你喜欢喝拿铁") is False
    print("✅ test_rule_judge_wrong_facts")


def test_rule_judge_empty() -> None:
    assert _rule_judge("", "hello") is False
    assert _rule_judge("hello", "") is False
    print("✅ test_rule_judge_empty")


# ── Evaluate Single ────────────────────────────────────────────────────────

def test_evaluate_single() -> None:
    result = evaluate_single(
        predicted_answer="根据你的喜好，推荐拿铁",
        gold_answer="你喜欢喝拿铁",
        question="我喜欢喝什么？",
    )
    assert "token_f1" in result
    assert "exact_match" in result
    assert "rule_judge_correct" in result
    assert result["exact_match"] is False  # 不可能精确匹配
    assert result["rule_judge_correct"] is True  # 语义等价
    print(f"✅ test_evaluate_single: {result}")


# ── score_results 聚合 ────────────────────────────────────────────────────

def test_score_results_empty() -> None:
    scores = score_results([])
    assert scores["overall"]["n"] == 0
    assert scores["overall"]["f1"] == 0.0
    print("✅ test_score_results_empty")


def test_score_results_basic() -> None:
    results = [
        {
            "case_id": "green-001",
            "question_type": "single_session_fact",
            "gold_answer": "你喜欢喝拿铁",
            "predicted_answer": "你喜欢喝拿铁",
            "error": None,
        },
        {
            "case_id": "green-003",
            "question_type": "cross_session_preference",
            "gold_answer": "根据你的喜好，推荐拿铁",
            "predicted_answer": "推荐喝茶",  # 错误答案
            "error": None,
        },
    ]
    scores = score_results(results)
    overall = scores["overall"]
    assert overall["n"] == 2
    assert overall["em"] == 0.5  # 第一题对，第二题错
    print(f"✅ test_score_results_basic: overall={overall}")


def test_score_results_by_type() -> None:
    results = [
        {
            "case_id": "g1", "question_type": "single_session_fact",
            "gold_answer": "拿铁", "predicted_answer": "拿铁",
        },
        {
            "case_id": "g2", "question_type": "single_session_fact",
            "gold_answer": "茶", "predicted_answer": "咖啡",
        },
        {
            "case_id": "g3", "question_type": "knowledge_update",
            "gold_answer": "你已经不喝咖啡了", "predicted_answer": "你已经不喝咖啡了改喝茶",
        },
    ]
    scores = score_results(results)
    by_type = scores["by_type"]
    assert "single_session_fact" in by_type
    assert "knowledge_update" in by_type
    assert by_type["single_session_fact"]["em"] == 0.5
    assert by_type["knowledge_update"]["n"] == 1
    print(f"✅ test_score_results_by_type: {by_type}")


def test_score_results_with_errors() -> None:
    results = [
        {
            "case_id": "e1", "question_type": "single_session_fact",
            "gold_answer": "", "predicted_answer": "",
            "error": "timeout",
        },
    ]
    scores = score_results(results)
    assert scores["overall"]["errors"] == 1
    assert scores["overall"]["f1"] == 0.0
    print("✅ test_score_results_with_errors")


def test_score_results_with_judge() -> None:
    results = [
        {
            "case_id": "j1", "question_type": "single_session_fact",
            "gold_answer": "你喜欢喝拿铁",
            "predicted_answer": "推荐拿铁给你",
            "judge_correct": True,
        },
        {
            "case_id": "j2", "question_type": "knowledge_update",
            "gold_answer": "你已经不喝咖啡了",
            "predicted_answer": "你喜欢咖啡",
            "judge_correct": False,
        },
    ]
    scores = score_results(results)
    assert scores["overall"]["judge_acc"] == 0.5
    print(f"✅ test_score_results_with_judge: judge_acc={scores['overall']['judge_acc']}")


# ── format_score_report ────────────────────────────────────────────────────

def test_format_score_report() -> None:
    scores = score_results([
        {
            "case_id": "r1", "question_type": "single_session_fact",
            "gold_answer": "拿铁", "predicted_answer": "拿铁",
        },
    ])
    report = format_score_report(scores)
    assert "RAG Evaluation Report" in report
    assert "Token F1" in report
    print(f"✅ test_format_score_report:\n{report}")


# ── 关键词提取 ─────────────────────────────────────────────────────────────

def test_extract_keywords() -> None:
    kw = _extract_keywords("你喜欢喝拿铁咖啡")
    # CJK 单字保留："拿" "铁" "咖" "啡"（"喝" "喜" "欢" 应保留，非停用词）
    assert any("拿" in k for k in kw)
    assert any("咖" in k for k in kw) or any("啡" in k for k in kw)
    # 停用词应被过滤
    assert "你" not in kw
    print(f"✅ test_extract_keywords: {kw}")


# ── 运行 ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Running metrics tests...\n")

    # Normalization
    test_normalise_english()
    test_normalise_chinese()
    test_tokenise_english()
    test_tokenise_chinese()

    # Token F1
    test_token_f1_perfect_match()
    test_token_f1_no_overlap()
    test_token_f1_partial_match()
    test_token_f1_english()
    test_token_f1_empty()

    # Exact Match
    test_exact_match_identical()
    test_exact_match_case_insensitive()
    test_exact_match_punctuation_insensitive()
    test_exact_match_different()
    test_exact_match_chinese()

    # Rule Judge
    test_rule_judge_high_f1()
    test_rule_judge_keyword_coverage()
    test_rule_judge_wrong_facts()
    test_rule_judge_empty()

    # Evaluate Single
    test_evaluate_single()

    # Score Results
    test_score_results_empty()
    test_score_results_basic()
    test_score_results_by_type()
    test_score_results_with_errors()
    test_score_results_with_judge()

    # Report
    test_format_score_report()

    # Keywords
    test_extract_keywords()

    print("\n" + "=" * 50)
    print("All metrics tests passed! ✅")
    print("=" * 50)
