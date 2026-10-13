"""Tool Factory durable OpenAPI analysis Job service (async Object Storage handoff)."""

from __future__ import annotations

import hashlib
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
from app.core.config import get_settings
from app.core.errors import AppError
from app.domain.enums import JobStatus, UserStatus
from app.factory import (
    ARTIFACT_TYPE_OPENAPI_ANALYSIS,
    FACTORY_BUILD_GENERATOR_VERSION,
    JOB_TYPE_OPENAPI_ANALYZE,
    OPENAPI_ANALYZER_VERSION,
    PHASE_ANALYZE,
)
from app.factory.contracts import FactoryOpenAPIAnalysis
from app.factory.errors import (
    FACTORY_SOURCE_OBJECT_STORE_FAILED,
)
from app.factory.openapi_analyzer import MAX_OPERATIONS, MAX_SOURCE_BYTES
from app.infrastructure.object_storage import (
    ObjectStorageClient,
    ObjectStorageError,
    get_object_storage,
)
from app.models.factory import ToolFactoryArtifact, ToolFactoryCandidate, ToolFactoryJob
from app.repositories.factory import FactoryRepository
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.outbox import OutboxRepository
from app.repositories.user import UserRepository
from app.schemas.factory import (
    FactoryArtifactSummary,
    FactoryBuildListResponse,
    FactoryBuildResponse,
    FactoryCandidateListResponse,
    FactoryCandidateMaterializeResponse,
    FactoryCandidateResponse,
    FactoryJobDetailResponse,
    FactoryJobListResponse,
    FactoryJobResponse,
)
from app.services.authorization import AuthorizationResolver

_MCP_SERVER_READ = "mcp.server.read"
_MCP_SERVER_MANAGE = "mcp.server.manage"

_OPERATION_SCOPE = "FACTORY_OPENAPI_ANALYZE_V1"
_RESOURCE_TYPE = "TOOL_FACTORY_JOB"
_BUILD_OPERATION_SCOPE = "FACTORY_BUILD_CREATE_V1"
_BUILD_RESOURCE_TYPE = "TOOL_FACTORY_BUILD"
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


@dataclass(frozen=True, slots=True)
class FactoryBuildCreateOutcome:
    result: FactoryBuildResponse
    http_status: int
    replayed: bool


def _normalize_idempotency_key(idempotency_key: str) -> str:
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
    return key


def _candidate_contract_snapshot(candidate: ToolFactoryCandidate) -> dict[str, Any]:
    """Immutable sanitized candidate contract for Build snapshot hashing."""

    tags = candidate.tags if isinstance(candidate.tags, list) else []
    parameters = candidate.parameters if isinstance(candidate.parameters, list) else []
    return {
        "candidate_id": str(candidate.id),
        "job_id": str(candidate.job_id),
        "analysis_artifact_id": str(candidate.analysis_artifact_id),
        "source_sha256": candidate.source_sha256,
        "analyzer_version": candidate.analyzer_version,
        "operation_key": candidate.operation_key,
        "method": candidate.method,
        "path": candidate.path,
        "operation_id": candidate.operation_id,
        "summary": candidate.summary,
        "description": candidate.description,
        "tags": [str(t) for t in tags],
        "deprecated": bool(candidate.deprecated),
        "parameters": parameters,
        "request_schema": candidate.request_schema,
        "response_schema": candidate.response_schema,
    }


def _build_request_hash(
    *,
    job_id: uuid.UUID,
    candidate_ids: list[uuid.UUID],
) -> str:
    return compute_canonical_json_hash(
        {
            "job_id": str(job_id),
            "candidate_ids": [str(cid) for cid in candidate_ids],
        }
    )


