import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["TG_BOT_TOKEN"] = "test_token"
os.environ["DEEPSEEK_API_KEY"] = "test_deepseek_key"
os.environ["ALIYUN_DASHSCOPE_API_KEY"] = "test_aliyun_key"
os.environ["DATABASE_PATH"] = tempfile.mktemp(suffix=".db")

from agent.core.types import MemoryItem
from eval.retrieval_probe_runner import (
    ProbeCase,
    RetrievalStrategy,
    _build_parser,
    _gold_signal_terms,
    _hit_summary,
    _parse_strategy_specs,
    _rrf_fuse_probe,
    _validate_args,
)


def _memory(summary: str, *, source_ref: str = "session:9000:9000#msg:0-1") -> MemoryItem:
    return MemoryItem(
        id=uuid4(),
        user_id=9000,
        memory_type="profile",
        summary=summary,
        embedding=[0.1],
        status="active",
        source_ref=source_ref,
    )


def test_retrieval_probe_requires_fresh() -> None:
    parser = _build_parser()
    args = parser.parse_args(["--set", "green"])

    try:
        _validate_args(args)
    except SystemExit as exc:
        assert "--fresh" in str(exc)
    else:
        raise AssertionError("retrieval probe should require --fresh")
    print("test_retrieval_probe_requires_fresh: PASS")


def test_retrieval_probe_strategy_parser() -> None:
    parser = _build_parser()
    args = parser.parse_args(
        [
            "--fresh",
            "--strategies",
            "current,vector_only,keyword_only,no_aux",
            "--rrf-k",
            "30",
            "--keyword-weight",
            "0.5",
        ]
    )

    strategies = _parse_strategy_specs(args)

    assert [strategy.name for strategy in strategies] == [
        "current",
        "vector_only",
        "keyword_only",
        "no_aux",
    ]
    assert strategies[0].rrf_k == 30
    assert strategies[0].keyword_weight == 0.5
    assert strategies[1].use_keyword is False
    assert strategies[2].use_vector is False
    assert strategies[3].use_aux is False
    print("test_retrieval_probe_strategy_parser: PASS")


def test_retrieval_probe_rrf_promotes_dual_lane_hit() -> None:
    vector_only = _memory("用户家里有一台打印机")
    both = _memory("用户家打印机型号是 Brother HL-L2460DW")
    keyword_only = _memory("Brother HL-L2460DW 是用户家打印机型号")

    fused, scores, lanes = _rrf_fuse_probe(
        [vector_only, both],
        [both, keyword_only],
        strategy=RetrievalStrategy(name="current", rrf_k=60),
    )

    assert fused[0].id == both.id
    assert scores[str(both.id)] > scores[str(vector_only.id)]
    assert lanes[str(both.id)] == ["vector", "keyword"]
    print("test_retrieval_probe_rrf_promotes_dual_lane_hit: PASS")


def test_retrieval_probe_hit_summary_uses_trace_expectations() -> None:
    case = ProbeCase(
        case_id="badcase-printer",
        question="我家打印机是什么型号？",
        gold_answer="你家打印机型号是 Brother HL-L2460DW。",
        trace_expectations={
            "expected_source_refs": ["session:9000:9000#msg:2-3"],
            "active_contains": ["用户家里打印机型号是 Brother HL-L2460DW"],
        },
    )
    payload = [
        {
            "rank": 1,
            "id": "noise",
            "summary": "用户今天午饭吃了番茄牛腩",
            "status": "active",
            "source_ref": "session:9000:9000#msg:0-1",
        },
        {
            "rank": 2,
            "id": "target",
            "summary": "用户家里打印机型号是 Brother HL-L2460DW",
            "status": "active",
            "source_ref": "session:9000:9000#msg:2-3",
        },
    ]

    hit = _hit_summary(case, payload)

    assert hit["best_rank"] == 2
    assert hit["hit_at_1"] is False
    assert hit["hit_at_3"] is True
    assert hit["matches"][0]["id"] == "target"
    print("test_retrieval_probe_hit_summary_uses_trace_expectations: PASS")


def test_retrieval_probe_gold_signal_terms_strip_common_verbs() -> None:
    assert "拿铁" in _gold_signal_terms("你喜欢喝拿铁")
    assert "杭州" in _gold_signal_terms("你的公司在杭州")
    print("test_retrieval_probe_gold_signal_terms_strip_common_verbs: PASS")


def main() -> None:
    test_retrieval_probe_requires_fresh()
    test_retrieval_probe_strategy_parser()
    test_retrieval_probe_rrf_promotes_dual_lane_hit()
    test_retrieval_probe_hit_summary_uses_trace_expectations()
    test_retrieval_probe_gold_signal_terms_strip_common_verbs()
    print("\nAll retrieval probe runner tests passed!")


if __name__ == "__main__":
    main()
