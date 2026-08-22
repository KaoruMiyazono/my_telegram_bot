"""
测试对话日志记录器和模拟对话生成器
"""
import asyncio
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from evaluation.conversation_logger import ConversationLogger
from evaluation.mock_conversation_generator import MockConversationGenerator


async def test_mock_generator():
    """测试模拟对话生成器"""
    print("=" * 50)
    print("测试 1: 模拟对话生成器")
    print("=" * 50)

    generator = MockConversationGenerator()
    conversations = generator.generate()

    print(f"生成了 {len(conversations)} 条模拟对话")
    for i, conv in enumerate(conversations, 1):
        print(f"\n对话 {i}:")
        print(f"  用户 ID: {conv['user_id']}")
        print(f"  消息数: {len(conv['messages'])}")
        for msg in conv['messages']:
            role = msg['role']
            content = msg['content'][:40] + "..." if len(msg['content']) > 40 else msg['content']
            print(f"    [{role}]: {content}")

    # 保存到文件
    file_path = generator.save_to_file(6)
    print(f"\n已保存到文件: {file_path}")

    # 加载验证
    loaded = generator.load_from_file()
    print(f"从文件加载了 {len(loaded)} 条对话")


async def test_conversation_logger():
    """测试对话日志记录器"""
    print("\n" + "=" * 50)
    print("测试 2: 对话日志记录器")
    print("=" * 50)

    # 清理旧数据
    logger = ConversationLogger(log_dir="./data/evaluation")
    logger.clear_raw_conversations()

    # 启动 logger
    await logger.start()

    # 模拟一些 TurnCommittedEvent
    from uuid import uuid4
    from agent.core.types import TurnCommittedEvent, OutboundMessage

    test_events = [
        TurnCommittedEvent(
            turn_id=str(uuid4()),
            user_id=1001,
            inbound_content="我喜欢喝咖啡，尤其是拿铁",
            outbound_message=OutboundMessage(
                chat_id=1001,
                content="好的，我记住了你喜欢喝拿铁！",
                format="text",
            ),
            new_memory_ids=[str(uuid4())],
        ),
        TurnCommittedEvent(
            turn_id=str(uuid4()),
            user_id=1002,
            inbound_content="最近在写一个新项目，有什么建议吗？",
            outbound_message=OutboundMessage(
                chat_id=1002,
                content="既然你用 Python，可以考虑用 Django。",
                format="text",
            ),
            new_memory_ids=[str(uuid4())],
        ),
    ]

    # 触发事件（调用 event_bus.emit，然后 logger 会订阅）
    from agent.core.event_bus import EventBus

    event_bus = EventBus.get_instance()
    for event in test_events:
        await event_bus.emit("turn_committed", event=event)

    # 等待写入完成
    await asyncio.sleep(1)

    # 停止 logger
    await logger.stop()

    # 加载并验证
    conversations = logger.load_raw_conversations()
    print(f"Logger 记录了 {len(conversations)} 条对话")
    for conv in conversations:
        print(f"  Turn ID: {conv['turn_id'][:8]}...")
        print(f"  用户 ID: {conv['user_id']}")
        print(f"  回复: {conv['outbound_message']['content'][:40]}...")


async def test_full_workflow():
    """测试完整工作流：生成 → 记录 → 加载"""
    print("\n" + "=" * 50)
    print("测试 3: 完整工作流")
    print("=" * 50)

    # 1. 生成模拟数据
    generator = MockConversationGenerator()
    generator.save_to_file(4)
    print("1. 生成了 4 条模拟对话")

    # 2. 验证加载
    mock_convs = generator.load_from_file()
    print(f"2. 从文件加载了 {len(mock_convs)} 条对话")

    # 3. 测试 logger
    logger = ConversationLogger()
    logger.clear_raw_conversations()
    await logger.start()

    # 模拟事件
    from uuid import uuid4
    from agent.core.event_bus import EventBus
    from agent.core.types import TurnCommittedEvent, OutboundMessage

    event_bus = EventBus.get_instance()
    for i in range(3):
        await event_bus.emit(
            "turn_committed",
            event=TurnCommittedEvent(
                turn_id=str(uuid4()),
                user_id=2000 + i,
                inbound_content=f"模拟问题 {i + 1}",
                outbound_message=OutboundMessage(
                    chat_id=2000 + i,
                    content=f"模拟回复 {i + 1}",
                    format="text",
                ),
                new_memory_ids=[str(uuid4())],
            ),
        )

    await asyncio.sleep(1)
    await logger.stop()

    raw_convs = logger.load_raw_conversations()
    print(f"3. Logger 记录了 {len(raw_convs)} 条原始对话")

    print("\n✅ 完整工作流测试通过！")


async def main():
    """运行所有测试"""
    print("开始测试对话日志系统...\n")

    await test_mock_generator()
    await test_conversation_logger()
    await test_full_workflow()

    print("\n" + "=" * 50)
    print("所有测试完成！")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
