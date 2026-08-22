"""
配置加载模块
从 config.toml 读取配置，支持 ${ENV_VAR} 格式的环境变量插值。
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


def _resolve_env_var(value: str) -> str:
    """解析 ${ENV_VAR} 格式的环境变量"""
    pattern = r'\$\{([^}]+)\}'

    def replace_var(match: re.Match) -> str:
        var_name = match.group(1)
        return os.environ.get(var_name, '')

    return re.sub(pattern, replace_var, value)


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    api_key: str
    base_url: str
    model: str
    max_tokens: int = 8192
    temperature: float = 0.7


@dataclass(frozen=True)
class MemoryEmbeddingConfig:
    model: str
    api_key: str
    base_url: str
    dimension: int = 1536


@dataclass(frozen=True)
class MemoryConfig:
    enabled: bool
    db_path: str
    embedding: MemoryEmbeddingConfig
    memory_window: int = 40


@dataclass(frozen=True)
class TelegramChannelConfig:
    token: str
    allow_from: list[str]
    webhook_url: str | None = None
    use_polling: bool = True


@dataclass(frozen=True)
class ChannelsConfig:
    telegram: TelegramChannelConfig


@dataclass(frozen=True)
class AgentConfig:
    system_prompt: str
    max_iterations: int = 40
    memory_window: int = 40


@dataclass(frozen=True)
class Config:
    llm: LLMConfig
    memory: MemoryConfig
    channels: ChannelsConfig
    agent: AgentConfig

    @classmethod
    def load(cls, path: str | Path = "config.toml") -> "Config":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在: {path}")

        with open(path, "rb") as f:
            data = tomllib.load(f)

        # 解析 LLM 配置
        llm_dict = _as_dict(data.get("llm", {}))
        llm_main = _as_dict(llm_dict.get("main", {}))

        # 解析 Memory 配置
        memory_dict = _as_dict(data.get("memory", {}))
        embedding_dict = _as_dict(memory_dict.get("embedding", {}))

        # 解析 Channels 配置
        channels_dict = _as_dict(data.get("channels", {}))
        telegram_dict = _as_dict(channels_dict.get("telegram", {}))

        # 解析 Agent 配置
        agent_dict = _as_dict(data.get("agent", {}))

        # 解析 provider（兼容旧格式）
        provider = str(llm_dict.get("provider", data.get("provider", "openai")))

        return cls(
            llm=LLMConfig(
                provider=provider,
                api_key=_resolve_env_var(
                    str(llm_main.get("api_key", data.get("api_key", "")))
                ),
                base_url=str(llm_main.get("base_url", "")),
                model=str(llm_main.get("model", "gpt-4o-mini")),
                max_tokens=int(llm_main.get("max_tokens", 8192)),
                temperature=float(llm_main.get("temperature", 0.7)),
            ),
            memory=MemoryConfig(
                enabled=bool(memory_dict.get("enabled", True)),
                db_path=str(memory_dict.get("db_path", "memory/memory.db")),
                embedding=MemoryEmbeddingConfig(
                    model=str(embedding_dict.get("model", "text-embedding-3-small")),
                    api_key=_resolve_env_var(
                        str(embedding_dict.get("api_key", llm_main.get("api_key", "")))
                    ),
                    base_url=str(embedding_dict.get("base_url", llm_main.get("base_url", ""))),
                    dimension=int(embedding_dict.get("dimension", 1536)),
                ),
                memory_window=int(memory_dict.get("memory_window", 40)),
            ),
            channels=ChannelsConfig(
                telegram=TelegramChannelConfig(
                    token=_resolve_env_var(str(telegram_dict.get("token", ""))),
                    allow_from=list(telegram_dict.get("allow_from", [])),
                    webhook_url=telegram_dict.get("webhook_url"),
                    use_polling=bool(telegram_dict.get("use_polling", True)),
                ),
            ),
            agent=AgentConfig(
                system_prompt=str(
                    agent_dict.get(
                        "system_prompt",
                        "You are a helpful AI assistant with long-term memory.",
                    )
                ),
                max_iterations=int(agent_dict.get("max_iterations", 40)),
                memory_window=int(agent_dict.get("memory_window", 40)),
            ),
        )
