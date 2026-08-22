from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from agent.tools.base import Tool

DEFAULT_EXA_MCP_URL = "https://mcp.exa.ai/mcp"
_RESULT_SEPARATOR = re.compile(r"\n\s*---\s*\n")


@dataclass(frozen=True)
class WebSearchConfig:
    endpoint: str = DEFAULT_EXA_MCP_URL
    api_key: str | None = None
    proxy: str | None = None
    timeout_s: float = 25.0
    max_results: int = 5


class WebSearchClient:
    """Call Exa's public MCP endpoint and normalize its text response."""

    def __init__(
        self,
        config: WebSearchConfig | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config or WebSearchConfig()
        self._client = client

    async def search(
        self,
        *,
        query: str,
        num_results: int | None = None,
        livecrawl: str = "fallback",
        search_type: str = "auto",
    ) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise ValueError("搜索关键词不能为空")
        configured_max = max(1, int(self.config.max_results))
        limit = max(1, min(int(num_results or configured_max), configured_max))
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "web_search_exa",
                "arguments": {
                    "query": query,
                    "numResults": limit,
                    "livecrawl": livecrawl,
                    "type": search_type,
                },
            },
        }
        headers = {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        }
        if self.config.api_key:
            headers["x-api-key"] = self.config.api_key

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=self.config.timeout_s,
            proxy=self.config.proxy,
            trust_env=False,
        )
        try:
            response = await client.post(self.config.endpoint, json=payload, headers=headers)
            response.raise_for_status()
            rpc_payload = _parse_mcp_response(response.text)
        finally:
            if owns_client:
                await client.aclose()

        if isinstance(rpc_payload.get("error"), dict):
            message = str(rpc_payload["error"].get("message") or "MCP 搜索失败")
            raise RuntimeError(message)

        text = _extract_text_content(rpc_payload)
        results = _normalize_exa_results(text, limit=limit)
        return {
            "query": query,
            "count": len(results),
            "results": results,
            "provider": "exa",
            "untrusted_external_content": True,
        }


def build_web_search_tool(client: WebSearchClient) -> Tool:
    max_results = max(1, int(client.config.max_results))

    async def _handler(args: dict[str, Any], ctx: Any) -> str:
        result = await client.search(
            query=str(args.get("query") or ""),
            num_results=args.get("num_results"),
            livecrawl=str(args.get("livecrawl") or "fallback"),
            search_type=str(args.get("type") or "auto"),
        )
        return json.dumps(result, ensure_ascii=False)

    return Tool(
        name="web_search",
        description=(
            "搜索当前互联网信息，返回标题、摘要、发布时间和 URL。"
            "新闻、价格、版本、人物现职、比赛结果等可能变化的信息必须先使用本工具；"
            "需要核实正文时继续调用 web_fetch。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 500,
                    "description": "准确、简洁的搜索关键词",
                },
                "num_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": max_results,
                    "description": f"结果数量，最大 {max_results}",
                },
                "livecrawl": {
                    "type": "string",
                    "enum": ["fallback", "preferred"],
                    "description": "fallback 使用缓存优先；preferred 优先获取最新页面",
                },
                "type": {
                    "type": "string",
                    "enum": ["auto", "fast", "deep"],
                    "description": "搜索深度，默认 auto",
                },
            },
            "required": ["query"],
        },
        handler=_handler,
        timeout_s=max(1.0, client.config.timeout_s + 2.0),
        idempotent=True,
        output_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "count": {"type": "integer"},
                "results": {"type": "array"},
            },
            "required": ["query", "count", "results"],
        },
    )


def _parse_mcp_response(text: str) -> dict[str, Any]:
    raw = text.strip()
    if not raw:
        raise RuntimeError("搜索服务返回空响应")
    if raw.startswith("{"):
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise RuntimeError("搜索服务返回格式不正确")
        return payload
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        payload = json.loads(data)
        if isinstance(payload, dict):
            return payload
    raise RuntimeError("无法解析搜索服务响应")


def _extract_text_content(payload: dict[str, Any]) -> str:
    result = payload.get("result")
    if not isinstance(result, dict):
        return ""
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    texts = [
        str(item.get("text") or "")
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    return "\n\n".join(text for text in texts if text)


def _normalize_exa_results(text: str, *, limit: int) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for block in _RESULT_SEPARATOR.split(text.strip()):
        if not block.strip():
            continue
        fields, body = _parse_result_block(block)
        url = fields.get("url", "")
        if not url.startswith(("http://", "https://")):
            continue
        snippet = _compact_snippet(body, max_chars=700)
        results.append(
            {
                "title": fields.get("title", "") or url,
                "url": url,
                "snippet": snippet,
                "published_at": fields.get("published", ""),
                "source": urlparse(url).hostname or "",
            }
        )
        if len(results) >= limit:
            break
    return results


def _parse_result_block(block: str) -> tuple[dict[str, str], str]:
    fields: dict[str, str] = {}
    body_lines: list[str] = []
    in_body = False
    for line in block.splitlines():
        stripped = line.strip()
        if stripped == "Highlights:":
            in_body = True
            continue
        if not in_body:
            match = re.match(r"^(Title|URL|Published|Author):\s*(.*)$", stripped)
            if match:
                fields[match.group(1).lower()] = match.group(2).strip()
                continue
        if in_body or stripped:
            body_lines.append(stripped)
    return fields, "\n".join(body_lines)


def _compact_snippet(text: str, *, max_chars: int) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        cleaned = line.strip().lstrip("- ").strip()
        if not cleaned or cleaned == "...":
            continue
        if cleaned not in lines:
            lines.append(cleaned)
        if sum(len(item) for item in lines) >= max_chars:
            break
    compact = " ".join(lines)
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 1].rstrip() + "…"
