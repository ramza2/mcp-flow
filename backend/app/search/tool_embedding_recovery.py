"""Outbox-poll recovery for MISSING/STALE/FAILED ToolEmbedding indexes."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.embedding_profile import EmbeddingProfileRepository
from app.repositories.outbox import OutboxRepository
from app.repositories.tool_embedding import ToolEmbeddingRepository

logger = logging.getLogger(__name__)


class ToolEmbeddingRecoveryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._profiles = EmbeddingProfileRepository(session)
        self._embeddings = ToolEmbeddingRepository(session)
        self._outbox = OutboxRepository(session)

    async def list_recoverable_version_ids(
        self,
        *,
        limit: int,
        min_age_seconds: int,
    ) -> tuple[uuid.UUID, list[uuid.UUID]] | tuple[None, list[uuid.UUID]]:
        """Return (active_profile_id, version_ids) needing ensure republish.

        Skips version+profile pairs that already have an unpublished ensure
        Outbox row so lifecycle scheduling and recovery do not stampede.
        """
        profile = await self._profiles.get_active_for_tools()
        if profile is None:
            return None, []

        candidates = await self._embeddings.list_missing_or_stale_for_profile(
            profile.id,
            limit=limit,
            min_age_seconds=min_age_seconds,
        )
        recoverable: list[uuid.UUID] = []
        for version_id in candidates:
            pending = await self._outbox.find_unpublished_tool_embedding_ensure(
                tool_version_id=version_id,
                embedding_profile_id=profile.id,
            )
            if pending is not None:
                continue
            recoverable.append(version_id)
        return profile.id, recoverable
