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


settings = Settings()
