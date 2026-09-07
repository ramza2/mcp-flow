"""Agent repository (docs/05 agents)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.schemas.common_page import ALLOWED_AGENT_SORT, parse_sort


class AgentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _live(self) -> Select[tuple[Agent]]:
        return select(Agent).where(Agent.deleted_at.is_(None))

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
    ) -> Agent:
        agent = Agent(
            code=code,
            name=name,
            description=description,
            visibility=visibility,
            status=status,
            owner_id=owner_id,
            created_by=created_by,
            updated_by=created_by,
        )
        self._session.add(agent)
        await self._session.flush()
        await self._session.refresh(agent)
        return agent

    async def get(self, agent_id: uuid.UUID) -> Agent | None:
        result = await self._session.execute(self._live().where(Agent.id == agent_id))
        return result.scalar_one_or_none()

    async def get_by_code(self, code: str) -> Agent | None:
        result = await self._session.execute(self._live().where(Agent.code == code))
        return result.scalar_one_or_none()

    async def lock_for_update(self, agent_id: uuid.UUID) -> Agent | None:
        stmt = (
            self._live()
            .where(Agent.id == agent_id)
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
    ) -> tuple[list[Agent], int]:
        field, direction = parse_sort(sort, allowed=ALLOWED_AGENT_SORT)
        stmt = self._live()

        if status:
            statuses = [part.strip() for part in status.split(",") if part.strip()]
            if len(statuses) == 1:
                stmt = stmt.where(Agent.status == statuses[0])
            elif statuses:
                stmt = stmt.where(Agent.status.in_(statuses))

        if q:
            pattern = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(
                    Agent.name.ilike(pattern),
                    Agent.code.ilike(pattern),
                    Agent.description.ilike(pattern),
                )
            )

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())

        sort_col = getattr(Agent, field)
        order = sort_col.desc() if direction == "desc" else sort_col.asc()
        offset = (page - 1) * page_size
        rows_stmt = stmt.order_by(order).offset(offset).limit(page_size)
        rows = list((await self._session.execute(rows_stmt)).scalars().all())
        return rows, total

    async def update_atomic(
        self,
        agent_id: uuid.UUID,
        *,
        expected_lock_version: int,
        updated_by: uuid.UUID | None = None,
        **fields: Any,
    ) -> Agent | None:
        values: dict[str, Any] = {
            key: value
            for key, value in fields.items()
            if hasattr(Agent, key) and key not in {"id", "lock_version", "code"}
        }
        values["lock_version"] = Agent.lock_version + 1
        values["updated_at"] = func.now()
        if updated_by is not None:
            values["updated_by"] = updated_by

        stmt = (
            update(Agent)
            .where(
                Agent.id == agent_id,
                Agent.lock_version == expected_lock_version,
                Agent.deleted_at.is_(None),
            )
            .values(**values)
            .returning(Agent)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        await self._session.refresh(row)
        return row

    async def set_current_version(
        self,
        agent_id: uuid.UUID,
        *,
        current_version_id: uuid.UUID,
        expected_lock_version: int | None = None,
    ) -> Agent | None:
        """Bump lock_version and set current_version_id (publish path)."""

        values: dict[str, Any] = {
            "current_version_id": current_version_id,
            "lock_version": Agent.lock_version + 1,
            "updated_at": func.now(),
        }
        conditions = [Agent.id == agent_id, Agent.deleted_at.is_(None)]
        if expected_lock_version is not None:
            conditions.append(Agent.lock_version == expected_lock_version)

        stmt = update(Agent).where(*conditions).values(**values).returning(Agent)
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        await self._session.refresh(row)
        return row
