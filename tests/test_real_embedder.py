import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

load_dotenv()

os.environ.setdefault("TG_BOT_TOKEN", "test_token")
os.environ.setdefault("DEEPSEEK_API_KEY", "test_deepseek_key")
os.environ["DATABASE_PATH"] = "/tmp/test.db"

if not os.getenv("ALIYUN_DASHSCOPE_API_KEY"):
    raise SystemExit("请先在 .env 中配置 ALIYUN_DASHSCOPE_API_KEY")

from memory.embedder import Embedder


async def main():
    embedder = Embedder()
    result = await embedder.embed("你好，世界")
    print(f"Embedding dimension: {len(result)}")
    print(f"First 5 values: {result[:5]}")
    print("Success!")


if __name__ == "__main__":
    asyncio.run(main())
