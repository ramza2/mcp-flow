"""Outbox-poll recovery for MISSING/STALE/FAILED ToolEmbedding indexes."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.embedding_profile import EmbeddingProfileRepository
from app.repositories.outbox import OutboxRepository
from app.repositories.tool_embedding import ToolEmbeddingRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ToolEmbeddingRecoveryEnqueueResult:
    profile_id: uuid.UUID | None
    selected: int
    enqueued: int


class ToolEmbeddingRecoveryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._profiles = EmbeddingProfileRepository(session)
        self._embeddings = ToolEmbeddingRepository(session)
        self._outbox = OutboxRepository(session)

    def _published_within_cooldown(
        self,
        *,
        published_at: datetime | None,
        min_age_seconds: int,
        now: datetime,
    ) -> bool:
        if published_at is None or min_age_seconds <= 0:
            return False
        ts = published_at if published_at.tzinfo is not None else published_at.replace(
            tzinfo=UTC
        )
        return (now - ts).total_seconds() < float(min_age_seconds)

    async def list_recoverable_version_ids(
        self,
        *,
        limit: int,
        min_age_seconds: int,
        now: datetime | None = None,
    ) -> tuple[uuid.UUID, list[uuid.UUID]] | tuple[None, list[uuid.UUID]]:
        """Return (active_profile_id, version_ids) needing a durable ensure Outbox.

        Protections (reuse Outbox evidence; no new tables):
        - Skip when an unpublished TOOL_EMBEDDING_ENSURE already exists
          (in-flight broker delivery / durable retry).
        - Skip when the latest ensure was published within ``min_age_seconds``
          (covers MISSING while the worker is still generating, and dampens
          FAILED/STALE retry storms). After a new Outbox is later published,
          cooldown applies again from that ``published_at``.
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
            if latest is not None and self._published_within_cooldown(
                published_at=latest.published_at,
                min_age_seconds=min_age_seconds,
                now=ts,
            ):
                continue

            recoverable.append(version_id)
        return profile.id, recoverable

    async def enqueue_recoverable(
        self,
        *,
        limit: int,
        min_age_seconds: int,
        now: datetime | None = None,
    ) -> ToolEmbeddingRecoveryEnqueueResult:
        """Create durable TOOL_EMBEDDING_ENSURE Outbox rows for recoverable gaps.

        Does not call Celery directly. Broker delivery stays on OutboxRelayService
        so publish failures remain unpublished and are retried on later polls.
        """
        ts = now or datetime.now(UTC)
        profile_id, version_ids = await self.list_recoverable_version_ids(
            limit=limit,
            min_age_seconds=min_age_seconds,
            now=ts,
        )
        if profile_id is None:
            return ToolEmbeddingRecoveryEnqueueResult(
                profile_id=None, selected=0, enqueued=0
            )

        enqueued = 0
        for version_id in version_ids:
            # Re-check unpublished + cooldown under this session before insert.
            pending = await self._outbox.find_unpublished_tool_embedding_ensure(
                tool_version_id=version_id,
                embedding_profile_id=profile_id,
            )
            if pending is not None:
                continue
            latest = await self._outbox.find_latest_tool_embedding_ensure(
                tool_version_id=version_id,
                embedding_profile_id=profile_id,
            )
            if latest is not None and self._published_within_cooldown(
                published_at=latest.published_at,
                min_age_seconds=min_age_seconds,
                now=ts,
            ):
                continue

            row = await self._outbox.create_tool_embedding_ensure(
                tool_version_id=version_id,
                embedding_profile_id=profile_id,
                created_at=ts,
            )
            enqueued += 1
            logger.info(
                "tool embedding recovery enqueued tool_version_id=%s profile_id=%s "
                "outbox_event_id=%s",
                version_id,
                profile_id,
                row.id,
            )
        return ToolEmbeddingRecoveryEnqueueResult(
            profile_id=profile_id,
            selected=len(version_ids),
            enqueued=enqueued,
        )
