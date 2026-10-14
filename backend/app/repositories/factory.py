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
    ToolFactoryGeneratedPackage,
    ToolFactoryJob,
    ToolFactoryReview,
    ToolFactoryTestRun,
)
from app.schemas.common_page import parse_sort

ALLOWED_FACTORY_JOB_SORT = {"created_at", "started_at", "finished_at", "status"}
ALLOWED_FACTORY_CANDIDATE_SORT = {"created_at", "operation_key", "method", "path"}
ALLOWED_FACTORY_BUILD_SORT = {"created_at"}
ALLOWED_FACTORY_PACKAGE_SORT = {"created_at"}
ALLOWED_FACTORY_TEST_RUN_SORT = {"created_at", "started_at", "finished_at", "status"}


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

    async def get_build_for_update(
        self, build_id: uuid.UUID
    ) -> ToolFactoryBuild | None:
        """Load Build with row lock for serialized package generation."""

        stmt = (
            select(ToolFactoryBuild)
            .where(ToolFactoryBuild.id == build_id)
            .with_for_update()
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

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

    async def create_generated_package(
        self,
        *,
        package_id: uuid.UUID,
        build_id: uuid.UUID,
        job_id: uuid.UUID,
        analysis_artifact_id: uuid.UUID,
        source_sha256: str,
        analyzer_version: str,
        build_input_hash: str,
        build_generator_version: str,
        generator_version: str,
        package_schema_version: str,
        package_content_sha256: str,
        package_size_bytes: int,
        tool_count: int,
        object_bucket: str,
        object_key: str,
        content_type: str,
        requested_by: uuid.UUID,
    ) -> ToolFactoryGeneratedPackage:
        row = ToolFactoryGeneratedPackage(
            id=package_id,
            build_id=build_id,
            job_id=job_id,
            analysis_artifact_id=analysis_artifact_id,
            source_sha256=source_sha256,
            analyzer_version=analyzer_version,
            build_input_hash=build_input_hash,
            build_generator_version=build_generator_version,
            generator_version=generator_version,
            package_schema_version=package_schema_version,
            package_content_sha256=package_content_sha256,
            package_size_bytes=package_size_bytes,
            tool_count=tool_count,
            object_bucket=object_bucket,
            object_key=object_key,
            content_type=content_type,
            requested_by=requested_by,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def get_generated_package(
        self, package_id: uuid.UUID
    ) -> ToolFactoryGeneratedPackage | None:
        return await self._session.get(ToolFactoryGeneratedPackage, package_id)

    async def get_generated_package_for_build(
        self,
        build_id: uuid.UUID,
        *,
        generator_version: str,
    ) -> ToolFactoryGeneratedPackage | None:
        stmt = select(ToolFactoryGeneratedPackage).where(
            ToolFactoryGeneratedPackage.build_id == build_id,
            ToolFactoryGeneratedPackage.generator_version == generator_version,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_generated_packages_for_build(
        self,
        build_id: uuid.UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        sort: str = "-created_at",
    ) -> tuple[list[ToolFactoryGeneratedPackage], int]:
        field, direction = parse_sort(
            sort,
            allowed=ALLOWED_FACTORY_PACKAGE_SORT,
            default_field="created_at",
        )
        stmt = select(ToolFactoryGeneratedPackage).where(
            ToolFactoryGeneratedPackage.build_id == build_id
        )
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())
        sort_col = getattr(ToolFactoryGeneratedPackage, field)
        order = sort_col.desc() if direction == "desc" else sort_col.asc()
        offset = (page - 1) * page_size
        rows_stmt = (
            stmt.order_by(order, ToolFactoryGeneratedPackage.id.asc())
            .offset(offset)
            .limit(page_size)
        )
        rows = list((await self._session.execute(rows_stmt)).scalars().all())
        return rows, total

    async def create_test_run(
        self,
        *,
        test_run_id: uuid.UUID,
        generated_package_id: uuid.UUID,
        build_id: uuid.UUID,
        job_id: uuid.UUID,
        analysis_artifact_id: uuid.UUID,
        package_content_sha256: str,
        generator_version: str,
        package_schema_version: str,
        source_sha256: str,
        analyzer_version: str,
        build_input_hash: str,
        security_policy_version: str,
        contract_policy_version: str,
        status: str,
        requested_by: uuid.UUID,
        queued_at: datetime | None,
    ) -> ToolFactoryTestRun:
        row = ToolFactoryTestRun(
            id=test_run_id,
            generated_package_id=generated_package_id,
            build_id=build_id,
            job_id=job_id,
            analysis_artifact_id=analysis_artifact_id,
            package_content_sha256=package_content_sha256,
            generator_version=generator_version,
            package_schema_version=package_schema_version,
            source_sha256=source_sha256,
            analyzer_version=analyzer_version,
            build_input_hash=build_input_hash,
            security_policy_version=security_policy_version,
            contract_policy_version=contract_policy_version,
            status=status,
            requested_by=requested_by,
            queued_at=queued_at,
            lock_version=1,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def get_test_run(self, test_run_id: uuid.UUID) -> ToolFactoryTestRun | None:
        return await self._session.get(ToolFactoryTestRun, test_run_id)

    async def get_test_run_for_update(
        self, test_run_id: uuid.UUID
    ) -> ToolFactoryTestRun | None:
        stmt = (
            select(ToolFactoryTestRun)
            .where(ToolFactoryTestRun.id == test_run_id)
            .with_for_update()
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_test_run_for_package_policies(
        self,
        *,
        generated_package_id: uuid.UUID,
        security_policy_version: str,
        contract_policy_version: str,
    ) -> ToolFactoryTestRun | None:
        stmt = select(ToolFactoryTestRun).where(
            ToolFactoryTestRun.generated_package_id == generated_package_id,
            ToolFactoryTestRun.security_policy_version == security_policy_version,
            ToolFactoryTestRun.contract_policy_version == contract_policy_version,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_test_runs_for_package(
        self,
        generated_package_id: uuid.UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        sort: str = "-created_at",
    ) -> tuple[list[ToolFactoryTestRun], int]:
        field, direction = parse_sort(
            sort,
            allowed=ALLOWED_FACTORY_TEST_RUN_SORT,
            default_field="created_at",
        )
        stmt = select(ToolFactoryTestRun).where(
            ToolFactoryTestRun.generated_package_id == generated_package_id
        )
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())
        sort_col = getattr(ToolFactoryTestRun, field)
        order = sort_col.desc() if direction == "desc" else sort_col.asc()
        offset = (page - 1) * page_size
        rows_stmt = (
            stmt.order_by(order, ToolFactoryTestRun.id.asc())
            .offset(offset)
            .limit(page_size)
        )
        rows = list((await self._session.execute(rows_stmt)).scalars().all())
        return rows, total

    async def finish_test_run_succeeded(
        self,
        row: ToolFactoryTestRun,
        *,
        security_result: dict[str, Any],
        contract_result: dict[str, Any],
        evidence: dict[str, Any],
        evidence_hash: str,
        finished_at: datetime,
    ) -> ToolFactoryTestRun:
        row.status = "SUCCEEDED"
        row.result = "PASS"
        row.security_result = security_result
        row.contract_result = contract_result
        row.evidence = evidence
        row.evidence_hash = evidence_hash
        row.error_code = None
        row.error_message = None
        row.finished_at = finished_at
        row.worker_id = None
        row.lease_token = None
        row.lease_expires_at = None
        row.lock_version = int(row.lock_version) + 1
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def finish_test_run_failed(
        self,
        row: ToolFactoryTestRun,
        *,
        error_code: str,
        error_message: str,
        finished_at: datetime,
        security_result: dict[str, Any] | None = None,
        contract_result: dict[str, Any] | None = None,
        evidence: dict[str, Any] | None = None,
        evidence_hash: str | None = None,
    ) -> ToolFactoryTestRun:
        row.status = "FAILED"
        row.result = "FAIL"
        row.security_result = security_result
        row.contract_result = contract_result
        row.evidence = evidence
        row.evidence_hash = evidence_hash
        row.error_code = error_code[:128]
        row.error_message = error_message[:500]
        row.finished_at = finished_at
        row.worker_id = None
        row.lease_token = None
        row.lease_expires_at = None
        row.lock_version = int(row.lock_version) + 1
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def create_review(
        self,
        *,
        review_id: uuid.UUID,
        test_run_id: uuid.UUID,
        generated_package_id: uuid.UUID,
        build_id: uuid.UUID,
        job_id: uuid.UUID,
        analysis_artifact_id: uuid.UUID,
        package_content_sha256: str,
        generator_version: str,
        package_schema_version: str,
        source_sha256: str,
        analyzer_version: str,
        build_input_hash: str,
        security_policy_version: str,
        contract_policy_version: str,
        test_evidence_hash: str,
        decision: str,
        comment: str | None,
        review_evidence: dict[str, Any],
        review_evidence_hash: str,
        reviewed_by: uuid.UUID,
        reviewed_at: datetime,
    ) -> ToolFactoryReview:
        row = ToolFactoryReview(
            id=review_id,
            test_run_id=test_run_id,
            generated_package_id=generated_package_id,
            build_id=build_id,
            job_id=job_id,
            analysis_artifact_id=analysis_artifact_id,
            package_content_sha256=package_content_sha256,
            generator_version=generator_version,
            package_schema_version=package_schema_version,
            source_sha256=source_sha256,
            analyzer_version=analyzer_version,
            build_input_hash=build_input_hash,
            security_policy_version=security_policy_version,
            contract_policy_version=contract_policy_version,
            test_evidence_hash=test_evidence_hash,
            decision=decision,
            comment=comment,
            review_evidence=review_evidence,
            review_evidence_hash=review_evidence_hash,
            reviewed_by=reviewed_by,
            reviewed_at=reviewed_at,
            lock_version=1,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def get_review(self, review_id: uuid.UUID) -> ToolFactoryReview | None:
        return await self._session.get(ToolFactoryReview, review_id)

    async def get_review_for_test_run(
        self, test_run_id: uuid.UUID
    ) -> ToolFactoryReview | None:
        stmt = select(ToolFactoryReview).where(
            ToolFactoryReview.test_run_id == test_run_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()
