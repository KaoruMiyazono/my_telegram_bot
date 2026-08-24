from __future__ import annotations

from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

server = FastMCP("m9-proactive-source-test")
acked: set[str] = set()


@server.tool()
def fetch_alerts(offset: int = 0, limit: int = 50) -> list[dict[str, object]]:
    """Return stable service alerts that have not been acknowledged."""

    items = [
        {
            "event_id": "cpu-95",
            "title": "服务器告警",
            "body": "CPU 已达到 95%",
            "triggered_at": datetime.now(timezone.utc).isoformat(),
        }
    ]
    pending = [item for item in items if str(item["event_id"]) not in acked]
    return pending[offset : offset + limit]


@server.tool()
def fetch_content(offset: int = 0, limit: int = 50) -> list[dict[str, object]]:
    """Return stable content candidates with provider relevance metadata."""

    items = [
        {
            "event_id": "news-001",
            "title": "新的 Agent Runtime 发布",
            "content": "这是一条来自本地可控 MCP Server 的候选内容。",
            "source": "local-demo",
            "relevance_score": 0.92,
            "published_at": datetime.now(timezone.utc).isoformat(),
        }
    ]
    pending = [item for item in items if str(item["event_id"]) not in acked]
    return pending[offset : offset + limit]


@server.tool()
def fetch_context() -> dict[str, object]:
    """Return context that can assist judging but cannot trigger or ACK."""

    return {
        "online": True,
        "local_hour": datetime.now().hour,
        "note": "用户当前在线",
    }


@server.tool()
def ack_events(event_ids: list[str], feedback: str = "delivered") -> dict[str, object]:
    """Idempotently acknowledge exactly the requested event IDs."""

    committed = list(dict.fromkeys(str(item) for item in event_ids if str(item)))
    acked.update(committed)
    return {"status": "committed", "ids": committed, "feedback": feedback}


if __name__ == "__main__":
    server.run(transport="stdio")
