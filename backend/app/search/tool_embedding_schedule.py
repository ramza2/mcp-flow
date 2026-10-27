"""Durable scheduling for ToolEmbedding ensure (Outbox only; no HTTP)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import MCPToolStatus
from app.models.mcp import MCPTool
from app.models.outbox import OutboxEvent
from app.repositories.embedding_profile import EmbeddingProfileRepository
from app.repositories.outbox import OutboxRepository
from app.repositories.tool_embedding import ToolEmbeddingRepository


async def enqueue_tool_embedding_ensure(
    session: AsyncSession,
    *,
    tool_version_id: uuid.UUID,
    embedding_profile_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
) -> OutboxEvent | None:
    """Enqueue one TOOL_EMBEDDING_ENSURE if not already unpublished.

    Resolves the active Embedding Profile when ``embedding_profile_id`` is omitted.
    Returns None when there is no active profile or an unpublished twin already
    exists. Never performs outbound embedding HTTP.
    """
    outbox = OutboxRepository(session)
    profile_id = embedding_profile_id
    if profile_id is None:
        active = await EmbeddingProfileRepository(session).get_active_for_tools()
        if active is None:
            return None
        profile_id = active.id

    existing = await outbox.find_unpublished_tool_embedding_ensure(
        tool_version_id=tool_version_id,
        embedding_profile_id=profile_id,
    )
    if existing is not None:
        return existing

    return await outbox.create_tool_embedding_ensure(
        tool_version_id=tool_version_id,
        embedding_profile_id=profile_id,
        created_at=created_at or datetime.now(UTC),
    )


async def enqueue_ensure_for_active_tool(
    session: AsyncSession,
    tool: MCPTool,
    *,
    created_at: datetime | None = None,
) -> OutboxEvent | None:
    """Enqueue ensure for an ACTIVE tool's current version + active profile."""
    if tool.status != MCPToolStatus.ACTIVE:
        return None
    if tool.current_version_id is None:
        return None
    return await enqueue_tool_embedding_ensure(
        session,
        tool_version_id=tool.current_version_id,
        created_at=created_at,
    )


async def enqueue_rebuild_for_profile(
    session: AsyncSession,
    profile_id: uuid.UUID,
    *,
    limit: int = 500,
    created_at: datetime | None = None,
) -> int:
    """Enqueue ensure for missing/STALE/FAILED current versions under a profile."""
    version_ids = await ToolEmbeddingRepository(
        session
    ).list_missing_or_stale_for_profile(profile_id, limit=limit)
    ts = created_at or datetime.now(UTC)
    enqueued = 0
    for version_id in version_ids:
        row = await enqueue_tool_embedding_ensure(
            session,
            tool_version_id=version_id,
            embedding_profile_id=profile_id,
            created_at=ts,
        )
        if row is not None:
            enqueued += 1
    return enqueued
