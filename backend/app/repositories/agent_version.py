"""AgentVersion repository (docs/05 agent_versions)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import AgentVersion
from app.schemas.common_page import ALLOWED_AGENT_VERSION_SORT, parse_sort


class AgentVersionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def next_version_no(self, agent_id: uuid.UUID) -> int:
        stmt = select(func.coalesce(func.max(AgentVersion.version_no), 0)).where(
            AgentVersion.agent_id == agent_id
        )
        current = int((await self._session.execute(stmt)).scalar_one())
        return current + 1

    async def create(
        self,
        *,
        agent_id: uuid.UUID,
        version_no: int,
        system_instruction: str,
        llm_profile_id: uuid.UUID,
        request_schema_version: str,
        plan_schema_version: str,
        selection_settings: dict[str, Any],
        planning_settings: dict[str, Any],
        response_settings: dict[str, Any],
        content_hash: str,
        change_summary: str | None = None,
        created_by: uuid.UUID | None = None,
    ) -> AgentVersion:
        version = AgentVersion(
            agent_id=agent_id,
            version_no=version_no,
            status="DRAFT",
            system_instruction=system_instruction,
            llm_profile_id=llm_profile_id,
            request_schema_version=request_schema_version,
            plan_schema_version=plan_schema_version,
            selection_settings=selection_settings,
            planning_settings=planning_settings,
            response_settings=response_settings,
            validation_status="INVALID",
            validation_report=None,
            content_hash=content_hash,
            change_summary=change_summary,
            created_by=created_by,
        )
        self._session.add(version)
        await self._session.flush()
        await self._session.refresh(version)
        return version

    async def get(self, version_id: uuid.UUID) -> AgentVersion | None:
        result = await self._session.execute(
            select(AgentVersion).where(AgentVersion.id == version_id)
        )
        return result.scalar_one_or_none()

    async def get_for_agent(
        self, agent_id: uuid.UUID, version_id: uuid.UUID
    ) -> AgentVersion | None:
        result = await self._session.execute(
            select(AgentVersion).where(
                AgentVersion.id == version_id,
                AgentVersion.agent_id == agent_id,
            )
        )
        return result.scalar_one_or_none()

    async def lock_for_update(
        self, agent_id: uuid.UUID, version_id: uuid.UUID
    ) -> AgentVersion | None:
        stmt = (
            select(AgentVersion)
            .where(
                AgentVersion.id == version_id,
                AgentVersion.agent_id == agent_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_for_agent(
        self,
        agent_id: uuid.UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        sort: str = "-version_no",
    ) -> tuple[list[AgentVersion], int]:
        field, direction = parse_sort(
            sort,
            allowed=ALLOWED_AGENT_VERSION_SORT,
            default_field="version_no",
        )
        stmt = select(AgentVersion).where(AgentVersion.agent_id == agent_id)
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())

        sort_col = getattr(AgentVersion, field)
        order = sort_col.desc() if direction == "desc" else sort_col.asc()
        offset = (page - 1) * page_size
        rows_stmt = stmt.order_by(order).offset(offset).limit(page_size)
        rows = list((await self._session.execute(rows_stmt)).scalars().all())
        return rows, total

    async def set_validation(
        self,
        version: AgentVersion,
        *,
        validation_status: str,
        validation_report: dict[str, Any] | None,
    ) -> AgentVersion:
        version.validation_status = validation_status
        version.validation_report = validation_report
        await self._session.flush()
        await self._session.refresh(version)
        return version

    async def mark_published(
        self,
        version: AgentVersion,
        *,
        published_at: datetime,
        published_by: uuid.UUID | None = None,
    ) -> AgentVersion:
        version.status = "PUBLISHED"
        version.published_at = published_at
        version.published_by = published_by
        version.deprecated_at = None
        version.deprecated_by = None
        await self._session.flush()
        await self._session.refresh(version)
        return version

    async def mark_deprecated(
        self,
        version: AgentVersion,
        *,
        deprecated_at: datetime,
        deprecated_by: uuid.UUID | None = None,
    ) -> AgentVersion:
        version.status = "DEPRECATED"
        version.deprecated_at = deprecated_at
        version.deprecated_by = deprecated_by
        await self._session.flush()
        await self._session.refresh(version)
        return version
