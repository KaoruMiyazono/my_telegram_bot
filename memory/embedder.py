import asyncio

from openai import AsyncOpenAI

from config.settings import settings


#  文本转换成向量的类，主要是调用openai的api来生成embedding向量
class Embedder:
    def __init__(self) -> None:
        self.client = AsyncOpenAI(
            #  定义api和模型的参数，api_key是从配置文件中读取的，base_url是aliyun的embedding服务地址，timeout是请求超时时间
            api_key=settings.ALIYUN_DASHSCOPE_API_KEY,
            base_url=settings.EMBEDDING_BASE_URL,
            timeout=30.0,
        )
        self.model = settings.EMBEDDING_MODEL

    async def embed(self, text: str) -> list[float]:
        """Generate embedding for the given text with retry."""
        for attempt in range(2):
            try:
                response = await self.client.embeddings.create(
                    model=self.model,
                    input=text,
                )
                return response.data[0].embedding
            except Exception as e:
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                raise
        return []  # unreachable but satisfies type checker

    async def close(self) -> None:
        await self.client.close()
