"""Workflow repository (docs/05 workflows)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workflow import Workflow
from app.schemas.common_page import ALLOWED_WORKFLOW_SORT, parse_sort


class WorkflowRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _live(self) -> Select[tuple[Workflow]]:
        return select(Workflow).where(Workflow.deleted_at.is_(None))

    async def create(
        self,
        *,
        code: str,
        name: str,
        description: str | None = None,
        visibility: str = "PRIVATE",
        status: str = "DRAFT",
        owner_id: uuid.UUID | None = None,
        created_by: uuid.UUID | None = None,
    ) -> Workflow:
        workflow = Workflow(
            code=code,
            name=name,
            description=description,
            visibility=visibility,
            status=status,
            owner_id=owner_id,
            created_by=created_by,
            updated_by=created_by,
        )
        self._session.add(workflow)
        await self._session.flush()
        await self._session.refresh(workflow)
        return workflow

    async def get(self, workflow_id: uuid.UUID) -> Workflow | None:
        result = await self._session.execute(
            self._live().where(Workflow.id == workflow_id)
        )
        return result.scalar_one_or_none()

    async def get_by_code(self, code: str) -> Workflow | None:
        result = await self._session.execute(self._live().where(Workflow.code == code))
        return result.scalar_one_or_none()

    async def lock_for_update(self, workflow_id: uuid.UUID) -> Workflow | None:
        stmt = (
            self._live()
            .where(Workflow.id == workflow_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: str | None = None,
        q: str | None = None,
        sort: str = "-updated_at",
    ) -> tuple[list[Workflow], int]:
        field, direction = parse_sort(sort, allowed=ALLOWED_WORKFLOW_SORT)
        stmt = self._live()

        if status:
            statuses = [part.strip() for part in status.split(",") if part.strip()]
            if len(statuses) == 1:
                stmt = stmt.where(Workflow.status == statuses[0])
            elif statuses:
                stmt = stmt.where(Workflow.status.in_(statuses))

        if q:
            pattern = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(
                    Workflow.name.ilike(pattern),
                    Workflow.code.ilike(pattern),
                    Workflow.description.ilike(pattern),
                )
            )

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())

        sort_col = getattr(Workflow, field)
        order = sort_col.desc() if direction == "desc" else sort_col.asc()
        offset = (page - 1) * page_size
        rows_stmt = stmt.order_by(order).offset(offset).limit(page_size)
        rows = list((await self._session.execute(rows_stmt)).scalars().all())
        return rows, total

    async def update_atomic(
        self,
        workflow_id: uuid.UUID,
        *,
        expected_lock_version: int,
        updated_by: uuid.UUID | None = None,
        **fields: Any,
    ) -> Workflow | None:
        values: dict[str, Any] = {
            key: value
            for key, value in fields.items()
            if hasattr(Workflow, key) and key not in {"id", "lock_version", "code"}
        }
        values["lock_version"] = Workflow.lock_version + 1
        values["updated_at"] = func.now()
        if updated_by is not None:
            values["updated_by"] = updated_by

        stmt = (
            update(Workflow)
            .where(
                Workflow.id == workflow_id,
                Workflow.lock_version == expected_lock_version,
                Workflow.deleted_at.is_(None),
            )
            .values(**values)
            .returning(Workflow)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        await self._session.refresh(row)
        return row

    async def set_current_version(
        self,
        workflow_id: uuid.UUID,
        *,
        current_version_id: uuid.UUID,
        expected_lock_version: int | None = None,
    ) -> Workflow | None:
        """Bump lock_version and set current_version_id (publish path)."""

        values: dict[str, Any] = {
            "current_version_id": current_version_id,
            "lock_version": Workflow.lock_version + 1,
            "updated_at": func.now(),
        }
        conditions = [Workflow.id == workflow_id, Workflow.deleted_at.is_(None)]
        if expected_lock_version is not None:
            conditions.append(Workflow.lock_version == expected_lock_version)

        stmt = update(Workflow).where(*conditions).values(**values).returning(Workflow)
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        await self._session.refresh(row)
        return row
