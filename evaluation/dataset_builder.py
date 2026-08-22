"""
评估数据集构建器：定义 EvalCase 数据结构，管理绿灯/红灯集的存取

数据流：
  raw_conversations.jsonl (原料)
    → 手工标注 → EvalCase 列表
    → green_set.json (正确回答的用例，基准)
    → red_set.json   (错误回答的用例，改进目标)

=== EvalCase 字段说明 ===

case_id (str)
  用例唯一标识，如 "green-001"、"red-003"。
  前缀 green/red 表示该用例属于绿灯集还是红灯集。

question (str)
  要测试的用户问题。这是实际会发送给 Bot 的文本。
  注意：问题本身可能不包含足够上下文，Bot 需要从记忆中检索才能正确回答。

gold_answer (str)
  标准答案。绿灯集 = Bot 当前回答已匹配此答案。
  红灯集 = Bot 当前回答与此不符，这是期望的目标答案。

question_type (QuestionType)
  问题类型，决定评估什么记忆能力：
  - single_session_fact:    单会话事实回忆 —— 信息在同一会话中给出和提问
  - cross_session_preference: 跨会话偏好记忆 —— 偏好信息在会话 A 给出，在会话 B 提问
  - knowledge_update:       知识更新 —— 信息被更新后，验证 Bot 记住的是最新值
  - user_identity:          用户身份 —— 记住用户的基本属性（职业、技能等）
  - multi_turn_context:     多轮上下文 —— 需要理解会话中的多个轮次

context_sessions (list[str])
  为该问题提供上下文的会话 ID 列表。
  评估时，这些会话中的记忆应该被 RAG 系统检索到。
  对应于 raw_conversations.jsonl 或 mock_conversations.jsonl 中的 session_id。

distance_type (DistanceType)
  答案评估方式：
  - exact_match:      精确匹配 —— 适用于日期、数字等确定性答案
  - semantic_similarity: 语义相似度 —— 适用于自然语言回答（用 embedding 比较）
  - llm_judge:        LLM 裁判 —— 用另一个 LLM 判断答案是否正确

source (str)
  数据来源："mock"（模拟数据）| "real"（真实对话）| "manual"（手工构造）

notes (str)
  标注者的备注，说明该用例的评估要点或注意事项。
"""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
import json
import logging

logger = logging.getLogger(__name__)


class QuestionType(str, Enum):
    """记忆问题类型 —— 不同测试维度"""
    SINGLE_SESSION_FACT = "single_session_fact"
    CROSS_SESSION_PREFERENCE = "cross_session_preference"
    KNOWLEDGE_UPDATE = "knowledge_update"
    USER_IDENTITY = "user_identity"
    MULTI_TURN_CONTEXT = "multi_turn_context"


class DistanceType(str, Enum):
    """答案评估方式"""
    EXACT_MATCH = "exact_match"
    SEMANTIC_SIMILARITY = "semantic_similarity"
    LLM_JUDGE = "llm_judge"


@dataclass
class EvalCase:
    """
    单个评估用例。

    表示一个可测试的 RAG 场景：
    「给定 context_sessions 中的上下文记忆，
      当用户问 question 时，
      Bot 应该回答 gold_answer」

    评估流程（第3步会实现）：
      1. 将 context_sessions 对应的记忆加载到 Bot 的 MemoryStore
      2. 向 Bot 发送 question
      3. 获取 Bot 的实际回答
      4. 用 distance_type 指定的方式比较 实际回答 vs gold_answer
    """
    case_id: str              # 用例唯一标识
    question: str             # 用户问题
    gold_answer: str          # 标准答案
    question_type: QuestionType  # 问题类型
    context_sessions: list[str]  # 上下文会话 ID 列表
    distance_type: DistanceType  # 评估方式
    source: str = "manual"    # 数据来源
    notes: str = ""           # 标注备注

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "question": self.question,
            "gold_answer": self.gold_answer,
            "question_type": self.question_type.value,
            "context_sessions": self.context_sessions,
            "distance_type": self.distance_type.value,
            "source": self.source,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvalCase":
        return cls(
            case_id=data["case_id"],
            question=data["question"],
            gold_answer=data["gold_answer"],
            question_type=QuestionType(data["question_type"]),
            context_sessions=data["context_sessions"],
            distance_type=DistanceType(data["distance_type"]),
            source=data.get("source", "manual"),
            notes=data.get("notes", ""),
        )