def _build_input_hash_payload(
    *,
    job_id: uuid.UUID,
    analysis_artifact_id: uuid.UUID,
    source_sha256: str,
    analyzer_version: str,
    generator_version: str,
    candidate_ids: list[str],
    candidates_snapshot_hash: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "job_id": str(job_id),
        "analysis_artifact_id": str(analysis_artifact_id),
        "source_sha256": source_sha256,
        "analyzer_version": analyzer_version,
        "generator_version": generator_version,
        "candidate_ids": list(candidate_ids),
        "candidates_snapshot_hash": candidates_snapshot_hash,
    }


def _utc_now() -> datetime:
    return datetime.now(UTC)


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


def _guess_content_type(source_name: str) -> str:
    lower = source_name.lower()
    if lower.endswith(".yaml") or lower.endswith(".yml"):
        return "application/yaml"
    return "application/json"


def build_source_object_key(*, job_id: uuid.UUID) -> str:
    """Server-generated non-guessable Object Storage key (no credentials/secrets)."""

    return f"factory/openapi/{job_id.hex}/{uuid.uuid4().hex}"


class FactoryService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        storage: ObjectStorageClient | None = None,
    ) -> None:
        self._session = session
        self._repo = FactoryRepository(session)
        self._users = UserRepository(session)
        self._authz = AuthorizationResolver(session)
        self._idempotency = IdempotencyRepository(session)
        self._outbox = OutboxRepository(session)
        self._storage = storage

    def _storage_client(self) -> ObjectStorageClient:
        if self._storage is not None:
            return self._storage
        return get_object_storage()

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

        key = _normalize_idempotency_key(idempotency_key)

        # Input ingestion failures must not consume the idempotency key.
        source_name = normalize_source_name(filename)
        validate_source_extension(source_name)
        raw = ingest_source_bytes(source_bytes)
        source_sha256 = hashlib.sha256(raw).hexdigest()
        content_type = _guess_content_type(source_name)
        size_bytes = len(raw)

        principal_key = str(actor_user_id)
        req_hash = _request_hash(source_name=source_name, source_sha256=source_sha256)

        existing = await self._idempotency.get(
            principal_key=principal_key,
            operation_scope=_OPERATION_SCOPE,
            idempotency_key=key,
        )
        if existing is not None:
            return await self._replay_or_conflict(existing, req_hash)

        settings = get_settings()
        bucket = settings.object_storage_bucket
        job_id = uuid.uuid4()
        object_key = build_source_object_key(job_id=job_id)
        storage = self._storage_client()

        try:
            await storage.ensure_bucket(bucket)
            await storage.put_bytes(
                bucket=bucket,
                object_key=object_key,
                data=raw,
                content_type=content_type,
            )
        except ObjectStorageError as exc:
            raise AppError(
                code=exc.code or FACTORY_SOURCE_OBJECT_STORE_FAILED,
                message=exc.message,
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            ) from exc

        try:
            outcome = await self._persist_queued_job(
                job_id=job_id,
                actor_user_id=actor_user_id,
                source_name=source_name,
                source_sha256=source_sha256,
                source_bucket=bucket,
                source_object_key=object_key,
                source_content_type=content_type,
                source_size_bytes=size_bytes,
                principal_key=principal_key,
                idempotency_key=key,
                request_hash=req_hash,
            )
            # Durable Outbox is written in the same TX. Broker publish is the
            # outbox poller's responsibility — never SoT for Job status.
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            # Loser of the idempotency race must not leave an orphan object.
            await storage.delete_object(bucket=bucket, object_key=object_key)
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
        except Exception:
            await self._session.rollback()
            await storage.delete_object(bucket=bucket, object_key=object_key)
            raise
        return outcome

    async def _persist_queued_job(
        self,
        *,
        job_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        source_name: str,
        source_sha256: str,
        source_bucket: str,
        source_object_key: str,
        source_content_type: str,
        source_size_bytes: int,
        principal_key: str,
        idempotency_key: str,
        request_hash: str,
    ) -> FactoryJobCreateOutcome:
        now = _utc_now()
        job = await self._repo.create_job(
            job_id=job_id,
            job_type=JOB_TYPE_OPENAPI_ANALYZE,
            status=JobStatus.QUEUED.value,
            source_name=source_name,
            source_sha256=source_sha256,
            source_bucket=source_bucket,
            source_object_key=source_object_key,
            source_content_type=source_content_type,
            source_size_bytes=source_size_bytes,
            analyzer_version=OPENAPI_ANALYZER_VERSION,
            requested_by=actor_user_id,
            progress_current=0,
            progress_total=1,
            current_phase=PHASE_ANALYZE,
            queued_at=now,
        )
        await self._outbox.create_factory_job_dispatch(
            job_id=job.id,
            created_at=now,
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
            completed_at=now,
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
        candidate_count = await self._repo.count_candidates(job.id)
        return FactoryJobDetailResponse(
            **base.model_dump(),
            analysis=analysis,
            analysis_artifact=artifact_summary,
            test_results=[],
            test_result_count=test_count,
            candidate_count=candidate_count,
        )

    def _candidate_to_response(self, row: Any) -> FactoryCandidateResponse:
        tags = row.tags if isinstance(row.tags, list) else []
        parameters = row.parameters if isinstance(row.parameters, list) else []
        return FactoryCandidateResponse(
            id=row.id,
            job_id=row.job_id,
            analysis_artifact_id=row.analysis_artifact_id,
            source_sha256=row.source_sha256,
            analyzer_version=row.analyzer_version,
            operation_key=row.operation_key,
            method=row.method,
            path=row.path,
            operation_id=row.operation_id,
            summary=row.summary,
            description=row.description,
            tags=[str(t) for t in tags],
            deprecated=bool(row.deprecated),
            parameters=parameters,
            request_schema=row.request_schema,
            response_schema=row.response_schema,
            created_at=row.created_at,
        )

    async def materialize_candidates_from_analysis(
        self,
        *,
        job: ToolFactoryJob,
        artifact: ToolFactoryArtifact,
        analysis: FactoryOpenAPIAnalysis | None = None,
    ) -> int:
        """Persist one Candidate Tool per analyzed operation (idempotent).

        Derives only from the sanitized OPENAPI_ANALYSIS artifact — never
        re-parses raw OpenAPI.

        All-or-nothing for a single materialization attempt: new inserts are
        wrapped in one outer savepoint so a mid-batch persistence error does
        not leave a partial candidate set. Per-row nested savepoints still
        absorb concurrent UNIQUE(job_id, operation_key) races.
        """
        if analysis is None:
            if artifact.inline_payload is None:
                return 0
            analysis = FactoryOpenAPIAnalysis.model_validate(artifact.inline_payload)

        existing_keys = await self._repo.list_candidate_operation_keys(job.id)
        pending = [
            operation
            for operation in analysis.operations
            if operation.operation_key not in existing_keys
        ]
        if not pending:
            return 0

        created = 0
        async with self._session.begin_nested():
            for operation in pending:
                try:
                    async with self._session.begin_nested():
                        await self._repo.create_candidate(
                            job_id=job.id,
                            analysis_artifact_id=artifact.id,
                            source_sha256=job.source_sha256,
                            analyzer_version=job.analyzer_version,
                            operation_key=operation.operation_key,
                            method=operation.method,
                            path=operation.path,
                            operation_id=operation.operation_id,
                            summary=operation.summary,
                            description=operation.description,
                            tags=list(operation.tags),
                            deprecated=bool(operation.deprecated),
                            parameters=[
                                p.model_dump(mode="json") for p in operation.parameters
                            ],
                            request_schema=operation.request_schema,
                            response_schema=operation.response_schema,
                        )
                    created += 1
                    existing_keys.add(operation.operation_key)
                except IntegrityError:
                    # Concurrent materialization of the same (job_id, operation_key).
                    existing_keys.add(operation.operation_key)
        return created

    async def list_candidates_for_job(
        self,
        actor_user_id: uuid.UUID,
        job_id: uuid.UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        q: str | None = None,
        sort: str = "operation_key",
    ) -> FactoryCandidateListResponse:
        """Read-only list of already-persisted Candidate Tools (no materialize)."""
        await self._assert_read(actor_user_id)
        job = await self._repo.get_job(job_id)
        if job is None:
            raise AppError(
                code="NOT_FOUND",
                message="Factory Job을 찾을 수 없습니다.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        rows, total = await self._repo.list_candidates_for_job(
            job.id,
            page=page,
            page_size=page_size,
            q=q,
            sort=sort,
        )
        return FactoryCandidateListResponse(
            items=[self._candidate_to_response(row) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
            has_next=(page * page_size) < total,
            job_id=job.id,
        )

    async def materialize_candidates_for_job(
        self,
        actor_user_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> FactoryCandidateMaterializeResponse:
        """Explicit durable materialization from a SUCCEEDED Job's analysis artifact.

        Commits in this service (same pattern as Job create). GET list/detail
        never write candidates.
        """
        await self._assert_manage(actor_user_id)
        job = await self._repo.get_job(job_id)
        if job is None:
            raise AppError(
                code="NOT_FOUND",
                message="Factory Job을 찾을 수 없습니다.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if job.status != JobStatus.SUCCEEDED.value:
            raise AppError(
                code="VALIDATION_ERROR",
                message=(
                    "Candidate Tools can only be materialized from a "
                    "SUCCEEDED OPENAPI_ANALYZE Job."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )
        artifact = await self._repo.get_analysis_artifact(
            job.id,
            artifact_type=ARTIFACT_TYPE_OPENAPI_ANALYSIS,
        )
        if artifact is None or artifact.inline_payload is None:
            raise AppError(
                code="NOT_FOUND",
                message="OPENAPI_ANALYSIS artifact를 찾을 수 없습니다.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        analysis = FactoryOpenAPIAnalysis.model_validate(artifact.inline_payload)
        created = await self.materialize_candidates_from_analysis(
            job=job,
            artifact=artifact,
            analysis=analysis,
        )
        await self._session.commit()
        total = await self._repo.count_candidates(job.id)
        return FactoryCandidateMaterializeResponse(
            job_id=job.id,
            created_count=created,
            total_count=total,
        )

    async def get_candidate(
        self,
        actor_user_id: uuid.UUID,
        candidate_id: uuid.UUID,
    ) -> FactoryCandidateResponse:
        await self._assert_read(actor_user_id)
        row = await self._repo.get_candidate(candidate_id)
        if row is None:
            raise AppError(
                code="NOT_FOUND",
                message="Factory Candidate Tool을 찾을 수 없습니다.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return self._candidate_to_response(row)

    def _build_to_response(self, build: Any) -> FactoryBuildResponse:
        raw_ids = build.candidate_ids if isinstance(build.candidate_ids, list) else []
        return FactoryBuildResponse(
            id=build.id,
            job_id=build.job_id,
            analysis_artifact_id=build.analysis_artifact_id,
            source_sha256=build.source_sha256,
            analyzer_version=build.analyzer_version,
            generator_version=build.generator_version,
            candidate_ids=[uuid.UUID(str(cid)) for cid in raw_ids],
            candidate_count=int(build.candidate_count),
            candidates_snapshot=build.candidates_snapshot or [],
            candidates_snapshot_hash=build.candidates_snapshot_hash,
            build_input_hash=build.build_input_hash,
            requested_by=build.requested_by,
            created_at=build.created_at,
        )

    async def _replay_or_conflict_build(
        self,
        record: Any,
        request_hash: str,
    ) -> FactoryBuildCreateOutcome:
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
        if (
            record.resource_type != _BUILD_RESOURCE_TYPE
            or record.resource_id is None
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency resource metadata가 손상되었습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        build = await self._repo.get_build(record.resource_id)
        if build is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency가 가리키는 Factory Build가 없습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if (
            record.response_status != status.HTTP_201_CREATED
            or record.response_body is None
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency response snapshot이 손상되었습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        try:
            result = FactoryBuildResponse.model_validate(record.response_body)
        except ValidationError as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency response snapshot이 유효하지 않습니다.",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc
        if result.id != record.resource_id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency response snapshot이 손상되었습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        return FactoryBuildCreateOutcome(
            result=result,
            http_status=status.HTTP_201_CREATED,
            replayed=True,
        )

    async def create_build(
        self,
        *,
        actor_user_id: uuid.UUID,
        job_id: uuid.UUID,
        candidate_ids: list[uuid.UUID],
        idempotency_key: str,
    ) -> FactoryBuildCreateOutcome:
        """Create a durable Build from an explicit Candidate subset.

        Validates OPENAPI_ANALYZE + SUCCEEDED Job / OPENAPI_ANALYSIS artifact
        lineage, snapshots candidate contracts immutably, and commits with
        Idempotency-Key.
        """
        await self._assert_manage(actor_user_id)
        key = _normalize_idempotency_key(idempotency_key)

        # Fail-closed selection validation before consuming the idempotency key.
        if not candidate_ids:
            raise AppError(
                code="VALIDATION_ERROR",
                message="Build requires a non-empty candidate_ids selection.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        if len(candidate_ids) > MAX_OPERATIONS:
            raise AppError(
                code="VALIDATION_ERROR",
                message=(
                    f"Build candidate_ids must not exceed {MAX_OPERATIONS} "
                    "(analyzer MAX_OPERATIONS)."
                ),
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        if len(candidate_ids) != len(set(candidate_ids)):
            raise AppError(
                code="VALIDATION_ERROR",
                message="Build candidate_ids must not contain duplicates.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        # Deterministic order for hashing / persistence (stable UUID sort).
        ordered_ids = sorted(candidate_ids, key=lambda cid: cid.hex)
        req_hash = _build_request_hash(job_id=job_id, candidate_ids=ordered_ids)
        principal_key = str(actor_user_id)

        existing = await self._idempotency.get(
            principal_key=principal_key,
            operation_scope=_BUILD_OPERATION_SCOPE,
            idempotency_key=key,
        )
        if existing is not None:
            return await self._replay_or_conflict_build(existing, req_hash)

        job = await self._repo.get_job(job_id)
        if job is None:
            raise AppError(
                code="NOT_FOUND",
                message="Factory Job을 찾을 수 없습니다.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if job.job_type != JOB_TYPE_OPENAPI_ANALYZE:
            raise AppError(
                code="VALIDATION_ERROR",
                message=(
                    "Builds can only be created from an OPENAPI_ANALYZE Job."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )
        if job.status != JobStatus.SUCCEEDED.value:
            raise AppError(
                code="VALIDATION_ERROR",
                message=(
                    "Builds can only be created from a SUCCEEDED "
                    "OPENAPI_ANALYZE Job."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )
        artifact = await self._repo.get_analysis_artifact(
            job.id,
            artifact_type=ARTIFACT_TYPE_OPENAPI_ANALYSIS,
        )
        if artifact is None or artifact.inline_payload is None:
            raise AppError(
                code="NOT_FOUND",
                message="OPENAPI_ANALYSIS artifact를 찾을 수 없습니다.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        rows = await self._repo.get_candidates_by_ids(ordered_ids)
        by_id = {row.id: row for row in rows}
        missing = [cid for cid in ordered_ids if cid not in by_id]
        if missing:
            raise AppError(
                code="NOT_FOUND",
                message="One or more Candidate Tools were not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        for cid in ordered_ids:
            row = by_id[cid]
            if (
                row.job_id != job.id
                or row.analysis_artifact_id != artifact.id
                or row.source_sha256 != job.source_sha256
                or row.analyzer_version != job.analyzer_version
            ):
                raise AppError(
                    code="VALIDATION_ERROR",
                    message=(
                        "All selected Candidate Tools must belong to the same "
                        "SUCCEEDED Job/analysis lineage."
                    ),
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )

        # Snapshot sorted by operation_key then candidate_id for determinism.
        snapshot_rows = sorted(
            (by_id[cid] for cid in ordered_ids),
            key=lambda c: (c.operation_key, c.id.hex),
        )
        candidates_snapshot = [
            _candidate_contract_snapshot(row) for row in snapshot_rows
        ]
        candidates_snapshot_hash = compute_canonical_json_hash(candidates_snapshot)
        candidate_id_strs = [str(cid) for cid in ordered_ids]
        build_input_hash = compute_canonical_json_hash(
            _build_input_hash_payload(
                job_id=job.id,
                analysis_artifact_id=artifact.id,
                source_sha256=job.source_sha256,
                analyzer_version=job.analyzer_version,
                generator_version=FACTORY_BUILD_GENERATOR_VERSION,
                candidate_ids=candidate_id_strs,
                candidates_snapshot_hash=candidates_snapshot_hash,
            )
        )

        build_id = uuid.uuid4()
        now = _utc_now()
        try:
            build = await self._repo.create_build(
                build_id=build_id,
                job_id=job.id,
                analysis_artifact_id=artifact.id,
                source_sha256=job.source_sha256,
                analyzer_version=job.analyzer_version,
                generator_version=FACTORY_BUILD_GENERATOR_VERSION,
                candidate_ids=candidate_id_strs,
                candidate_count=len(candidate_id_strs),
                candidates_snapshot=candidates_snapshot,
                candidates_snapshot_hash=candidates_snapshot_hash,
                build_input_hash=build_input_hash,
                requested_by=actor_user_id,
            )
            response = self._build_to_response(build)
            await self._idempotency.create_completed(
                principal_key=principal_key,
                operation_scope=_BUILD_OPERATION_SCOPE,
                idempotency_key=key,
                request_hash=req_hash,
                response_status=status.HTTP_201_CREATED,
                response_body=response.model_dump(mode="json"),
                resource_type=_BUILD_RESOURCE_TYPE,
                resource_id=build.id,
                completed_at=now,
            )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if not _is_idempotency_pk_violation(exc):
                raise
            raced = await self._idempotency.get(
                principal_key=principal_key,
                operation_scope=_BUILD_OPERATION_SCOPE,
                idempotency_key=key,
            )
            if raced is None:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="Factory Build 생성 중 충돌이 발생했습니다.",
                    status_code=status.HTTP_409_CONFLICT,
                ) from exc
            return await self._replay_or_conflict_build(raced, req_hash)

        return FactoryBuildCreateOutcome(
            result=response,
            http_status=status.HTTP_201_CREATED,
            replayed=False,
        )

    async def get_build(
        self,
        actor_user_id: uuid.UUID,
        build_id: uuid.UUID,
    ) -> FactoryBuildResponse:
        await self._assert_read(actor_user_id)
        build = await self._repo.get_build(build_id)
        if build is None:
            raise AppError(
                code="NOT_FOUND",
                message="Factory Build를 찾을 수 없습니다.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return self._build_to_response(build)

    async def list_builds_for_job(
        self,
        actor_user_id: uuid.UUID,
        job_id: uuid.UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        sort: str = "-created_at",
    ) -> FactoryBuildListResponse:
        await self._assert_read(actor_user_id)
        job = await self._repo.get_job(job_id)
        if job is None:
            raise AppError(
                code="NOT_FOUND",
                message="Factory Job을 찾을 수 없습니다.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        rows, total = await self._repo.list_builds_for_job(
            job.id,
            page=page,
            page_size=page_size,
            sort=sort,
        )
        return FactoryBuildListResponse(
            items=[self._build_to_response(row) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
            has_next=(page * page_size) < total,
            job_id=job.id,
        )
