from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

McpTransport = Literal["stdio", "streamable_http"]
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEADER_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")


@dataclass(frozen=True)
class McpServerSpec:
    """Validated, non-secret configuration for one MCP server."""

    name: str
    transport: McpTransport
    enabled: bool = False
    command: str = ""
    args: tuple[str, ...] = ()
    cwd: str | None = None
    url: str = ""
    env_refs: dict[str, str] = field(default_factory=dict)
    header_refs: dict[str, str] = field(default_factory=dict)
    connect_timeout: float = 20.0
    call_timeout: float = 30.0

    def validate(
        self,
        *,
        allowed_commands: set[str],
        allow_loopback_http: bool,
    ) -> None:
        if not _NAME_RE.fullmatch(self.name):
            raise ValueError(
                "MCP server name must match ^[a-z][a-z0-9_-]{0,31}$"
            )
        if self.connect_timeout <= 0 or self.call_timeout <= 0:
            raise ValueError("MCP timeouts must be greater than zero")
        self._validate_secret_refs()
        if self.transport == "stdio":
            self._validate_stdio(allowed_commands)
            return
        if self.transport == "streamable_http":
            self._validate_http(allow_loopback_http)
            return
        raise ValueError(f"Unsupported MCP transport: {self.transport}")

    def resolved_env(self) -> dict[str, str]:
        return _resolve_refs(self.env_refs)

    def resolved_headers(self) -> dict[str, str]:
        return _resolve_refs(self.header_refs)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "transport": self.transport,
            "enabled": self.enabled,
            "command": self.command,
            "args": list(self.args),
            "cwd": self.cwd,
            "url": self.url,
            "env_refs": dict(self.env_refs),
            "header_refs": dict(self.header_refs),
            "connect_timeout": self.connect_timeout,
            "call_timeout": self.call_timeout,
        }

    def _validate_stdio(self, allowed_commands: set[str]) -> None:
        if not self.command or any(char.isspace() for char in self.command):
            raise ValueError("stdio MCP command must be one executable without spaces")
        command_name = Path(self.command).name
        if command_name not in allowed_commands:
            raise ValueError(f"MCP stdio command is not allowlisted: {command_name}")
        if self.url or self.header_refs:
            raise ValueError("stdio MCP cannot define url or header_refs")
        if self.cwd is not None and not Path(self.cwd).expanduser().is_dir():
            raise ValueError(f"MCP cwd does not exist: {self.cwd}")

    def _validate_http(self, allow_loopback_http: bool) -> None:
        parsed = urlsplit(self.url)
        if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("MCP HTTP URL must be an absolute URL without credentials/fragment")
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and loopback and allow_loopback_http
        ):
            raise ValueError("MCP HTTP URL must use HTTPS (HTTP is loopback-only)")
        if self.command or self.args or self.cwd or self.env_refs:
            raise ValueError("streamable_http MCP cannot define stdio fields")
        for header in self.header_refs:
            if not _HEADER_RE.fullmatch(header):
                raise ValueError(f"Invalid MCP HTTP header name: {header}")

    def _validate_secret_refs(self) -> None:
        for target, source in [*self.env_refs.items(), *self.header_refs.items()]:
            if not _ENV_RE.fullmatch(source):
                raise ValueError(f"Invalid environment-variable reference: {source}")
            if not target:
                raise ValueError("MCP environment/header target cannot be empty")


def load_mcp_specs(
    path: str | Path,
    *,
    default_connect_timeout: float = 20.0,
) -> dict[str, McpServerSpec]:
    config_path = Path(path).expanduser()
    if not config_path.exists():
        return {}
    payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    raw_servers = payload.get("servers", {})
    if not isinstance(raw_servers, dict):
        raise ValueError("mcp_servers.toml must contain a [servers] table")
    specs: dict[str, McpServerSpec] = {}
    for name, raw in raw_servers.items():
        if not isinstance(raw, dict):
            raise ValueError(f"MCP server config must be a table: {name}")
        transport = str(raw.get("transport") or "")
        if transport == "http":
            transport = "streamable_http"
        spec = McpServerSpec(
            name=str(name),
            transport=transport,  # type: ignore[arg-type]
            enabled=bool(raw.get("enabled", False)),
            command=str(raw.get("command") or ""),
            args=tuple(str(item) for item in raw.get("args", [])),
            cwd=str(raw["cwd"]) if raw.get("cwd") else None,
            url=str(raw.get("url") or ""),
            env_refs=_string_map(raw.get("env_refs")),
            header_refs=_string_map(raw.get("header_refs")),
            connect_timeout=float(raw.get("connect_timeout", default_connect_timeout)),
            call_timeout=float(raw.get("call_timeout", 30.0)),
        )
        specs[spec.name] = spec
    return specs


def _resolve_refs(refs: dict[str, str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for target, source in refs.items():
        value = os.environ.get(source)
        if value is None:
            raise ValueError(f"Required environment variable is missing: {source}")
        resolved[target] = value
    return resolved


def _string_map(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("MCP secret references must be a table")
    return {str(key): str(item) for key, item in value.items()}
