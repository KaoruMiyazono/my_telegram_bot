from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from agent.mcp.spec import McpServerSpec
from persistence.database import get_connection

McpRuntimeStatus = Literal[
    "configured", "starting", "ready", "draining", "stopped", "failed"
]


@dataclass(frozen=True)
class McpRuntimeState:
    name: str
    transport: str
    status: McpRuntimeStatus
    generation: int
    tool_count: int
    last_error: str = ""
    started_at: str | None = None
    updated_at: str = ""


class McpRuntimeStateStore:
    """Persist MCP lifecycle metadata without storing secret values."""

    def transition(
        self,
        spec: McpServerSpec,
        *,
        status: McpRuntimeStatus,
        generation: int,
        tool_count: int = 0,
        last_error: str = "",
        started: bool = False,
    ) -> McpRuntimeState:
        conn = get_connection()
        now = datetime.now(timezone.utc).isoformat()
        prior = conn.execute(
            "SELECT started_at FROM mcp_runtime_servers WHERE name = ?", (spec.name,)
        ).fetchone()
        started_at = now if started else (str(prior[0]) if prior and prior[0] else None)
        conn.execute(
            """
            INSERT INTO mcp_runtime_servers (
                name, transport, config_json, status, generation, tool_count,
                last_error, started_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                transport=excluded.transport,
                config_json=excluded.config_json,
                status=excluded.status,
                generation=excluded.generation,
                tool_count=excluded.tool_count,
                last_error=excluded.last_error,
                started_at=excluded.started_at,
                updated_at=excluded.updated_at
            """,
            (
                spec.name,
                spec.transport,
                json.dumps(spec.to_public_dict(), ensure_ascii=False, sort_keys=True),
                status,
                generation,
                tool_count,
                last_error[:2000],
                started_at,
                now,
            ),
        )
        conn.commit()
        return McpRuntimeState(
            name=spec.name,
            transport=spec.transport,
            status=status,
            generation=generation,
            tool_count=tool_count,
            last_error=last_error[:2000],
            started_at=started_at,
            updated_at=now,
        )

    def get(self, name: str) -> McpRuntimeState | None:
        row = get_connection().execute(
            """
            SELECT name, transport, status, generation, tool_count,
                   COALESCE(last_error, ''), started_at, updated_at
            FROM mcp_runtime_servers WHERE name = ?
            """,
            (name,),
        ).fetchone()
        return _row_to_state(row) if row else None

    def list(self) -> list[McpRuntimeState]:
        rows = get_connection().execute(
            """
            SELECT name, transport, status, generation, tool_count,
                   COALESCE(last_error, ''), started_at, updated_at
            FROM mcp_runtime_servers ORDER BY name
            """
        ).fetchall()
        return [_row_to_state(row) for row in rows]


def _row_to_state(row: tuple[Any, ...]) -> McpRuntimeState:
    return McpRuntimeState(
        name=str(row[0]),
        transport=str(row[1]),
        status=str(row[2]),  # type: ignore[arg-type]
        generation=int(row[3]),
        tool_count=int(row[4]),
        last_error=str(row[5]),
        started_at=str(row[6]) if row[6] else None,
        updated_at=str(row[7]),
    )
