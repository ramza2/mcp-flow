"""Bounded rediscovery of stuck Tool Factory Jobs (docs/05 §15.2).

PostgreSQL remains SoT. Broker republish is at-least-once; FactoryWorkerService
claim fencing collapses duplicates.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import JobStatus
from app.models.factory import ToolFactoryJob


class FactoryRecoveryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_expired_running_ids(
        self,
        *,
        limit: int,
        now: datetime | None = None,
    ) -> list[uuid.UUID]:
        ts = now or datetime.now(UTC)
        stmt = (
            select(ToolFactoryJob.id)
            .where(
                ToolFactoryJob.status == JobStatus.RUNNING.value,
                ToolFactoryJob.lease_expires_at.is_not(None),
                ToolFactoryJob.lease_expires_at <= ts,
            )
            .order_by(ToolFactoryJob.lease_expires_at.asc(), ToolFactoryJob.id.asc())
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_stale_queued_ids(
        self,
        *,
        limit: int,
        min_age_seconds: int,
        now: datetime | None = None,
    ) -> list[uuid.UUID]:
        """QUEUED Jobs older than min_age — covers lost broker messages after mark-published."""

        ts = now or datetime.now(UTC)
        cutoff = ts - timedelta(seconds=max(0, min_age_seconds))
        stmt = (
            select(ToolFactoryJob.id)
            .where(
                ToolFactoryJob.status == JobStatus.QUEUED.value,
                or_(
                    ToolFactoryJob.queued_at.is_(None),
                    ToolFactoryJob.queued_at <= cutoff,
                ),
            )
            .order_by(ToolFactoryJob.queued_at.asc().nullsfirst(), ToolFactoryJob.id.asc())
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())
