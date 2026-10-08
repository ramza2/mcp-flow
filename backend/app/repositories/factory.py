"""Persistence-only repository for Tool Factory Jobs (no commit)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.factory import ToolFactoryArtifact, ToolFactoryJob
from app.schemas.common_page import parse_sort

ALLOWED_FACTORY_JOB_SORT = {"created_at", "started_at", "finished_at", "status"}


class FactoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_job(
        self,
        *,
        job_type: str,
        status: str,
        source_name: str,
        source_sha256: str,
        analyzer_version: str,
        requested_by: uuid.UUID,
        source_format: str | None = None,
        operation_count: int = 0,
        server_count: int = 0,
        progress_current: int = 0,
        progress_total: int = 1,
        current_phase: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> ToolFactoryJob:
        job = ToolFactoryJob(
            job_type=job_type,
            status=status,
            source_name=source_name,
            source_sha256=source_sha256,
            source_format=source_format,
            analyzer_version=analyzer_version,
            operation_count=operation_count,
            server_count=server_count,
            progress_current=progress_current,
            progress_total=progress_total,
            current_phase=current_phase,
            error_code=error_code,
            error_message=error_message,
            requested_by=requested_by,
            started_at=started_at,
            finished_at=finished_at,
        )
        self._session.add(job)
        await self._session.flush()
        await self._session.refresh(job)
        return job

    async def finish_job_success(
        self,
        job: ToolFactoryJob,
        *,
        source_format: str,
        operation_count: int,
        server_count: int,
        finished_at: datetime,
    ) -> ToolFactoryJob:
        job.status = "SUCCEEDED"
        job.source_format = source_format
        job.operation_count = operation_count
        job.server_count = server_count
        job.progress_current = 1
        job.progress_total = 1
        job.error_code = None
        job.error_message = None
        job.finished_at = finished_at
        await self._session.flush()
        await self._session.refresh(job)
        return job

    async def finish_job_failed(
        self,
        job: ToolFactoryJob,
        *,
        error_code: str,
        error_message: str,
        finished_at: datetime,
        source_format: str | None = None,
    ) -> ToolFactoryJob:
        job.status = "FAILED"
        job.source_format = source_format
        job.progress_current = 0
        job.progress_total = 1
        job.error_code = error_code[:128]
        job.error_message = error_message[:500]
        job.finished_at = finished_at
        await self._session.flush()
        await self._session.refresh(job)
        return job

    async def get_job(self, job_id: uuid.UUID) -> ToolFactoryJob | None:
        return await self._session.get(ToolFactoryJob, job_id)

    async def list_jobs(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: str | None = None,
        q: str | None = None,
        sort: str = "-created_at",
    ) -> tuple[list[ToolFactoryJob], int]:
        field, direction = parse_sort(
            sort,
            allowed=ALLOWED_FACTORY_JOB_SORT,
            default_field="created_at",
        )
        stmt = select(ToolFactoryJob)

        if status:
            stmt = stmt.where(ToolFactoryJob.status == status)

        if q:
            raw = q.strip()
            if raw:
                escaped = (
                    raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                )
                like = f"%{escaped}%"
                stmt = stmt.where(ToolFactoryJob.source_name.ilike(like, escape="\\"))

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())

        sort_col = getattr(ToolFactoryJob, field)
        order = sort_col.desc() if direction == "desc" else sort_col.asc()
        offset = (page - 1) * page_size
        rows_stmt = stmt.order_by(order, ToolFactoryJob.id.asc()).offset(offset).limit(
            page_size
        )
        rows = list((await self._session.execute(rows_stmt)).scalars().all())
        return rows, total

    async def create_analysis_artifact(
        self,
        *,
        job_id: uuid.UUID,
        artifact_type: str,
        content_type: str,
        content_sha256: str,
        size_bytes: int,
        inline_payload: dict[str, Any],
    ) -> ToolFactoryArtifact:
        artifact = ToolFactoryArtifact(
            job_id=job_id,
            artifact_type=artifact_type,
            content_type=content_type,
            content_sha256=content_sha256,
            size_bytes=size_bytes,
            inline_payload=inline_payload,
        )
        self._session.add(artifact)
        await self._session.flush()
        await self._session.refresh(artifact)
        return artifact

    async def get_analysis_artifact(
        self,
        job_id: uuid.UUID,
        *,
        artifact_type: str,
    ) -> ToolFactoryArtifact | None:
        stmt = select(ToolFactoryArtifact).where(
            ToolFactoryArtifact.job_id == job_id,
            ToolFactoryArtifact.artifact_type == artifact_type,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def count_test_results(self, job_id: uuid.UUID) -> int:
        from app.models.factory import ToolFactoryTestResult

        stmt = select(func.count()).where(ToolFactoryTestResult.job_id == job_id)
        return int((await self._session.execute(stmt)).scalar_one())
