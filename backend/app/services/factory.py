"""Tool Factory durable OpenAPI analysis Job service."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from fastapi import status
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.canonical_hash import compute_canonical_json_hash
from app.core.errors import AppError
from app.domain.enums import JobStatus, UserStatus
from app.factory import (
    ARTIFACT_CONTENT_TYPE_JSON,
    ARTIFACT_TYPE_OPENAPI_ANALYSIS,
    JOB_TYPE_OPENAPI_ANALYZE,
    OPENAPI_ANALYZER_VERSION,
    PHASE_ANALYZE,
)
from app.factory.contracts import FactoryOpenAPIAnalysis
from app.factory.errors import FactoryAnalysisError
from app.factory.openapi_analyzer import MAX_SOURCE_BYTES, analyze_openapi
from app.repositories.factory import FactoryRepository
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.user import UserRepository
from app.schemas.factory import (
    FactoryArtifactSummary,
    FactoryJobDetailResponse,
    FactoryJobListResponse,
    FactoryJobResponse,
)
from app.services.authorization import AuthorizationResolver

_MCP_SERVER_READ = "mcp.server.read"
_MCP_SERVER_MANAGE = "mcp.server.manage"

_OPERATION_SCOPE = "FACTORY_OPENAPI_ANALYZE_V1"
_RESOURCE_TYPE = "TOOL_FACTORY_JOB"
_IDEMPOTENCY_KEY_MAX_LEN = 128
_IDEMPOTENCY_PK_NAME = "pk_api_idempotency_records"

_ALLOWED_EXTENSIONS = {".json", ".yaml", ".yml"}
_FALLBACK_SOURCE_NAME = "openapi.json"
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class FactoryJobCreateOutcome:
    result: FactoryJobResponse
    http_status: int
    replayed: bool


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _request_hash(*, source_name: str, source_sha256: str) -> str:
    return compute_canonical_json_hash(
        {
            "job_type": JOB_TYPE_OPENAPI_ANALYZE,
            "source_name": source_name,
            "source_sha256": source_sha256,
        }
    )


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


def _is_idempotency_pk_violation(exc: IntegrityError) -> bool:
    name = _constraint_name(exc)
    if name == _IDEMPOTENCY_PK_NAME:
        return True
    orig = getattr(exc, "orig", None)
    msg = str(orig if orig is not None else exc)
    if _IDEMPOTENCY_PK_NAME in msg:
        return True
    lower = msg.lower()
    return (
        "unique constraint failed" in lower
        and "api_idempotency_records" in lower
        and "idempotency_key" in lower
    )


def normalize_source_name(filename: str | None) -> str:
    """Basename-only safe name; never treat as a filesystem path."""

    if not filename or not str(filename).strip():
        return _FALLBACK_SOURCE_NAME
    raw = str(filename).strip().replace("\x00", "")
    # Drop any directory components from POSIX or Windows-style paths.
    name = PureWindowsPath(raw).name
    name = PurePosixPath(name).name
    name = _CONTROL_RE.sub("", name).strip()
    if not name or name in {".", ".."}:
        return _FALLBACK_SOURCE_NAME
    if len(name) > 255:
        name = name[:255]
    return name


def validate_source_extension(source_name: str) -> None:
    lower = source_name.lower()
    if not any(lower.endswith(ext) for ext in _ALLOWED_EXTENSIONS):
        raise AppError(
            code="VALIDATION_ERROR",
            message="Factory source filename must end with .json, .yaml, or .yml.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )


def ingest_source_bytes(data: bytes) -> bytes:
    """Bound uploaded bytes; never persist truncated content."""

    if not isinstance(data, (bytes, bytearray)):
        raise AppError(
            code="VALIDATION_ERROR",
            message="Factory source must be raw bytes.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    raw = bytes(data)
    if len(raw) == 0:
        raise AppError(
            code="VALIDATION_ERROR",
            message="Factory source file is empty.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    if len(raw) > MAX_SOURCE_BYTES:
        raise AppError(
            code="FACTORY_SOURCE_TOO_LARGE",
            message=f"Factory source exceeds maximum size of {MAX_SOURCE_BYTES} bytes.",
            status_code=413,
        )
    return raw


class FactoryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = FactoryRepository(session)
        self._users = UserRepository(session)
        self._authz = AuthorizationResolver(session)
        self._idempotency = IdempotencyRepository(session)

    async def _assert_active(self, actor_user_id: uuid.UUID) -> None:
        user = await self._users.get(actor_user_id)
        if user is None or user.status != UserStatus.ACTIVE.value:
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Actor is not an ACTIVE user.",
                status_code=status.HTTP_403_FORBIDDEN,
            )

    async def _assert_read(self, actor_user_id: uuid.UUID) -> None:
        await self._assert_active(actor_user_id)
        if not await self._authz.has_permission(actor_user_id, _MCP_SERVER_READ):
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Missing mcp.server.read permission.",
                status_code=status.HTTP_403_FORBIDDEN,
            )

    async def _assert_manage(self, actor_user_id: uuid.UUID) -> None:
        await self._assert_active(actor_user_id)
        if not await self._authz.has_permission(actor_user_id, _MCP_SERVER_MANAGE):
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Missing mcp.server.manage permission.",
                status_code=status.HTTP_403_FORBIDDEN,
            )

    def _job_to_response(self, job: Any) -> FactoryJobResponse:
        return FactoryJobResponse(
            id=job.id,
            job_type=job.job_type,
            status=JobStatus(job.status),
            source_name=job.source_name,
            source_sha256=job.source_sha256,
            source_format=job.source_format,
            analyzer_version=job.analyzer_version,
            operation_count=job.operation_count,
            server_count=job.server_count,
            progress_current=job.progress_current,
            progress_total=job.progress_total,
            current_phase=job.current_phase,
            error_code=job.error_code,
            error_message=job.error_message,
            requested_by=job.requested_by,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )

    async def create_openapi_analyze_job(
        self,
        *,
        actor_user_id: uuid.UUID,
        source_bytes: bytes,
        filename: str | None,
        idempotency_key: str,
    ) -> FactoryJobCreateOutcome:
        await self._assert_manage(actor_user_id)

        key = idempotency_key.strip()
        if not key:
            raise AppError(
                code="VALIDATION_ERROR",
                message="Idempotency-Key must be a non-empty string.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        if len(key) > _IDEMPOTENCY_KEY_MAX_LEN:
            raise AppError(
                code="VALIDATION_ERROR",
                message=(
                    f"Idempotency-Key must be at most "
                    f"{_IDEMPOTENCY_KEY_MAX_LEN} characters."
                ),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        # Input ingestion failures must not consume the idempotency key.
        source_name = normalize_source_name(filename)
        validate_source_extension(source_name)
        raw = ingest_source_bytes(source_bytes)
        source_sha256 = hashlib.sha256(raw).hexdigest()

        principal_key = str(actor_user_id)
        req_hash = _request_hash(source_name=source_name, source_sha256=source_sha256)

        existing = await self._idempotency.get(
            principal_key=principal_key,
            operation_scope=_OPERATION_SCOPE,
            idempotency_key=key,
        )
        if existing is not None:
            return await self._replay_or_conflict(existing, req_hash)

        try:
            outcome = await self._analyze_and_persist(
                actor_user_id=actor_user_id,
                raw=raw,
                source_name=source_name,
                source_sha256=source_sha256,
                principal_key=principal_key,
                idempotency_key=key,
                request_hash=req_hash,
            )
            await self._session.commit()
            return outcome
        except IntegrityError as exc:
            await self._session.rollback()
            if not _is_idempotency_pk_violation(exc):
                raise
            raced = await self._idempotency.get(
                principal_key=principal_key,
                operation_scope=_OPERATION_SCOPE,
                idempotency_key=key,
            )
            if raced is None:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="Factory Job 생성 중 충돌이 발생했습니다.",
                    status_code=status.HTTP_409_CONFLICT,
                ) from exc
            return await self._replay_or_conflict(raced, req_hash)

    async def _analyze_and_persist(
        self,
        *,
        actor_user_id: uuid.UUID,
        raw: bytes,
        source_name: str,
        source_sha256: str,
        principal_key: str,
        idempotency_key: str,
        request_hash: str,
    ) -> FactoryJobCreateOutcome:
        now = _utc_now()
        job = await self._repo.create_job(
            job_type=JOB_TYPE_OPENAPI_ANALYZE,
            status=JobStatus.RUNNING.value,
            source_name=source_name,
            source_sha256=source_sha256,
            analyzer_version=OPENAPI_ANALYZER_VERSION,
            requested_by=actor_user_id,
            progress_current=0,
            progress_total=1,
            current_phase=PHASE_ANALYZE,
            started_at=now,
        )

        try:
            analysis = analyze_openapi(raw, filename=source_name)
        except FactoryAnalysisError as exc:
            finished = _utc_now()
            job = await self._repo.finish_job_failed(
                job,
                error_code=exc.code,
                error_message=exc.message,
                finished_at=finished,
            )
            response = self._job_to_response(job)
            await self._idempotency.create_completed(
                principal_key=principal_key,
                operation_scope=_OPERATION_SCOPE,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response_status=status.HTTP_201_CREATED,
                response_body=response.model_dump(mode="json"),
                resource_type=_RESOURCE_TYPE,
                resource_id=job.id,
                completed_at=finished,
            )
            return FactoryJobCreateOutcome(
                result=response,
                http_status=status.HTTP_201_CREATED,
                replayed=False,
            )

        payload = analysis.model_dump(mode="json")
        encoded = _canonical_json_bytes(payload)
        content_sha256 = compute_canonical_json_hash(payload)

        await self._repo.create_analysis_artifact(
            job_id=job.id,
            artifact_type=ARTIFACT_TYPE_OPENAPI_ANALYSIS,
            content_type=ARTIFACT_CONTENT_TYPE_JSON,
            content_sha256=content_sha256,
            size_bytes=len(encoded),
            inline_payload=payload,
        )
        finished = _utc_now()
        job = await self._repo.finish_job_success(
            job,
            source_format=analysis.source_format,
            operation_count=len(analysis.operations),
            server_count=len(analysis.servers),
            finished_at=finished,
        )
        response = self._job_to_response(job)
        await self._idempotency.create_completed(
            principal_key=principal_key,
            operation_scope=_OPERATION_SCOPE,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            response_status=status.HTTP_201_CREATED,
            response_body=response.model_dump(mode="json"),
            resource_type=_RESOURCE_TYPE,
            resource_id=job.id,
            completed_at=finished,
        )
        return FactoryJobCreateOutcome(
            result=response,
            http_status=status.HTTP_201_CREATED,
            replayed=False,
        )

    async def _replay_or_conflict(
        self,
        record: Any,
        request_hash: str,
    ) -> FactoryJobCreateOutcome:
        if record.request_hash != request_hash:
            raise AppError(
                code="IDEMPOTENCY_KEY_REUSED",
                message="Idempotency-Key가 다른 요청에 재사용되었습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if record.status != "COMPLETED":
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency record가 COMPLETED가 아닙니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if record.resource_type != _RESOURCE_TYPE or record.resource_id is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency resource metadata가 손상되었습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        job = await self._repo.get_job(record.resource_id)
        if job is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency가 가리키는 Factory Job이 없습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if record.response_status != status.HTTP_201_CREATED or record.response_body is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency response snapshot이 손상되었습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        try:
            result = FactoryJobResponse.model_validate(record.response_body)
        except ValidationError as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency response snapshot이 유효하지 않습니다.",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc
        if result.id != record.resource_id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency response snapshot id가 resource_id와 불일치합니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        # Prefer live job row for current safe metadata (FAILED/SUCCEEDED).
        live = self._job_to_response(job)
        return FactoryJobCreateOutcome(
            result=live,
            http_status=int(record.response_status),
            replayed=True,
        )

    async def list_jobs(
        self,
        actor_user_id: uuid.UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        status_filter: str | None = None,
        q: str | None = None,
        sort: str = "-created_at",
    ) -> FactoryJobListResponse:
        await self._assert_read(actor_user_id)
        if status_filter is not None:
            try:
                JobStatus(status_filter)
            except ValueError as exc:
                raise AppError(
                    code="VALIDATION_ERROR",
                    message="Invalid JobStatus filter.",
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                ) from exc
        rows, total = await self._repo.list_jobs(
            page=page,
            page_size=page_size,
            status=status_filter,
            q=q,
            sort=sort,
        )
        items = [self._job_to_response(row) for row in rows]
        return FactoryJobListResponse(
            items=items,
            page=page,
            page_size=page_size,
            total=total,
            has_next=(page * page_size) < total,
        )

    async def get_job(
        self,
        actor_user_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> FactoryJobDetailResponse:
        await self._assert_read(actor_user_id)
        job = await self._repo.get_job(job_id)
        if job is None:
            raise AppError(
                code="NOT_FOUND",
                message="Factory Job을 찾을 수 없습니다.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        base = self._job_to_response(job)
        artifact = await self._repo.get_analysis_artifact(
            job.id,
            artifact_type=ARTIFACT_TYPE_OPENAPI_ANALYSIS,
        )
        analysis: FactoryOpenAPIAnalysis | None = None
        artifact_summary: FactoryArtifactSummary | None = None
        if artifact is not None:
            artifact_summary = FactoryArtifactSummary(
                id=artifact.id,
                artifact_type=artifact.artifact_type,
                content_sha256=artifact.content_sha256,
                size_bytes=artifact.size_bytes,
                created_at=artifact.created_at,
            )
            if artifact.inline_payload is not None:
                analysis = FactoryOpenAPIAnalysis.model_validate(artifact.inline_payload)
        test_count = await self._repo.count_test_results(job.id)
        return FactoryJobDetailResponse(
            **base.model_dump(),
            analysis=analysis,
            analysis_artifact=artifact_summary,
            test_results=[],
            test_result_count=test_count,
        )
