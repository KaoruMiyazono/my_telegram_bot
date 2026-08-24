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
    LLM_CONTEXT_WINDOW: int = 64_000
    LLM_OUTPUT_RESERVE: int = 4_096
    LLM_CONTEXT_SOFT_LIMIT_RATIO: float = 0.74
    LLM_CONTEXT_KEEP_RECENT_TOKENS: int = 20_000
    LLM_COMPACTION_SUMMARY_MAX_TOKENS: int = 8_192
    TOOL_INITIAL_MAX_SCHEMAS: int = 8
    TOOL_INITIAL_SCHEMA_CHAR_BUDGET: int = 12_000
    TOOL_SESSION_LRU_SIZE: int = 4
    MEMORY_OPTIMIZER_ENABLED: bool = True
    MEMORY_OPTIMIZER_INTERVAL_SECONDS: float = 900.0

    # M8 proactive runtime (disabled until a target is configured explicitly)
    PROACTIVE_ENABLED: bool = False
    PROACTIVE_MODE: str = "shadow"
    PROACTIVE_CHANNEL: str = "telegram"
    PROACTIVE_CHAT_ID: str = ""
    PROACTIVE_USER_ID: str = ""
    PROACTIVE_THRESHOLD: float = 0.6
    PROACTIVE_INTERVAL_SECONDS: int = 300
    PROACTIVE_BLOCKED_INTERVAL_SECONDS: int = 60
    PROACTIVE_EMPTY_INTERVAL_SECONDS: int = 600
    PROACTIVE_COOLDOWN_SECONDS: int = 3600
    PROACTIVE_DAILY_LIMIT: int = 3
    PROACTIVE_QUIET_START_HOUR: int = 22
    PROACTIVE_QUIET_END_HOUR: int = 8
    PROACTIVE_TIMEZONE: str = "Asia/Shanghai"
    PROACTIVE_URGENT_BYPASS_BUSY: bool = False
    PROACTIVE_URGENT_BYPASS_COOLDOWN: bool = True
    PROACTIVE_URGENT_BYPASS_QUIET: bool = True
    PROACTIVE_URGENT_BYPASS_DAILY_LIMIT: bool = False
    PROACTIVE_SOURCE_CONFIG_PATH: str = "./config/proactive_sources.toml"

    # Generic MCP runtime
    MCP_CONFIG_PATH: str = "./config/mcp_servers.toml"
    MCP_STDIO_COMMAND_ALLOWLIST: str = "python,python3,node,npx,uvx"
    MCP_CONNECT_TIMEOUT: float = 20.0
    MCP_DRAIN_TIMEOUT: float = 10.0
    MCP_ALLOW_LOOPBACK_HTTP: bool = True

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
