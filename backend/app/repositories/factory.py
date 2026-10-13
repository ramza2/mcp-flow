"""Persistence-only repository for Tool Factory Jobs (no commit)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.factory import (
    ToolFactoryArtifact,
    ToolFactoryBuild,
    ToolFactoryCandidate,
    ToolFactoryJob,
)
from app.schemas.common_page import parse_sort

ALLOWED_FACTORY_JOB_SORT = {"created_at", "started_at", "finished_at", "status"}
ALLOWED_FACTORY_CANDIDATE_SORT = {"created_at", "operation_key", "method", "path"}
ALLOWED_FACTORY_BUILD_SORT = {"created_at"}


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
        source_bucket: str | None = None,
        source_object_key: str | None = None,
        source_content_type: str | None = None,
        source_size_bytes: int | None = None,
        source_format: str | None = None,
        operation_count: int = 0,
        server_count: int = 0,
        progress_current: int = 0,
        progress_total: int = 1,
        current_phase: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        queued_at: datetime | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        job_id: uuid.UUID | None = None,
    ) -> ToolFactoryJob:
        job = ToolFactoryJob(
            id=job_id or uuid.uuid4(),
            job_type=job_type,
            status=status,
            source_name=source_name,
            source_sha256=source_sha256,
            source_bucket=source_bucket,
            source_object_key=source_object_key,
            source_content_type=source_content_type,
            source_size_bytes=source_size_bytes,
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
            queued_at=queued_at,
            started_at=started_at,
            finished_at=finished_at,
            lock_version=1,
        )
        self._session.add(job)
        await self._session.flush()
        await self._session.refresh(job)
        return job

    async def get_job_for_update(self, job_id: uuid.UUID) -> ToolFactoryJob | None:
        stmt = (
            select(ToolFactoryJob)
            .where(ToolFactoryJob.id == job_id)
            .with_for_update()
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

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
        job.worker_id = None
        job.lease_token = None
        job.lease_expires_at = None
        job.lock_version = int(job.lock_version) + 1
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
        job.worker_id = None
        job.lease_token = None
        job.lease_expires_at = None
        job.lock_version = int(job.lock_version) + 1
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

    async def list_candidate_operation_keys(
        self, job_id: uuid.UUID
    ) -> set[str]:
        stmt = select(ToolFactoryCandidate.operation_key).where(
            ToolFactoryCandidate.job_id == job_id
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return {str(key) for key in rows}

    async def create_candidate(
        self,
        *,
        job_id: uuid.UUID,
        analysis_artifact_id: uuid.UUID,
        source_sha256: str,
        analyzer_version: str,
        operation_key: str,
        method: str,
        path: str,
        operation_id: str | None,
        summary: str | None,
        description: str | None,
        tags: list[Any],
        deprecated: bool,
        parameters: list[Any],
        request_schema: dict[str, Any] | None,
        response_schema: dict[str, Any] | None,
        candidate_id: uuid.UUID | None = None,
    ) -> ToolFactoryCandidate:
        candidate = ToolFactoryCandidate(
            id=candidate_id or uuid.uuid4(),
            job_id=job_id,
            analysis_artifact_id=analysis_artifact_id,
            source_sha256=source_sha256,
            analyzer_version=analyzer_version,
            operation_key=operation_key,
            method=method,
            path=path,
            operation_id=operation_id,
            summary=summary,
            description=description,
            tags=tags,
            deprecated=deprecated,
            parameters=parameters,
            request_schema=request_schema,
            response_schema=response_schema,
        )
        self._session.add(candidate)
        await self._session.flush()
        await self._session.refresh(candidate)
        return candidate

    async def get_candidate(
        self, candidate_id: uuid.UUID
    ) -> ToolFactoryCandidate | None:
        return await self._session.get(ToolFactoryCandidate, candidate_id)

    async def count_candidates(self, job_id: uuid.UUID) -> int:
        stmt = select(func.count()).where(ToolFactoryCandidate.job_id == job_id)
        return int((await self._session.execute(stmt)).scalar_one())

    async def list_candidates_for_job(
        self,
        job_id: uuid.UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        q: str | None = None,
        sort: str = "operation_key",
    ) -> tuple[list[ToolFactoryCandidate], int]:
        field, direction = parse_sort(
            sort,
            allowed=ALLOWED_FACTORY_CANDIDATE_SORT,
            default_field="operation_key",
        )
        stmt = select(ToolFactoryCandidate).where(
            ToolFactoryCandidate.job_id == job_id
        )

        if q:
            raw = q.strip()
            if raw:
                escaped = (
                    raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                )
                like = f"%{escaped}%"
                stmt = stmt.where(
                    ToolFactoryCandidate.operation_key.ilike(like, escape="\\")
                    | ToolFactoryCandidate.path.ilike(like, escape="\\")
                    | ToolFactoryCandidate.method.ilike(like, escape="\\")
                )

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())

        sort_col = getattr(ToolFactoryCandidate, field)
        order = sort_col.desc() if direction == "desc" else sort_col.asc()
        offset = (page - 1) * page_size
        rows_stmt = (
            stmt.order_by(order, ToolFactoryCandidate.id.asc())
            .offset(offset)
            .limit(page_size)
        )
        rows = list((await self._session.execute(rows_stmt)).scalars().all())
        return rows, total

    async def get_candidates_by_ids(
        self, candidate_ids: list[uuid.UUID]
    ) -> list[ToolFactoryCandidate]:
        if not candidate_ids:
            return []
        stmt = select(ToolFactoryCandidate).where(
            ToolFactoryCandidate.id.in_(candidate_ids)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def create_build(
        self,
        *,
        job_id: uuid.UUID,
        analysis_artifact_id: uuid.UUID,
        source_sha256: str,
        analyzer_version: str,
        generator_version: str,
        candidate_ids: list[str],
        candidate_count: int,
        candidates_snapshot: list[Any],
        candidates_snapshot_hash: str,
        build_input_hash: str,
        requested_by: uuid.UUID,
        build_id: uuid.UUID | None = None,
    ) -> ToolFactoryBuild:
        build = ToolFactoryBuild(
            id=build_id or uuid.uuid4(),
            job_id=job_id,
            analysis_artifact_id=analysis_artifact_id,
            source_sha256=source_sha256,
            analyzer_version=analyzer_version,
            generator_version=generator_version,
            candidate_ids=candidate_ids,
            candidate_count=candidate_count,
            candidates_snapshot=candidates_snapshot,
            candidates_snapshot_hash=candidates_snapshot_hash,
            build_input_hash=build_input_hash,
            requested_by=requested_by,
        )
        self._session.add(build)
        await self._session.flush()
        await self._session.refresh(build)
        return build

    async def get_build(self, build_id: uuid.UUID) -> ToolFactoryBuild | None:
        return await self._session.get(ToolFactoryBuild, build_id)

    async def list_builds_for_job(
        self,
        job_id: uuid.UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        sort: str = "-created_at",
    ) -> tuple[list[ToolFactoryBuild], int]:
        field, direction = parse_sort(
            sort,
            allowed=ALLOWED_FACTORY_BUILD_SORT,
            default_field="created_at",
        )
        stmt = select(ToolFactoryBuild).where(ToolFactoryBuild.job_id == job_id)
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())
        sort_col = getattr(ToolFactoryBuild, field)
        order = sort_col.desc() if direction == "desc" else sort_col.asc()
        offset = (page - 1) * page_size
        rows_stmt = (
            stmt.order_by(order, ToolFactoryBuild.id.asc())
            .offset(offset)
            .limit(page_size)
        )
        rows = list((await self._session.execute(rows_stmt)).scalars().all())
        return rows, total
