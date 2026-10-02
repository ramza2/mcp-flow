"""WorkflowVersion repository (docs/05 workflow_versions)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workflow import WorkflowVersion
from app.schemas.common_page import ALLOWED_WORKFLOW_VERSION_SORT, parse_sort


class WorkflowVersionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def next_version_no(self, workflow_id: uuid.UUID) -> int:
        stmt = select(func.coalesce(func.max(WorkflowVersion.version_no), 0)).where(
            WorkflowVersion.workflow_id == workflow_id
        )
        current = int((await self._session.execute(stmt)).scalar_one())
        return current + 1

    async def create(
        self,
        *,
        workflow_id: uuid.UUID,
        version_no: int,
        plan_schema_version: str,
        plan_definition: dict[str, Any],
        input_schema: dict[str, Any],
        output_schema: dict[str, Any],
        policy_defaults: dict[str, Any],
        content_hash: str,
        change_summary: str | None = None,
        created_by: uuid.UUID | None = None,
    ) -> WorkflowVersion:
        version = WorkflowVersion(
            workflow_id=workflow_id,
            version_no=version_no,
            status="DRAFT",
            plan_schema_version=plan_schema_version,
            plan_definition=plan_definition,
            input_schema=input_schema,
            output_schema=output_schema,
            policy_defaults=policy_defaults,
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

    async def get(self, version_id: uuid.UUID) -> WorkflowVersion | None:
        result = await self._session.execute(
            select(WorkflowVersion).where(WorkflowVersion.id == version_id)
        )
        return result.scalar_one_or_none()

    async def get_for_workflow(
        self, workflow_id: uuid.UUID, version_id: uuid.UUID
    ) -> WorkflowVersion | None:
        result = await self._session.execute(
            select(WorkflowVersion).where(
                WorkflowVersion.id == version_id,
                WorkflowVersion.workflow_id == workflow_id,
            )
        )
        return result.scalar_one_or_none()

    async def lock_for_update(
        self, workflow_id: uuid.UUID, version_id: uuid.UUID
    ) -> WorkflowVersion | None:
        stmt = (
            select(WorkflowVersion)
            .where(
                WorkflowVersion.id == version_id,
                WorkflowVersion.workflow_id == workflow_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_for_workflow(
        self,
        workflow_id: uuid.UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        sort: str = "-version_no",
    ) -> tuple[list[WorkflowVersion], int]:
        field, direction = parse_sort(
            sort,
            allowed=ALLOWED_WORKFLOW_VERSION_SORT,
            default_field="version_no",
        )
        stmt = select(WorkflowVersion).where(WorkflowVersion.workflow_id == workflow_id)
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())

        sort_col = getattr(WorkflowVersion, field)
        order = sort_col.desc() if direction == "desc" else sort_col.asc()
        offset = (page - 1) * page_size
        rows_stmt = stmt.order_by(order).offset(offset).limit(page_size)
        rows = list((await self._session.execute(rows_stmt)).scalars().all())
        return rows, total

    async def set_validation(
        self,
        version: WorkflowVersion,
        *,
        validation_status: str,
        validation_report: dict[str, Any] | None,
    ) -> WorkflowVersion:
        version.validation_status = validation_status
        version.validation_report = validation_report
        await self._session.flush()
        await self._session.refresh(version)
        return version

    async def update_plan(
        self,
        version: WorkflowVersion,
        *,
        plan_definition: dict[str, Any],
        content_hash: str,
        change_summary: str | None = None,
        clear_change_summary: bool = False,
    ) -> WorkflowVersion:
        version.plan_definition = plan_definition
        version.content_hash = content_hash
        version.validation_status = "INVALID"
        version.validation_report = None
        if clear_change_summary:
            version.change_summary = None
        elif change_summary is not None:
            version.change_summary = change_summary
        await self._session.flush()
        await self._session.refresh(version)
        return version

    async def mark_published(
        self,
        version: WorkflowVersion,
        *,
        published_at: datetime,
        published_by: uuid.UUID | None = None,
    ) -> WorkflowVersion:
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
        version: WorkflowVersion,
        *,
        deprecated_at: datetime,
        deprecated_by: uuid.UUID | None = None,
    ) -> WorkflowVersion:
        version.status = "DEPRECATED"
        version.deprecated_at = deprecated_at
        version.deprecated_by = deprecated_by
        await self._session.flush()
        await self._session.refresh(version)
        return version
