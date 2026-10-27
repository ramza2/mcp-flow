"""Outbox-poll recovery for MISSING/STALE/FAILED ToolEmbedding indexes."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

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
        now: datetime | None = None,
    ) -> tuple[uuid.UUID, list[uuid.UUID]] | tuple[None, list[uuid.UUID]]:
        """Return (active_profile_id, version_ids) needing ensure republish.

        Protections (reuse Outbox evidence; no new tables):
        - Skip when an unpublished TOOL_EMBEDDING_ENSURE already exists
          (in-flight broker delivery).
        - Skip when the latest ensure was published within ``min_age_seconds``
          (covers MISSING after mark-published while the worker is still
          generating, and dampens FAILED/STALE retry storms).
        - Candidate listing already restricts to ACTIVE server/tool + VALID
          version and ages STALE/FAILED embedding rows by ``min_age_seconds``.
        """
        profile = await self._profiles.get_active_for_tools()
        if profile is None:
            return None, []

        ts = now or datetime.now(UTC)
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

            latest = await self._outbox.find_latest_tool_embedding_ensure(
                tool_version_id=version_id,
                embedding_profile_id=profile.id,
            )
            if (
                latest is not None
                and latest.published_at is not None
                and min_age_seconds > 0
            ):
                published_at = latest.published_at
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=UTC)
                age_seconds = (ts - published_at).total_seconds()
                if age_seconds < float(min_age_seconds):
                    continue

            recoverable.append(version_id)
        return profile.id, recoverable
