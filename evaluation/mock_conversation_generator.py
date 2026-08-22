"""
模拟对话生成器：生成用于评估测试的模拟对话数据

复刻 akashic LongMemEval 的 haystack 概念：
  大量无关会话构成检索噪声，agent 需主动用 recall_memory + fetch_messages 取证。
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


class MockConversationGenerator:
    """模拟对话生成器"""

    def __init__(self, output_path: str = "./data/evaluation/mock_conversations.jsonl") -> None:
        self.output_path = Path(output_path)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

    def generate(self) -> list[dict[str, Any]]:
        """生成 15 条模拟对话（原 6 条 + 9 条新主题）"""

        from uuid import uuid4 as _uuid4
        now = datetime.now(timezone.utc).isoformat

        return [
            # ═══ 原有 6 条（咖啡/茶 + 程序员） ═══
            {
                "session_id": str(_uuid4()), "timestamp": now(), "user_id": 1001, "chat_id": 1001,
                "messages": [
                    {"role": "user", "content": "我喜欢喝咖啡，尤其是拿铁"},
                    {"role": "assistant", "content": "好的，我记住了你喜欢喝拿铁！"},
                ],
            },
            {
                "session_id": str(_uuid4()), "timestamp": now(), "user_id": 1001, "chat_id": 1001,
                "messages": [
                    {"role": "user", "content": "我渴了，有什么推荐的吗？"},
                    {"role": "assistant", "content": "根据你的喜好，我推荐来一杯拿铁！"},
                ],
            },
            {
                "session_id": str(_uuid4()), "timestamp": now(), "user_id": 1001, "chat_id": 1001,
                "messages": [
                    {"role": "user", "content": "最近我戒了咖啡，改喝茶了"},
                    {"role": "assistant", "content": "明白了，你已经戒掉咖啡，现在改喝茶了。"},
                ],
            },
            {
                "session_id": str(_uuid4()), "timestamp": now(), "user_id": 1002, "chat_id": 1002,
                "messages": [
                    {"role": "user", "content": "我是个程序员，最常用的编程语言是 Python"},
                    {"role": "assistant", "content": "好的，Python 是个很棒的语言！"},
                ],
            },
            {
                "session_id": str(_uuid4()), "timestamp": now(), "user_id": 1002, "chat_id": 1002,
                "messages": [
                    {"role": "user", "content": "最近在写一个新项目，有什么建议吗？"},
                    {"role": "assistant", "content": "既然你用 Python，可以考虑用 Django 或者 FastAPI 做后端框架。"},
                ],
            },
            {
                "session_id": str(_uuid4()), "timestamp": now(), "user_id": 1003, "chat_id": 1003,
                "messages": [
                    {"role": "user", "content": "我的生日是 6 月 15 号"},
                    {"role": "assistant", "content": "好的，我记住了你的生日是 6 月 15 号！"},
                ],
            },

            # ═══ 新增 9 条：音乐、居住地、学习、食物、旅行、否定偏好 ═══
            # 7. 音乐偏好
            {
                "session_id": str(_uuid4()), "timestamp": now(), "user_id": 1001, "chat_id": 1001,
                "messages": [
                    {"role": "user", "content": "我特别喜欢听爵士乐，尤其是 Miles Davis"},
                    {"role": "assistant", "content": "Miles Davis 是爵士乐传奇！我记住了你喜欢爵士乐。"},
                ],
            },
            # 8. 否定偏好
            {
                "session_id": str(_uuid4()), "timestamp": now(), "user_id": 1001, "chat_id": 1001,
                "messages": [
                    {"role": "user", "content": "我不太喜欢摇滚乐，太吵了"},
                    {"role": "assistant", "content": "了解了，你不喜欢摇滚乐。喜欢安静的爵士风格。"},
                ],
            },
            # 9. 居住地 + 工作地
            {
                "session_id": str(_uuid4()), "timestamp": now(), "user_id": 1001, "chat_id": 1001,
                "messages": [
                    {"role": "user", "content": "我住在北京，但公司在杭州，经常两地跑"},
                    {"role": "assistant", "content": "记住了，你住在北京，工作在杭州，经常出差。"},
                ],
            },
            # 10. 学习新技能
            {
                "session_id": str(_uuid4()), "timestamp": now(), "user_id": 1002, "chat_id": 1002,
                "messages": [
                    {"role": "user", "content": "我最近在学 Rust，之前主要用 Python 和 Go"},
                    {"role": "assistant", "content": "Rust 是个很好的选择！你已经有 Python 和 Go 的基础了。"},
                ],
            },
            # 11. 学习进度更新
            {
                "session_id": str(_uuid4()), "timestamp": now(), "user_id": 1002, "chat_id": 1002,
                "messages": [
                    {"role": "user", "content": "Rust 学了两周了，感觉 ownership 概念挺难的"},
                    {"role": "assistant", "content": "ownership 确实是 Rust 的难点，但掌握后就通了。坚持住！"},
                ],
            },
            # 12. 食物偏好
            {
                "session_id": str(_uuid4()), "timestamp": now(), "user_id": 1001, "chat_id": 1001,
                "messages": [
                    {"role": "user", "content": "我喜欢吃辣的，尤其是川菜"},
                    {"role": "assistant", "content": "川菜确实够味！我记住了你喜欢吃辣。"},
                ],
            },
            # 13. 旅行经历
            {
                "session_id": str(_uuid4()), "timestamp": now(), "user_id": 1001, "chat_id": 1001,
                "messages": [
                    {"role": "user", "content": "去年我去了一趟日本，特别喜欢京都"},
                    {"role": "assistant", "content": "京都很美！红叶季和樱花季都值得再去。"},
                ],
            },
            # 14. 偏好更新
            {
                "session_id": str(_uuid4()), "timestamp": now(), "user_id": 1001, "chat_id": 1001,
                "messages": [
                    {"role": "user", "content": "我换了手机，之前用 iPhone，现在用 Android 了"},
                    {"role": "assistant", "content": "收到，你的手机偏好已更新：iPhone → Android。"},
                ],
            },
            # 15. 计划/事件
            {
                "session_id": str(_uuid4()), "timestamp": now(), "user_id": 1002, "chat_id": 1002,
                "messages": [
                    {"role": "user", "content": "下个月有个 Rust 线下 meetup，我打算去参加"},
                    {"role": "assistant", "content": "太好了！线下交流对学 Rust 很有帮助。"},
                ],
            },
        ]

    def save_to_file(self) -> str:
        conversations = self.generate()
        with open(self.output_path, "w", encoding="utf-8") as f:
            for conv in conversations:
                f.write(json.dumps(conv, ensure_ascii=False) + "\n")
        print(f"Generated {len(conversations)} mock conversations to {self.output_path}")
        return str(self.output_path)

    def load_from_file(self) -> list[dict[str, Any]]:
        if not self.output_path.exists():
            return []
        conversations = []
        with open(self.output_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    conversations.append(json.loads(line))
        return conversations


if __name__ == "__main__":
    generator = MockConversationGenerator()
    generator.save_to_file()
