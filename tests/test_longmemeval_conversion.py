import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from eval.longmemeval.convert_mock_cases import convert_mock_cases


def test_convert_mock_cases_to_longmemeval_schema() -> None:
    instances = convert_mock_cases(set_names=["green", "red"])

    assert len(instances) == 18
    first = instances[0]
    assert first["question_id"] == "green-001"
    assert first["question_type"] == "single-session-user"
    assert first["question"] == "我喜欢喝什么咖啡？"
    assert first["answer"] == "你喜欢喝拿铁"
    assert first["haystack_session_ids"] == [
        "f1fba2a1-072d-47f6-bddb-51beb8047e49"
    ]
    assert first["answer_session_ids"] == first["haystack_session_ids"]
    assert first["haystack_dates"]
    assert first["question_date"]
    assert first["haystack_sessions"] == [
        [
            {"role": "user", "content": "我喜欢喝咖啡，尤其是拿铁"},
            {"role": "assistant", "content": "好的，我记住了你喜欢喝拿铁！"},
        ]
    ]

    by_id = {item["question_id"]: item for item in instances}
    assert by_id["green-004"]["question_type"] == "knowledge-update"
    assert by_id["green-003"]["question_type"] == "single-session-preference"
    assert by_id["green-011"]["question_type"] == "single-session-preference"
    assert by_id["green-011"]["haystack_session_ids"] == [
        "6528cca5-82ba-4292-b3f5-d14d2d858555",
        "24cafc8a-bfbb-4e20-bc32-17f9ef6a1a1b",
        "1a7a9c3f-8cc8-44ca-900f-3920e55fa065",
        "619c1fe5-b459-45db-bf29-6d7d3206865b",
    ]
    assert by_id["green-013"]["question_type"] == "knowledge-update"
    assert by_id["red-005"]["question_type"] == "single-session-preference"
    assert by_id["red-002"]["metadata"]["original_question_type"] == "multi_turn_context"


def test_generated_longmemeval_file_is_current() -> None:
    path = Path(__file__).parent.parent / "eval" / "longmemeval" / "data" / "local_mock_longmemeval.json"
    assert path.exists(), f"missing generated dataset: {path}"

    generated = json.loads(path.read_text(encoding="utf-8"))
    expected = convert_mock_cases(set_names=["green", "red"])
    assert generated == expected


if __name__ == "__main__":
    test_convert_mock_cases_to_longmemeval_schema()
    print("test_convert_mock_cases_to_longmemeval_schema passed")
    test_generated_longmemeval_file_is_current()
    print("test_generated_longmemeval_file_is_current passed")
