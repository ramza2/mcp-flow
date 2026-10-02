"""Schedule repository (docs/05 §14)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schedule import Schedule
from app.schemas.common_page import parse_sort

ALLOWED_SCHEDULE_SORT = frozenset(
    {"name", "updated_at", "next_run_at"},
)


class ScheduleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _live(self) -> Select[tuple[Schedule]]:
        return select(Schedule).where(Schedule.deleted_at.is_(None))

    async def get(self, schedule_id: uuid.UUID) -> Schedule | None:
        result = await self._session.execute(
            self._live().where(Schedule.id == schedule_id)
        )
        return result.scalar_one_or_none()

    async def get_for_owner(
        self, schedule_id: uuid.UUID, owner_id: uuid.UUID
    ) -> Schedule | None:
        result = await self._session.execute(
            self._live().where(
                Schedule.id == schedule_id,
                Schedule.owner_id == owner_id,
            )
        )
        return result.scalar_one_or_none()

    async def lock_for_update(self, schedule_id: uuid.UUID) -> Schedule | None:
        stmt = (
            self._live()
            .where(Schedule.id == schedule_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_for_owner(
        self,
        owner_id: uuid.UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        q: str | None = None,
        status: str | None = None,
        target_type: str | None = None,
        sort: str = "-updated_at",
    ) -> tuple[list[Schedule], int]:
        field, direction = parse_sort(sort, allowed=set(ALLOWED_SCHEDULE_SORT))
        stmt = self._live().where(Schedule.owner_id == owner_id)

        if status:
            statuses = [part.strip() for part in status.split(",") if part.strip()]
            if len(statuses) == 1:
                stmt = stmt.where(Schedule.status == statuses[0])
            elif statuses:
                stmt = stmt.where(Schedule.status.in_(statuses))

        if target_type:
            stmt = stmt.where(Schedule.target_type == target_type)

        if q:
            pattern = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(
                    Schedule.name.ilike(pattern),
                    Schedule.description.ilike(pattern),
                )
            )

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())

        sort_col = getattr(Schedule, field)
        order = sort_col.desc() if direction == "desc" else sort_col.asc()
        offset = (page - 1) * page_size
        rows_stmt = stmt.order_by(order).offset(offset).limit(page_size)
        rows = list((await self._session.execute(rows_stmt)).scalars().all())
        return rows, total

    async def create(
        self,
        *,
        name: str,
        description: str | None,
        owner_id: uuid.UUID,
        target_type: str,
        agent_version_id: uuid.UUID | None,
        workflow_version_id: uuid.UUID | None,
        schedule_type: str,
        schedule_expression: str,
        timezone: str,
        input_template: dict[str, Any],
        misfire_policy: str,
        overlap_policy: str,
        max_catch_up: int,
        status: str,
        next_run_at: Any = None,
        last_run_at: Any = None,
        start_at: Any = None,
        end_at: Any = None,
        created_by: uuid.UUID | None = None,
    ) -> Schedule:
        schedule = Schedule(
            name=name,
            description=description,
            owner_id=owner_id,
            target_type=target_type,
            agent_version_id=agent_version_id,
            workflow_version_id=workflow_version_id,
            schedule_type=schedule_type,
            schedule_expression=schedule_expression,
            timezone=timezone,
            input_template=input_template,
            misfire_policy=misfire_policy,
            overlap_policy=overlap_policy,
            max_catch_up=max_catch_up,
            status=status,
            next_run_at=next_run_at,
            last_run_at=last_run_at,
            start_at=start_at,
            end_at=end_at,
            created_by=created_by,
            updated_by=created_by,
        )
        self._session.add(schedule)
        await self._session.flush()
        await self._session.refresh(schedule)
        return schedule

    async def update_atomic(
        self,
        schedule_id: uuid.UUID,
        *,
        expected_lock_version: int,
        updated_by: uuid.UUID | None = None,
        **fields: Any,
    ) -> Schedule | None:
        values: dict[str, Any] = {
            key: value
            for key, value in fields.items()
            if hasattr(Schedule, key) and key not in {"id", "lock_version", "owner_id"}
        }
        values["lock_version"] = Schedule.lock_version + 1
        values["updated_at"] = func.now()
        if updated_by is not None:
            values["updated_by"] = updated_by

        stmt = (
            update(Schedule)
            .where(
                Schedule.id == schedule_id,
                Schedule.lock_version == expected_lock_version,
                Schedule.deleted_at.is_(None),
            )
            .values(**values)
            .returning(Schedule)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        await self._session.refresh(row)
        return row