class EvalDataset:
    """
    评估数据集管理类。

    负责绿色/红色集的存取、统计、格式验证。
    数据存在 data/evaluation/ 目录下：
      - green_set.json: 正确答案用例（基准）
      - red_set.json:   错误答案用例（改进目标）
    """

    DEFAULT_GREEN_PATH = "green_set.json"
    DEFAULT_RED_PATH = "red_set.json"

    def __init__(self, data_dir: str = "./data/evaluation") -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # ── 保存 ──────────────────────────────────────────

    def save_green_set(
        self, cases: list[EvalCase], path: str | None = None
    ) -> str:
        """保存绿灯集到 JSON 文件。"""
        return self._save_cases(cases, path or self.DEFAULT_GREEN_PATH)

    def save_red_set(
        self, cases: list[EvalCase], path: str | None = None
    ) -> str:
        """保存红灯集到 JSON 文件。"""
        return self._save_cases(cases, path or self.DEFAULT_RED_PATH)

    def _save_cases(self, cases: list[EvalCase], filename: str) -> str:
        filepath = self.data_dir / filename
        data = [case.to_dict() for case in cases]
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("Saved %d cases to %s", len(cases), filepath)
        return str(filepath)

    # ── 加载 ──────────────────────────────────────────

    def load_green_set(
        self, path: str | None = None
    ) -> list[EvalCase]:
        """从 JSON 文件加载绿灯集。"""
        return self._load_cases(path or self.DEFAULT_GREEN_PATH)

    def load_red_set(
        self, path: str | None = None
    ) -> list[EvalCase]:
        """从 JSON 文件加载红灯集。"""
        return self._load_cases(path or self.DEFAULT_RED_PATH)

    def _load_cases(self, filename: str) -> list[EvalCase]:
        filepath = self.data_dir / filename
        if not filepath.exists():
            logger.warning("Eval dataset not found: %s", filepath)
            return []

        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        cases = [EvalCase.from_dict(item) for item in data]
        logger.info("Loaded %d cases from %s", len(cases), filepath)
        return cases

    # ── 统计 ──────────────────────────────────────────

    def stats(self, cases: list[EvalCase]) -> dict[str, Any]:
        """计算数据集统计信息。"""
        type_counts: dict[str, int] = {}
        distance_counts: dict[str, int] = {}
        source_counts: dict[str, int] = {}

        for case in cases:
            t = case.question_type.value
            type_counts[t] = type_counts.get(t, 0) + 1

            d = case.distance_type.value
            distance_counts[d] = distance_counts.get(d, 0) + 1

            s = case.source
            source_counts[s] = source_counts.get(s, 0) + 1

        return {
            "total_cases": len(cases),
            "by_question_type": type_counts,
            "by_distance_type": distance_counts,
            "by_source": source_counts,
        }

    def print_stats(self, cases: list[EvalCase], label: str = "Dataset") -> None:
        """打印可读的统计信息。"""
        s = self.stats(cases)
        print(f"\n=== {label} 统计 ===")
        print(f"  总用例数: {s['total_cases']}")
        print(f"  按问题类型:")
        for t, n in s["by_question_type"].items():
            print(f"    {t}: {n}")
        print(f"  按评估方式:")
        for d, n in s["by_distance_type"].items():
            print(f"    {d}: {n}")
        print(f"  按数据来源:")
        for src, n in s["by_source"].items():
            print(f"    {src}: {n}")


# ── 便捷函数 ──────────────────────────────────────────

