from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram Bot
    TG_BOT_TOKEN: str

    # DeepSeek LLM
    DEEPSEEK_API_KEY: str
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1"
    LLM_MODEL: str = "deepseek-chat"

    # Aliyun Embedding
    ALIYUN_DASHSCOPE_API_KEY: str
    EMBEDDING_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    EMBEDDING_MODEL: str = "text-embedding-v3"

    # Database
    DATABASE_PATH: str = "./data/memory.db"

    # Proxy (for Telegram API in China)
    HTTP_PROXY: str | None = None

    # Web search and fetch
    WEB_SEARCH_ENDPOINT: str = "https://mcp.exa.ai/mcp"
    SEARCH_API_KEY: str | None = None
    WEB_PROXY: str | None = None
    WEB_SEARCH_TIMEOUT: float = 25.0
    WEB_SEARCH_MAX_RESULTS: int = 5
    WEB_FETCH_TIMEOUT: float = 20.0
    WEB_FETCH_MAX_BYTES: int = 5 * 1024 * 1024
    WEB_FETCH_MAX_CHARS: int = 15_000
    WEB_FETCH_MAX_REDIRECTS: int = 3


settings = Settings()
