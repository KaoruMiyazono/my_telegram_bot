from __future__ import annotations

from agent.tools.registry import ToolRegistry
from agent.tools.web_fetch import WebFetchClient, build_web_fetch_tool
from agent.tools.web_search import WebSearchClient, WebSearchConfig, build_web_search_tool


def register_web_tools(
    registry: ToolRegistry,
    *,
    search_client: WebSearchClient | None = None,
    fetch_client: WebFetchClient | None = None,
) -> None:
    """Register first-party read-only web tools in the shared registry."""
    if search_client is None or fetch_client is None:
        from config.settings import settings

        if search_client is None:
            search_client = WebSearchClient(
                WebSearchConfig(
                    endpoint=settings.WEB_SEARCH_ENDPOINT,
                    api_key=settings.SEARCH_API_KEY,
                    proxy=settings.WEB_PROXY,
                    timeout_s=settings.WEB_SEARCH_TIMEOUT,
                    max_results=settings.WEB_SEARCH_MAX_RESULTS,
                )
            )
        if fetch_client is None:
            fetch_client = WebFetchClient(
                proxy=settings.WEB_PROXY,
                timeout_s=settings.WEB_FETCH_TIMEOUT,
                max_bytes=settings.WEB_FETCH_MAX_BYTES,
                max_chars=settings.WEB_FETCH_MAX_CHARS,
                max_redirects=settings.WEB_FETCH_MAX_REDIRECTS,
            )

    registry.register(
        build_web_search_tool(search_client),
        risk="read-only",
        always_on=False,
        search_hint="联网搜索 最新消息 新闻 价格 当前信息",
        source_type="builtin",
        source_name="web",
        tier="core",
        preloadable=True,
    )
    registry.register(
        build_web_fetch_tool(fetch_client),
        risk="read-only",
        always_on=False,
        search_hint="打开网页 读取链接 核实来源",
        source_type="builtin",
        source_name="web",
        tier="core",
        preloadable=True,
    )