def build_green_set_from_mock() -> list[EvalCase]:
    """
    基于 mock_conversations.jsonl 中的模拟数据，
    手工构建绿灯集（8 个用例）。

    Mock 数据中的会话 ID：
      session-1: 7b73622d — 用户说喜欢拿铁
      session-2: bf29645b — 用户渴了，Bot 推荐拿铁
      session-3: 6836bca0 — 用户戒咖啡改喝茶
      session-4: be93a503 — 用户是 Python 程序员

    覆盖类型：单会话事实 / 跨会话偏好 / 知识更新 / 用户身份
    """

    S1 = "f1fba2a1-072d-47f6-bddb-51beb8047e49"  # latte preference
    S2 = "3ccd551a-1aff-4ff3-8f92-7499eced9509"  # latte recommendation
    S3 = "0c2a114b-bf73-4096-836d-bc0cbc5a203f"  # coffee -> tea update
    S4 = "a3271ef7-8d02-428a-a2a6-98c4302a6d97"  # programmer / Python
    S5 = "33b53d36-8534-4382-8cc7-3a3cc3248d8f"  # project advice
    S6 = "0f4049c8-2eec-4453-a689-3d4063684afd"  # birthday
    S7 = "1a7a9c3f-8cc8-44ca-900f-3920e55fa065"  # jazz / Miles Davis
    S8 = "24cafc8a-bfbb-4e20-bc32-17f9ef6a1a1b"  # dislikes rock
    S9 = "619c1fe5-b459-45db-bf29-6d7d3206865b"  # Beijing / Hangzhou
    S12 = "6528cca5-82ba-4292-b3f5-d14d2d858555"  # spicy food
    S14 = "d30dedb8-919e-4571-83f6-5aa2e74b9f94"  # phone update

    return [
        EvalCase(
            case_id="green-001",
            question="我喜欢喝什么咖啡？",
            gold_answer="你喜欢喝拿铁",
            question_type=QuestionType.SINGLE_SESSION_FACT,
            context_sessions=[S1],
            distance_type=DistanceType.SEMANTIC_SIMILARITY,
            source="mock",
            notes="单会话事实回忆：用户在同一会话中表达了偏好，Bot 正确记住了",
        ),
        EvalCase(
            case_id="green-002",
            question="你知道我的咖啡偏好吗？说出来。",
            gold_answer="你喜欢喝拿铁",
            question_type=QuestionType.SINGLE_SESSION_FACT,
            context_sessions=[S1],
            distance_type=DistanceType.SEMANTIC_SIMILARITY,
            source="mock",
            notes="单会话事实回忆：确认偏好记忆的召回准确性",
        ),
        EvalCase(
            case_id="green-003",
            question="我渴了，有什么推荐的吗？",
            gold_answer="根据你的喜好，推荐拿铁",
            question_type=QuestionType.CROSS_SESSION_PREFERENCE,
            context_sessions=[S1, S2],
            distance_type=DistanceType.SEMANTIC_SIMILARITY,
            source="mock",
            notes="跨会话偏好记忆：偏好信息在 session-1，提问在 session-2。Bot 需要跨会话检索",
        ),
        EvalCase(
            case_id="green-004",
            question="我现在还喝咖啡吗？",
            gold_answer="你已经不喝咖啡了，改喝茶了",
            question_type=QuestionType.KNOWLEDGE_UPDATE,
            context_sessions=[S1, S3],
            distance_type=DistanceType.SEMANTIC_SIMILARITY,
            source="mock",
            notes="知识更新 — 关键用例：验证 Bot 使用了最新信息（茶）而非旧信息（咖啡）。这是 RAG 系统最容易出错的场景",
        ),
        EvalCase(
            case_id="green-005",
            question="我现在喜欢喝什么？",
            gold_answer="你现在喜欢喝茶",
            question_type=QuestionType.KNOWLEDGE_UPDATE,
            context_sessions=[S1, S3],
            distance_type=DistanceType.SEMANTIC_SIMILARITY,
            source="mock",
            notes="知识更新：与 green-004 互补，直接询问当前偏好",
        ),
        EvalCase(
            case_id="green-006",
            question="我是做什么工作的？",
            gold_answer="你是个程序员",
            question_type=QuestionType.USER_IDENTITY,
            context_sessions=[S4],
            distance_type=DistanceType.SEMANTIC_SIMILARITY,
            source="mock",
            notes="用户身份记忆：验证用户职业信息的检索",
        ),
        EvalCase(
            case_id="green-007",
            question="我用什么编程语言？",
            gold_answer="你主要用 Python",
            question_type=QuestionType.USER_IDENTITY,
            context_sessions=[S4],
            distance_type=DistanceType.SEMANTIC_SIMILARITY,
            source="mock",
            notes="用户身份记忆：验证编程语言偏好的事实提取",
        ),
        EvalCase(
            case_id="green-008",
            question="我以前说过我喜欢喝什么？全部告诉我。",
            gold_answer="你之前喜欢喝拿铁，后来改喝茶了",
            question_type=QuestionType.KNOWLEDGE_UPDATE,
            context_sessions=[S1, S3],
            distance_type=DistanceType.SEMANTIC_SIMILARITY,
            source="mock",
            notes="知识更新：需要同时召回旧偏好和新偏好，验证记忆的时序完整性",
        ),
        EvalCase(
            case_id="green-009",
            question="我的生日是哪天？",
            gold_answer="你的生日是6月15号",
            question_type=QuestionType.SINGLE_SESSION_FACT,
            context_sessions=[S6],
            distance_type=DistanceType.EXACT_MATCH,
            source="mock",
            notes="单会话精确匹配：日期类信息适合 exact_match。生日信息来自 mock 会话。",
        ),
        EvalCase(
            case_id="green-010",
            question="给我一个项目建议，用我擅长的技术栈。",
            gold_answer="你可以用 Python 的 Django 或 FastAPI 框架来做后端",
            question_type=QuestionType.MULTI_TURN_CONTEXT,
            context_sessions=[S4, S5],
            distance_type=DistanceType.SEMANTIC_SIMILARITY,
            source="mock",
            notes="多轮上下文：需要结合用户身份（Python 程序员）给出个性化建议，而不能是泛泛的答案",
        ),
        EvalCase(
            case_id="green-011",
            question="我喜欢听什么音乐人？",
            gold_answer="你喜欢听爵士乐，尤其是 Miles Davis",
            question_type=QuestionType.CROSS_SESSION_PREFERENCE,
            context_sessions=[S12, S8, S7, S9],
            distance_type=DistanceType.SEMANTIC_SIMILARITY,
            source="mock",
            notes="长 haystack + 干扰：食物、摇滚、居住地都是噪声，正确答案在爵士乐会话。用于逼近 LongMemEval 的 haystack 检索场景。",
        ),
        EvalCase(
            case_id="green-012",
            question="我的公司在哪个城市？",
            gold_answer="你的公司在杭州",
            question_type=QuestionType.USER_IDENTITY,
            context_sessions=[S7, S9, S12],
            distance_type=DistanceType.SEMANTIC_SIMILARITY,
            source="mock",
            notes="原文定位：问题需要从居住地/工作地同句中取具体城市，适合 recall 后 search_messages/fetch_messages 回源确认。",
        ),
        EvalCase(
            case_id="green-013",
            question="我现在用 iPhone 还是 Android？",
            gold_answer="你现在用 Android",
            question_type=QuestionType.KNOWLEDGE_UPDATE,
            context_sessions=[S1, S14],
            distance_type=DistanceType.SEMANTIC_SIMILARITY,
            source="mock",
            notes="知识更新：同一句里包含旧值 iPhone 和新值 Android，测试是否选择更新后的当前状态。",
        ),
    ]


if __name__ == "__main__":
    # 直接运行：构建绿灯集并保存
    logging.basicConfig(level=logging.INFO)

    dataset = EvalDataset()
    green_cases = build_green_set_from_mock()

    # 保存
    path = dataset.save_green_set(green_cases)
    print(f"\n绿灯集已保存: {path}")

    # 统计
    dataset.print_stats(green_cases, "绿灯集")

    # 验证加载
    loaded = dataset.load_green_set()
    assert len(loaded) == len(green_cases), f"加载数量不匹配: {len(loaded)} != {len(green_cases)}"
    print(f"\n✅ 加载验证通过: {len(loaded)} 个用例")
