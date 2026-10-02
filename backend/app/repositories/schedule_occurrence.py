"""ScheduleOccurrence repository (docs/05 §14.2)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import OccurrenceStatus
from app.models.schedule import ScheduleOccurrence

_UQ_SCHEDULE_OCCURRENCE = "uq_schedule_occurrences_schedule_scheduled_for"


def _is_schedule_occurrence_unique_violation(exc: IntegrityError) -> bool:
    """Match only UNIQUE(schedule_id, scheduled_for); never hide unrelated errors."""
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None) if orig is not None else None
    name = getattr(diag, "constraint_name", None) if diag is not None else None
    if name is None and orig is not None:
        name = getattr(orig, "constraint_name", None)
    if name is not None and str(name) == _UQ_SCHEDULE_OCCURRENCE:
        return True
    msg = str(orig if orig is not None else exc)
    return _UQ_SCHEDULE_OCCURRENCE in msg


def _normalize_scheduled_for_utc(scheduled_for: datetime) -> datetime:
    """Require timezone-aware scheduled_for and normalize to UTC for durable keys."""
    if scheduled_for.tzinfo is None:
        raise AppError(
            code="VALIDATION_ERROR",
            message="scheduled_for must be timezone-aware.",
            status_code=400,
        )
    return scheduled_for.astimezone(UTC)


class ScheduleOccurrenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, occurrence_id: uuid.UUID) -> ScheduleOccurrence | None:
        result = await self._session.execute(
            select(ScheduleOccurrence).where(ScheduleOccurrence.id == occurrence_id)
        )
        return result.scalar_one_or_none()

    async def get_by_schedule_and_time(
        self,
        schedule_id: uuid.UUID,
        scheduled_for: datetime,
    ) -> ScheduleOccurrence | None:
        scheduled_for_utc = _normalize_scheduled_for_utc(scheduled_for)
        result = await self._session.execute(
            select(ScheduleOccurrence).where(
                ScheduleOccurrence.schedule_id == schedule_id,
                ScheduleOccurrence.scheduled_for == scheduled_for_utc,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_schedule(
        self,
        schedule_id: uuid.UUID,
        *,
        status: str | None = None,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[ScheduleOccurrence], int]:
        stmt = select(ScheduleOccurrence).where(
            ScheduleOccurrence.schedule_id == schedule_id
        )
        if status is not None:
            stmt = stmt.where(ScheduleOccurrence.status == status)
        if from_time is not None:
            stmt = stmt.where(ScheduleOccurrence.scheduled_for >= from_time)
        if to_time is not None:
            # Canonical API: to is exclusive.
            stmt = stmt.where(ScheduleOccurrence.scheduled_for < to_time)

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())

        offset = (page - 1) * page_size
        rows_stmt = (
            stmt.order_by(
                ScheduleOccurrence.scheduled_for.desc(),
                ScheduleOccurrence.id.desc(),
            )
            .offset(offset)
            .limit(page_size)
        )
        rows = list((await self._session.execute(rows_stmt)).scalars().all())
        return rows, total

    async def create_planned(
        self,
        schedule_id: uuid.UUID,
        scheduled_for: datetime,
    ) -> ScheduleOccurrence:
        scheduled_for_utc = _normalize_scheduled_for_utc(scheduled_for)
        existing = await self.get_by_schedule_and_time(schedule_id, scheduled_for_utc)
        if existing is not None:
            return existing

        occurrence = ScheduleOccurrence(
            schedule_id=schedule_id,
            scheduled_for=scheduled_for_utc,
            status=OccurrenceStatus.PLANNED.value,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(occurrence)
                await self._session.flush()
                await self._session.refresh(occurrence)
                return occurrence
        except IntegrityError as exc:
            if not _is_schedule_occurrence_unique_violation(exc):
                raise
            existing = await self.get_by_schedule_and_time(
                schedule_id, scheduled_for_utc
            )
            if existing is None:
                raise
            return existing
