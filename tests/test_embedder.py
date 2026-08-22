import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

# Set test environment variables
os.environ["TG_BOT_TOKEN"] = "test_token"
os.environ["DEEPSEEK_API_KEY"] = "test_deepseek_key"
os.environ["ALIYUN_DASHSCOPE_API_KEY"] = "test_aliyun_key"
os.environ["DATABASE_PATH"] = "/tmp/test.db"

from memory.embedder import Embedder


async def test_embed_success():
    """Test successful embedding generation."""
    mock_response = MagicMock()
    mock_response.data = [MagicMock(embedding=[0.1, 0.2, 0.3, 0.4])]

    with patch("memory.embedder.AsyncOpenAI") as MockAsyncOpenAI:
        mock_client = AsyncMock()
        mock_client.embeddings.create = AsyncMock(return_value=mock_response)
        MockAsyncOpenAI.return_value = mock_client

        embedder = Embedder()
        result = await embedder.embed("hello world")

        assert result == [0.1, 0.2, 0.3, 0.4]
        mock_client.embeddings.create.assert_called_once_with(
            model="text-embedding-v3",
            input="hello world",
        )
        print("test_embed_success: PASS")


async def test_embed_retry_on_failure():
    """Test retry on API failure."""
    mock_response = MagicMock()
    mock_response.data = [MagicMock(embedding=[0.5, 0.6])]

    with patch("memory.embedder.AsyncOpenAI") as MockAsyncOpenAI:
        mock_client = AsyncMock()
        # First call fails, second succeeds
        mock_client.embeddings.create = AsyncMock(
            side_effect=[Exception("API error"), mock_response]
        )
        MockAsyncOpenAI.return_value = mock_client

        embedder = Embedder()
        result = await embedder.embed("test")

        assert result == [0.5, 0.6]
        assert mock_client.embeddings.create.call_count == 2
        print("test_embed_retry_on_failure: PASS")


async def test_embed_failure_after_retry():
    """Test failure after retry exhaustion."""
    with patch("memory.embedder.AsyncOpenAI") as MockAsyncOpenAI:
        mock_client = AsyncMock()
        mock_client.embeddings.create = AsyncMock(side_effect=Exception("Persistent error"))
        MockAsyncOpenAI.return_value = mock_client

        embedder = Embedder()

        try:
            await embedder.embed("test")
            assert False, "Should have raised exception"
        except Exception as e:
            assert str(e) == "Persistent error"
            assert mock_client.embeddings.create.call_count == 2
        print("test_embed_failure_after_retry: PASS")


async def main():
    await test_embed_success()
    await test_embed_retry_on_failure()
    await test_embed_failure_after_retry()
    print("\nAll embedder tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
