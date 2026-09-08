"""Bootstrap permission catalog helpers (docs/06 §6)."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import BOOTSTRAP_PERMISSION_CODES
from app.models.auth import Permission

_BOOTSTRAP_META: dict[str, tuple[str, str]] = {
    "mcp.server.read": ("MCP Server Read", "Read MCP Server registry resources."),
    "mcp.server.manage": ("MCP Server Manage", "Create and manage MCP Servers."),
    "mcp.tool.read": ("MCP Tool Read", "Read MCP Tool registry resources."),
    "mcp.tool.execute": (
        "MCP Tool Execute",
        "Execute MCP Tools within granted scope.",
    ),
    "agent.read": ("Agent Read", "Read Agent registry resources."),
    "agent.manage": ("Agent Manage", "Create and manage Agents."),
    "workflow.execute": ("Workflow Execute", "Execute Workflow versions."),
    "execution.read": ("Execution Read", "Read Execution state and events."),
    "execution.cancel": ("Execution Cancel", "Cancel running Executions."),
    "approval.decide": ("Approval Decide", "Approve or reject pending Approvals."),
    "audit.read": ("Audit Read", "Read audit records."),
}


def permission_seed_id(code: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"mcpflow:permission:{code}")


async def seed_bootstrap_permissions(session: AsyncSession) -> None:
    """Idempotent insert of docs/06 bootstrap permission codes (for SQLite tests)."""
    for code in BOOTSTRAP_PERMISSION_CODES:
        name, description = _BOOTSTRAP_META[code]
        existing = await session.get(Permission, permission_seed_id(code))
        if existing is not None:
            continue
        session.add(
            Permission(
                id=permission_seed_id(code),
                code=code,
                name=name,
                description=description,
            )
        )
    await session.flush()
