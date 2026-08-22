"""
测试 EvalCase 数据结构和 EvalDataset 的存取与验证
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from evaluation.dataset_builder import (
    EvalCase,
    EvalDataset,
    QuestionType,
    DistanceType,
    build_green_set_from_mock,
)


def test_evalcase_to_from_dict() -> None:
    """测试 EvalCase 的序列化/反序列化"""
    case = EvalCase(
        case_id="test-001",
        question="测试问题",
        gold_answer="测试答案",
        question_type=QuestionType.SINGLE_SESSION_FACT,
        context_sessions=["session-1"],
        distance_type=DistanceType.EXACT_MATCH,
        source="manual",
        notes="测试备注",
    )

    # 序列化
    d = case.to_dict()
    assert d["case_id"] == "test-001"
    assert d["question_type"] == "single_session_fact"
    assert d["distance_type"] == "exact_match"

    # 反序列化
    restored = EvalCase.from_dict(d)
    assert restored.case_id == case.case_id
    assert restored.question == case.question
    assert restored.gold_answer == case.gold_answer
    assert restored.question_type == case.question_type
    assert restored.distance_type == case.distance_type
    assert restored.context_sessions == case.context_sessions
    assert restored.source == case.source
    assert restored.notes == case.notes

    print("✅ test_evalcase_to_from_dict 通过")


def test_build_green_set() -> None:
    """测试绿灯集构建函数"""
    cases = build_green_set_from_mock()
    assert len(cases) == 13, f"期望 13 个用例，实际 {len(cases)}"

    # 验证类型覆盖
    types = {c.question_type for c in cases}
    assert QuestionType.SINGLE_SESSION_FACT in types
    assert QuestionType.CROSS_SESSION_PREFERENCE in types
    assert QuestionType.KNOWLEDGE_UPDATE in types
    assert QuestionType.USER_IDENTITY in types
    assert QuestionType.MULTI_TURN_CONTEXT in types
    print("✅ 五种问题类型全部覆盖")

    # 验证每个用例字段完整
    for case in cases:
        assert case.case_id, "case_id 不能为空"
        assert case.question, "question 不能为空"
        assert case.gold_answer, "gold_answer 不能为空"
    print("✅ 所有用例字段完整")

    print("✅ test_build_green_set 通过")


def test_dataset_save_load() -> None:
    """测试数据集保存和加载"""
    cases = build_green_set_from_mock()

    # 使用临时目录避免污染真实数据
    with tempfile.TemporaryDirectory() as tmpdir:
        dataset = EvalDataset(data_dir=tmpdir)

        # 保存
        path = dataset.save_green_set(cases)
        assert Path(path).exists()

        # 加载
        loaded = dataset.load_green_set()
        assert len(loaded) == len(cases)

        for original, restored in zip(cases, loaded):
            assert original.case_id == restored.case_id
            assert original.question == restored.question
            assert original.gold_answer == restored.gold_answer

        print("✅ test_dataset_save_load 通过")


def test_dataset_stats() -> None:
    """测试数据集统计"""
    cases = build_green_set_from_mock()
    dataset = EvalDataset()
    stats = dataset.stats(cases)

    assert stats["total_cases"] == 13
    assert "by_question_type" in stats
    assert stats["by_question_type"]["single_session_fact"] == 3
    assert stats["by_question_type"]["knowledge_update"] == 4
    assert stats["by_question_type"]["cross_session_preference"] == 2
    assert stats["by_question_type"]["user_identity"] == 3
    assert stats["by_question_type"]["multi_turn_context"] == 1

    assert stats["by_distance_type"]["semantic_similarity"] == 12
    assert stats["by_distance_type"]["exact_match"] == 1

    print("✅ test_dataset_stats 通过")


def test_green_set_file_exists() -> None:
    """验证实际 green_set.json 文件存在且有效"""
    green_path = Path(__file__).parent.parent / "data" / "evaluation" / "green_set.json"
    assert green_path.exists(), f"green_set.json 不存在: {green_path}"

    with open(green_path, "r") as f:
        data = json.load(f)

    assert len(data) == 13
    # 验证 JSON 格式
    required_fields = {"case_id", "question", "gold_answer", "question_type",
                       "context_sessions", "distance_type", "source", "notes"}
    for item in data:
        assert required_fields.issubset(item.keys()), \
            f"缺少字段: {required_fields - set(item.keys())} in {item['case_id']}"

    print(f"✅ green_set.json 存在且格式有效: {len(data)} 个用例")


if __name__ == "__main__":
    print("开始测试评估数据集...\n")

    test_evalcase_to_from_dict()
    test_build_green_set()
    test_dataset_save_load()
    test_dataset_stats()
    test_green_set_file_exists()

    print("\n" + "=" * 50)
    print("所有测试通过！✅")
    print("=" * 50)
