"""ScheduleOccurrence repository (docs/05 §14.2)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import OccurrenceStatus
from app.models.schedule import ScheduleOccurrence

_UQ_SCHEDULE_OCCURRENCE = "uq_schedule_occurrences_schedule_scheduled_for"


def _constraint_name(exc: IntegrityError) -> str | None:
    orig = getattr(exc, "orig", None)
    if orig is None:
        return None
    diag = getattr(orig, "diag", None)
    name = getattr(diag, "constraint_name", None) if diag is not None else None
    if name:
        return str(name)
    name = getattr(orig, "constraint_name", None)
    return str(name) if name else None


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
        result = await self._session.execute(
            select(ScheduleOccurrence).where(
                ScheduleOccurrence.schedule_id == schedule_id,
                ScheduleOccurrence.scheduled_for == scheduled_for,
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
            stmt = stmt.where(ScheduleOccurrence.scheduled_for <= to_time)

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
        occurrence = ScheduleOccurrence(
            schedule_id=schedule_id,
            scheduled_for=scheduled_for,
            status=OccurrenceStatus.PLANNED.value,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(occurrence)
                await self._session.flush()
                await self._session.refresh(occurrence)
                return occurrence
        except IntegrityError as exc:
            if _constraint_name(exc) != _UQ_SCHEDULE_OCCURRENCE:
                raise
            existing = await self.get_by_schedule_and_time(schedule_id, scheduled_for)
            if existing is None:
                raise
            return existing
