"""Tool Factory durable OpenAPI analysis Job service (async Object Storage handoff)."""

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
from app.core.config import get_settings
from app.core.errors import AppError
from app.domain.enums import (
    JobStatus,
    MCPToolStatus,
    ToolVersionValidationStatus,
    UserStatus,
)
from app.factory import (
    ARTIFACT_CONTENT_TYPE_JSON,
    ARTIFACT_TYPE_OPENAPI_ANALYSIS,
    FACTORY_BUILD_GENERATOR_VERSION,
    FACTORY_CONTRACT_POLICY_VERSION,
    FACTORY_PACKAGE_GENERATOR_VERSION,
    FACTORY_PUBLISH_EVIDENCE_KIND,
    FACTORY_PUBLISH_EVIDENCE_SCHEMA_VERSION,
    FACTORY_REVIEW_EVIDENCE_KIND,
    FACTORY_REVIEW_EVIDENCE_SCHEMA_VERSION,
    FACTORY_SECURITY_POLICY_VERSION,
    JOB_TYPE_OPENAPI_ANALYZE,
    OPENAPI_ANALYZER_VERSION,
    PACKAGE_KIND,
    PACKAGE_SCHEMA_VERSION,
    PHASE_ANALYZE,
    PHASE_PUBLISH,
    PHASE_REVIEW,
    PHASE_SANDBOX_TEST,
)
from app.factory.contracts import FactoryOpenAPIAnalysis
from app.factory.errors import (
    FACTORY_PACKAGE_HASH_MISMATCH,
    FACTORY_PACKAGE_LINEAGE_MISMATCH,
    FACTORY_PACKAGE_OBJECT_STORE_FAILED,
    FACTORY_PACKAGE_SCHEMA_INVALID,
    FACTORY_PACKAGE_SNAPSHOT_INVALID,
    FACTORY_PACKAGE_TOOL_COUNT_MISMATCH,
    FACTORY_PUBLISH_ALREADY_EXISTS,
    FACTORY_PUBLISH_EVIDENCE_MISMATCH,
    FACTORY_PUBLISH_NOT_ELIGIBLE,
    FACTORY_PUBLISH_REJECTED_REVIEW,
    FACTORY_REVIEW_ALREADY_EXISTS,
    FACTORY_REVIEW_EVIDENCE_MISMATCH,
    FACTORY_REVIEW_NOT_ELIGIBLE,
    FACTORY_SOURCE_OBJECT_STORE_FAILED,
)
from app.factory.import_input_schema import build_mcp_callable_input_schema
from app.factory.openapi_analyzer import MAX_OPERATIONS, MAX_SOURCE_BYTES
from app.factory.package_generator import (
    PackageGenerationError,
    build_generated_package,
)
from app.factory.sandbox_security import (
    SandboxSecurityError,
    validate_package_integrity,
)
from app.infrastructure.object_storage import (
    ObjectStorageClient,
    ObjectStorageError,
    get_object_storage,
)
from app.models.factory import (
    ToolFactoryArtifact,
    ToolFactoryBuild,
    ToolFactoryCandidate,
    ToolFactoryGeneratedPackage,
    ToolFactoryJob,
)
from app.repositories.factory import FactoryRepository
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.outbox import OutboxRepository
from app.repositories.user import UserRepository
from app.schemas.factory import (
    FactoryArtifactSummary,
    FactoryBuildListResponse,
    FactoryBuildResponse,
    FactoryCandidateListResponse,
    FactoryCandidateMaterializeResponse,
    FactoryCandidateResponse,
    FactoryGeneratedPackageListResponse,
    FactoryGeneratedPackageResponse,
    FactoryGeneratedPackageTool,
    FactoryJobDetailResponse,
    FactoryJobListResponse,
    FactoryJobResponse,
    FactoryPublishResponse,
    FactoryReviewResponse,
    FactoryTestRunListResponse,
    FactoryTestRunResponse,
)
from app.services.authorization import AuthorizationResolver
from app.services.mcp_server import MCPServerService

_MCP_SERVER_READ = "mcp.server.read"
_MCP_SERVER_MANAGE = "mcp.server.manage"

_OPERATION_SCOPE = "FACTORY_OPENAPI_ANALYZE_V1"
_RESOURCE_TYPE = "TOOL_FACTORY_JOB"
_BUILD_OPERATION_SCOPE = "FACTORY_BUILD_CREATE_V1"
_BUILD_RESOURCE_TYPE = "TOOL_FACTORY_BUILD"
_PACKAGE_OPERATION_SCOPE = "FACTORY_PACKAGE_GENERATE_V1"
_PACKAGE_RESOURCE_TYPE = "TOOL_FACTORY_GENERATED_PACKAGE"
_PACKAGE_UNIQUE_NAME = "uq_tool_factory_generated_packages_build_generator"
_TEST_RUN_OPERATION_SCOPE = "FACTORY_TEST_RUN_CREATE_V1"
_TEST_RUN_RESOURCE_TYPE = "TOOL_FACTORY_TEST_RUN"
_TEST_RUN_UNIQUE_NAME = "uq_tool_factory_test_runs_package_policies"
_REVIEW_OPERATION_SCOPE = "FACTORY_REVIEW_CREATE_V1"
_REVIEW_RESOURCE_TYPE = "TOOL_FACTORY_REVIEW"
_REVIEW_UNIQUE_NAME = "uq_tool_factory_reviews_test_run_id"
_REVIEW_DECISIONS = frozenset({"APPROVE", "REJECT"})
_PUBLISH_OPERATION_SCOPE = "FACTORY_PUBLISH_IMPORT_V1"
_PUBLISH_RESOURCE_TYPE = "TOOL_FACTORY_PUBLISH"
_PUBLISH_UNIQUE_NAME = "uq_tool_factory_publishes_review_id"
# Must match factory_test_worker sealed TestRun evidence contract.
_SANDBOX_TEST_EVIDENCE_KIND = "FACTORY_SANDBOX_TEST_EVIDENCE"
_SANDBOX_TEST_EVIDENCE_SCHEMA_VERSION = 1
_PACKAGE_PAYLOAD_SCHEMA_VERSION = 1
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


@dataclass(frozen=True, slots=True)
class FactoryPackageGenerateOutcome:
    result: FactoryGeneratedPackageResponse
    http_status: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class FactoryTestRunCreateOutcome:
    result: FactoryTestRunResponse
    http_status: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class FactoryReviewCreateOutcome:
    result: FactoryReviewResponse
    http_status: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class FactoryPublishCreateOutcome:
    result: FactoryPublishResponse
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


def build_package_object_key(*, build_id: uuid.UUID, generator_version: str) -> str:
    """Deterministic Object Storage key for a Build + package generator version."""

    return (
        f"factory/packages/{build_id.hex}/"
        f"{generator_version}/package.json"
    )


def _package_request_hash(*, build_id: uuid.UUID, generator_version: str) -> str:
    return compute_canonical_json_hash(
        {
            "build_id": str(build_id),
            "generator_version": generator_version,
        }
    )


def _is_package_unique_violation(exc: IntegrityError) -> bool:
    name = _constraint_name(exc)
    if name == _PACKAGE_UNIQUE_NAME:
        return True
    orig = getattr(exc, "orig", None)
    msg = str(orig if orig is not None else exc)
    return _PACKAGE_UNIQUE_NAME in msg


def _is_test_run_unique_violation(exc: IntegrityError) -> bool:
    name = _constraint_name(exc)
    if name == _TEST_RUN_UNIQUE_NAME:
        return True
    orig = getattr(exc, "orig", None)
    msg = str(orig if orig is not None else exc)
    return _TEST_RUN_UNIQUE_NAME in msg


def _test_run_request_hash(
    *,
    package_id: uuid.UUID,
    security_policy_version: str,
    contract_policy_version: str,
) -> str:
    return compute_canonical_json_hash(
        {
            "generated_package_id": str(package_id),
            "security_policy_version": security_policy_version,
            "contract_policy_version": contract_policy_version,
        }
    )


def _is_review_unique_violation(exc: IntegrityError) -> bool:
    name = _constraint_name(exc)
    if name == _REVIEW_UNIQUE_NAME:
        return True
    orig = getattr(exc, "orig", None)
    msg = str(orig if orig is not None else exc)
    if _REVIEW_UNIQUE_NAME in msg:
        return True
    lower = msg.lower()
    return (
        "unique constraint failed" in lower
        and "tool_factory_reviews" in lower
        and "test_run_id" in lower
    )


def _is_publish_unique_violation(exc: IntegrityError) -> bool:
    name = _constraint_name(exc)
    if name == _PUBLISH_UNIQUE_NAME:
        return True
    orig = getattr(exc, "orig", None)
    msg = str(orig if orig is not None else exc)
    if _PUBLISH_UNIQUE_NAME in msg:
        return True
    lower = msg.lower()
    return (
        "unique constraint failed" in lower
        and "tool_factory_publishes" in lower
        and "review_id" in lower
    )


def _publish_request_hash(*, review_id: uuid.UUID) -> str:
    return compute_canonical_json_hash({"review_id": str(review_id)})


def _declarative_tool_remote_name(tool: dict[str, Any]) -> str:
    """Stable MCP Tool remote_name from package operation identity."""

    key = str(tool.get("operation_key") or "").strip()
    if not key:
        raise AppError(
            code=FACTORY_PACKAGE_SCHEMA_INVALID,
            message="Package tool missing operation_key.",
            status_code=status.HTTP_409_CONFLICT,
        )
    return key[:255]


def _normalize_review_comment(comment: str | None) -> str | None:
    if comment is None:
        return None
    cleaned = _CONTROL_RE.sub("", comment).strip()
    if not cleaned:
        return None
    if len(cleaned) > 500:
        raise AppError(
            code="VALIDATION_ERROR",
            message="Review comment must be at most 500 characters.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    return cleaned


def _review_request_hash(
    *,
    test_run_id: uuid.UUID,
    decision: str,
    comment: str | None,
) -> str:
    return compute_canonical_json_hash(
        {
            "test_run_id": str(test_run_id),
            "decision": decision,
            "comment": comment,
        }
    )


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

    def _revalidate_build_hashes(self, build: ToolFactoryBuild) -> list[Any]:
        """Fail-closed integrity check immediately before package generation."""

        snapshot = build.candidates_snapshot
        if not isinstance(snapshot, list) or not snapshot:
            raise AppError(
                code=FACTORY_PACKAGE_SNAPSHOT_INVALID,
                message="Build candidates_snapshot is missing or empty.",
                status_code=status.HTTP_409_CONFLICT,
            )
        recomputed_snapshot_hash = compute_canonical_json_hash(snapshot)
        if recomputed_snapshot_hash != build.candidates_snapshot_hash:
            raise AppError(
                code=FACTORY_PACKAGE_HASH_MISMATCH,
                message=(
                    "Build candidates_snapshot_hash does not match the "
                    "persisted snapshot."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )
        raw_ids = build.candidate_ids if isinstance(build.candidate_ids, list) else []
        candidate_ids = [str(cid) for cid in raw_ids]
        if not candidate_ids:
            raise AppError(
                code=FACTORY_PACKAGE_SNAPSHOT_INVALID,
                message="Build candidate_ids lineage is missing.",
                status_code=status.HTTP_409_CONFLICT,
            )
        recomputed_input_hash = compute_canonical_json_hash(
            _build_input_hash_payload(
                job_id=build.job_id,
                analysis_artifact_id=build.analysis_artifact_id,
                source_sha256=build.source_sha256,
                analyzer_version=build.analyzer_version,
                generator_version=build.generator_version,
                candidate_ids=candidate_ids,
                candidates_snapshot_hash=build.candidates_snapshot_hash,
            )
        )
        if recomputed_input_hash != build.build_input_hash:
            raise AppError(
                code=FACTORY_PACKAGE_HASH_MISMATCH,
                message=(
                    "Build build_input_hash does not match recomputed "
                    "Build input evidence."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )
        if (
            not build.source_sha256
            or len(build.source_sha256) != 64
            or not build.analyzer_version
        ):
            raise AppError(
                code=FACTORY_PACKAGE_LINEAGE_MISMATCH,
                message="Build source lineage is missing or corrupt.",
                status_code=status.HTTP_409_CONFLICT,
            )
        return snapshot

    def _package_to_response(
        self,
        row: ToolFactoryGeneratedPackage,
        *,
        tools: list[Any] | None = None,
    ) -> FactoryGeneratedPackageResponse:
        projected: list[FactoryGeneratedPackageTool] = []
        if tools is not None:
            projected = [
                FactoryGeneratedPackageTool.model_validate(tool) for tool in tools
            ]
        return FactoryGeneratedPackageResponse(
            id=row.id,
            artifact_type="GENERATED_PACKAGE",
            build_id=row.build_id,
            job_id=row.job_id,
            analysis_artifact_id=row.analysis_artifact_id,
            source_sha256=row.source_sha256,
            analyzer_version=row.analyzer_version,
            build_input_hash=row.build_input_hash,
            build_generator_version=row.build_generator_version,
            generator_version=row.generator_version,
            package_schema_version=row.package_schema_version,
            package_content_sha256=row.package_content_sha256,
            package_size_bytes=row.package_size_bytes,
            tool_count=row.tool_count,
            tools=projected,
            requested_by=row.requested_by,
            created_at=row.created_at,
        )

    async def _load_package_tools(
        self,
        row: ToolFactoryGeneratedPackage,
    ) -> list[Any]:
        storage = self._storage_client()
        try:
            raw = await storage.get_bytes(
                bucket=row.object_bucket,
                object_key=row.object_key,
            )
        except ObjectStorageError as exc:
            raise AppError(
                code=exc.code or FACTORY_PACKAGE_OBJECT_STORE_FAILED,
                message=exc.message,
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            ) from exc
        if len(raw) != int(row.package_size_bytes):
            raise AppError(
                code=FACTORY_PACKAGE_HASH_MISMATCH,
                message=(
                    "Generated Package object size does not match "
                    "persisted package_size_bytes."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AppError(
                code=FACTORY_PACKAGE_SNAPSHOT_INVALID,
                message="Generated Package object payload is corrupt.",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc
        if compute_canonical_json_hash(payload) != row.package_content_sha256:
            raise AppError(
                code=FACTORY_PACKAGE_HASH_MISMATCH,
                message="Generated Package object hash does not match evidence.",
                status_code=status.HTTP_409_CONFLICT,
            )
        tools = payload.get("tools") if isinstance(payload, dict) else None
        if not isinstance(tools, list):
            raise AppError(
                code=FACTORY_PACKAGE_SNAPSHOT_INVALID,
                message="Generated Package tools payload is invalid.",
                status_code=status.HTTP_409_CONFLICT,
            )
        return tools

    async def _validate_package_for_test_enqueue(
        self,
        row: ToolFactoryGeneratedPackage,
    ) -> None:
        """Reload Object Storage and run side-effect-free integrity checks.

        Enqueue gate mirrors worker integrity (size/hash/kind/schema/generator/
        embedded Build/Job/Artifact/source/analyzer lineage/tool_count) before
        creating a durable TestRun + Outbox. Does not execute package content.
        """

        storage = self._storage_client()
        try:
            raw = await storage.get_bytes(
                bucket=row.object_bucket,
                object_key=row.object_key,
            )
        except ObjectStorageError as exc:
            raise AppError(
                code=exc.code or FACTORY_PACKAGE_OBJECT_STORE_FAILED,
                message=exc.message,
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            ) from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AppError(
                code=FACTORY_PACKAGE_SNAPSHOT_INVALID,
                message="Generated Package object payload is corrupt.",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc
        if not isinstance(payload, dict):
            raise AppError(
                code=FACTORY_PACKAGE_SNAPSHOT_INVALID,
                message="Generated Package object payload must be a JSON object.",
                status_code=status.HTTP_409_CONFLICT,
            )
        try:
            validate_package_integrity(
                raw=raw,
                package=payload,
                expected_size_bytes=row.package_size_bytes,
                expected_content_sha256=row.package_content_sha256,
                expected_generator_version=row.generator_version,
                expected_package_schema_version=row.package_schema_version,
                expected_build_id=str(row.build_id),
                expected_build_input_hash=row.build_input_hash,
                expected_job_id=str(row.job_id),
                expected_analysis_artifact_id=str(row.analysis_artifact_id),
                expected_source_sha256=row.source_sha256,
                expected_analyzer_version=row.analyzer_version,
                expected_tool_count=row.tool_count,
            )
        except SandboxSecurityError as exc:
            status_code = (
                status.HTTP_503_SERVICE_UNAVAILABLE
                if exc.code == FACTORY_PACKAGE_OBJECT_STORE_FAILED
                else status.HTTP_409_CONFLICT
            )
            raise AppError(
                code=exc.code,
                message=exc.message,
                status_code=status_code,
            ) from exc

    async def _cleanup_package_object_if_unowned(
        self,
        *,
        storage: ObjectStorageClient,
        bucket: str,
        object_key: str,
        build_id: uuid.UUID,
        generator_version: str,
    ) -> None:
        """Delete Object Storage bytes only when no durable package owns the key.

        Deterministic package keys are shared across attempts for the same
        Build + generator version. A race loser must not delete the winner's
        object after UNIQUE/idempotency conflict.
        """
        owner = await self._repo.get_generated_package_for_build(
            build_id,
            generator_version=generator_version,
        )
        if (
            owner is not None
            and owner.object_bucket == bucket
            and owner.object_key == object_key
        ):
            return
        await storage.delete_object(bucket=bucket, object_key=object_key)

    async def _replay_or_conflict_package(
        self,
        record: Any,
        request_hash: str,
    ) -> FactoryPackageGenerateOutcome:
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
            record.resource_type != _PACKAGE_RESOURCE_TYPE
            or record.resource_id is None
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency resource metadata가 손상되었습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        row = await self._repo.get_generated_package(record.resource_id)
        if row is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency가 가리키는 Generated Package가 없습니다.",
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
            cached = FactoryGeneratedPackageResponse.model_validate(
                record.response_body
            )
        except ValidationError as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency response snapshot이 유효하지 않습니다.",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc
        if cached.id != record.resource_id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency response snapshot이 손상되었습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        # Revalidate Object Storage existence/hash/size on every replay.
        tools = await self._load_package_tools(row)
        live = self._package_to_response(row, tools=tools)
        if live.package_content_sha256 != cached.package_content_sha256:
            raise AppError(
                code=FACTORY_PACKAGE_HASH_MISMATCH,
                message=(
                    "Idempotency snapshot package hash does not match "
                    "durable Generated Package evidence."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )
        return FactoryPackageGenerateOutcome(
            result=live,
            http_status=status.HTTP_201_CREATED,
            replayed=True,
        )

    async def generate_package(
        self,
        *,
        actor_user_id: uuid.UUID,
        build_id: uuid.UUID,
        idempotency_key: str,
    ) -> FactoryPackageGenerateOutcome:
        """Generate a durable declarative package artifact from a Build snapshot."""

        await self._assert_manage(actor_user_id)
        key = _normalize_idempotency_key(idempotency_key)
        generator_version = FACTORY_PACKAGE_GENERATOR_VERSION
        req_hash = _package_request_hash(
            build_id=build_id,
            generator_version=generator_version,
        )
        principal_key = str(actor_user_id)

        existing = await self._idempotency.get(
            principal_key=principal_key,
            operation_scope=_PACKAGE_OPERATION_SCOPE,
            idempotency_key=key,
        )
        if existing is not None:
            return await self._replay_or_conflict_package(existing, req_hash)

        # Serialize generation per Build so UNIQUE(build_id, generator_version)
        # and Idempotency-Key races cannot interleave mid-write.
        build = await self._repo.get_build_for_update(build_id)
        if build is None:
            raise AppError(
                code="NOT_FOUND",
                message="Factory Build를 찾을 수 없습니다.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        # Re-check idempotency after lock (winner may have committed).
        existing = await self._idempotency.get(
            principal_key=principal_key,
            operation_scope=_PACKAGE_OPERATION_SCOPE,
            idempotency_key=key,
        )
        if existing is not None:
            # Release Build FOR UPDATE before replaying from a fresh read.
            await self._session.rollback()
            raced = await self._idempotency.get(
                principal_key=principal_key,
                operation_scope=_PACKAGE_OPERATION_SCOPE,
                idempotency_key=key,
            )
            if raced is None:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="Generated Package 생성 중 충돌이 발생했습니다.",
                    status_code=status.HTTP_409_CONFLICT,
                )
            return await self._replay_or_conflict_package(raced, req_hash)

        # Natural identity: one package per Build + generator version.
        already = await self._repo.get_generated_package_for_build(
            build.id,
            generator_version=generator_version,
        )
        if already is not None:
            tools = await self._load_package_tools(already)
            response = self._package_to_response(already, tools=tools)
            now = _utc_now()
            try:
                await self._idempotency.create_completed(
                    principal_key=principal_key,
                    operation_scope=_PACKAGE_OPERATION_SCOPE,
                    idempotency_key=key,
                    request_hash=req_hash,
                    response_status=status.HTTP_201_CREATED,
                    response_body=response.model_dump(mode="json"),
                    resource_type=_PACKAGE_RESOURCE_TYPE,
                    resource_id=already.id,
                    completed_at=now,
                )
                await self._session.commit()
            except IntegrityError as exc:
                await self._session.rollback()
                if not _is_idempotency_pk_violation(exc):
                    raise
                raced = await self._idempotency.get(
                    principal_key=principal_key,
                    operation_scope=_PACKAGE_OPERATION_SCOPE,
                    idempotency_key=key,
                )
                if raced is None:
                    raise AppError(
                        code="RESOURCE_CONFLICT",
                        message="Generated Package 생성 중 충돌이 발생했습니다.",
                        status_code=status.HTTP_409_CONFLICT,
                    ) from exc
                return await self._replay_or_conflict_package(raced, req_hash)
            return FactoryPackageGenerateOutcome(
                result=response,
                http_status=status.HTTP_201_CREATED,
                replayed=True,
            )

        snapshot = self._revalidate_build_hashes(build)
        try:
            package, raw, content_sha256 = build_generated_package(
                build_id=str(build.id),
                build_input_hash=build.build_input_hash,
                build_generator_version=build.generator_version,
                job_id=str(build.job_id),
                analysis_artifact_id=str(build.analysis_artifact_id),
                source_sha256=build.source_sha256,
                analyzer_version=build.analyzer_version,
                candidates_snapshot=snapshot,
                generator_version=generator_version,
                package_schema_version=PACKAGE_SCHEMA_VERSION,
            )
        except PackageGenerationError as exc:
            raise AppError(
                code=exc.code,
                message=exc.message,
                status_code=status.HTTP_409_CONFLICT,
            ) from exc

        settings = get_settings()
        bucket = settings.object_storage_bucket
        # Capture scalars before persistence so rollback cannot expire them.
        locked_build_id = build.id
        locked_job_id = build.job_id
        locked_analysis_artifact_id = build.analysis_artifact_id
        locked_source_sha256 = build.source_sha256
        locked_analyzer_version = build.analyzer_version
        locked_build_generator_version = build.generator_version
        locked_build_input_hash = build.build_input_hash
        object_key = build_package_object_key(
            build_id=locked_build_id,
            generator_version=generator_version,
        )
        storage = self._storage_client()
        try:
            await storage.ensure_bucket(bucket)
            await storage.put_bytes(
                bucket=bucket,
                object_key=object_key,
                data=raw,
                content_type=ARTIFACT_CONTENT_TYPE_JSON,
            )
        except ObjectStorageError as exc:
            raise AppError(
                code=exc.code or FACTORY_PACKAGE_OBJECT_STORE_FAILED,
                message=exc.message,
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            ) from exc

        package_id = uuid.uuid4()
        now = _utc_now()
        try:
            row = await self._repo.create_generated_package(
                package_id=package_id,
                build_id=locked_build_id,
                job_id=locked_job_id,
                analysis_artifact_id=locked_analysis_artifact_id,
                source_sha256=locked_source_sha256,
                analyzer_version=locked_analyzer_version,
                build_input_hash=locked_build_input_hash,
                build_generator_version=locked_build_generator_version,
                generator_version=generator_version,
                package_schema_version=PACKAGE_SCHEMA_VERSION,
                package_content_sha256=content_sha256,
                package_size_bytes=len(raw),
                tool_count=int(package["tool_count"]),
                object_bucket=bucket,
                object_key=object_key,
                content_type=ARTIFACT_CONTENT_TYPE_JSON,
                requested_by=actor_user_id,
            )
            response = self._package_to_response(row, tools=package["tools"])
            await self._idempotency.create_completed(
                principal_key=principal_key,
                operation_scope=_PACKAGE_OPERATION_SCOPE,
                idempotency_key=key,
                request_hash=req_hash,
                response_status=status.HTTP_201_CREATED,
                response_body=response.model_dump(mode="json"),
                resource_type=_PACKAGE_RESOURCE_TYPE,
                resource_id=row.id,
                completed_at=now,
            )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            # Compensating delete only when no durable owner keeps this key.
            await self._cleanup_package_object_if_unowned(
                storage=storage,
                bucket=bucket,
                object_key=object_key,
                build_id=locked_build_id,
                generator_version=generator_version,
            )
            if _is_idempotency_pk_violation(exc) or _is_package_unique_violation(exc):
                raced = await self._idempotency.get(
                    principal_key=principal_key,
                    operation_scope=_PACKAGE_OPERATION_SCOPE,
                    idempotency_key=key,
                )
                if raced is not None:
                    return await self._replay_or_conflict_package(raced, req_hash)
                existing_pkg = await self._repo.get_generated_package_for_build(
                    locked_build_id,
                    generator_version=generator_version,
                )
                if existing_pkg is not None:
                    tools = await self._load_package_tools(existing_pkg)
                    return FactoryPackageGenerateOutcome(
                        result=self._package_to_response(
                            existing_pkg, tools=tools
                        ),
                        http_status=status.HTTP_201_CREATED,
                        replayed=True,
                    )
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="Generated Package 생성 중 충돌이 발생했습니다.",
                    status_code=status.HTTP_409_CONFLICT,
                ) from exc
            raise
        except Exception:
            await self._session.rollback()
            await self._cleanup_package_object_if_unowned(
                storage=storage,
                bucket=bucket,
                object_key=object_key,
                build_id=locked_build_id,
                generator_version=generator_version,
            )
            raise

        return FactoryPackageGenerateOutcome(
            result=response,
            http_status=status.HTTP_201_CREATED,
            replayed=False,
        )

    async def list_generated_packages_for_build(
        self,
        actor_user_id: uuid.UUID,
        build_id: uuid.UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        sort: str = "-created_at",
    ) -> FactoryGeneratedPackageListResponse:
        await self._assert_read(actor_user_id)
        build = await self._repo.get_build(build_id)
        if build is None:
            raise AppError(
                code="NOT_FOUND",
                message="Factory Build를 찾을 수 없습니다.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        rows, total = await self._repo.list_generated_packages_for_build(
            build.id,
            page=page,
            page_size=page_size,
            sort=sort,
        )
        items: list[FactoryGeneratedPackageResponse] = []
        for row in rows:
            tools = await self._load_package_tools(row)
            items.append(self._package_to_response(row, tools=tools))
        return FactoryGeneratedPackageListResponse(
            items=items,
            page=page,
            page_size=page_size,
            total=total,
            has_next=(page * page_size) < total,
            build_id=build.id,
        )

    async def get_generated_package(
        self,
        actor_user_id: uuid.UUID,
        package_id: uuid.UUID,
    ) -> FactoryGeneratedPackageResponse:
        await self._assert_read(actor_user_id)
        row = await self._repo.get_generated_package(package_id)
        if row is None:
            raise AppError(
                code="NOT_FOUND",
                message="Generated Package를 찾을 수 없습니다.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        tools = await self._load_package_tools(row)
        return self._package_to_response(row, tools=tools)

    def _test_run_to_response(self, row: Any) -> FactoryTestRunResponse:
        return FactoryTestRunResponse(
            id=row.id,
            generated_package_id=row.generated_package_id,
            build_id=row.build_id,
            job_id=row.job_id,
            analysis_artifact_id=row.analysis_artifact_id,
            package_content_sha256=row.package_content_sha256,
            generator_version=row.generator_version,
            package_schema_version=row.package_schema_version,
            source_sha256=row.source_sha256,
            analyzer_version=row.analyzer_version,
            build_input_hash=row.build_input_hash,
            security_policy_version=row.security_policy_version,
            contract_policy_version=row.contract_policy_version,
            status=JobStatus(row.status),
            result=row.result,
            security_result=row.security_result,
            contract_result=row.contract_result,
            evidence_hash=row.evidence_hash,
            error_code=row.error_code,
            error_message=row.error_message,
            requested_by=row.requested_by,
            created_at=row.created_at,
            queued_at=row.queued_at,
            started_at=row.started_at,
            finished_at=row.finished_at,
        )

    async def _replay_or_conflict_test_run(
        self,
        record: Any,
        request_hash: str,
    ) -> FactoryTestRunCreateOutcome:
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
            record.resource_type != _TEST_RUN_RESOURCE_TYPE
            or record.resource_id is None
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency resource metadata가 손상되었습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        row = await self._repo.get_test_run(record.resource_id)
        if row is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency가 가리키는 Factory TestRun이 없습니다.",
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
            cached = FactoryTestRunResponse.model_validate(record.response_body)
        except ValidationError as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency response snapshot이 유효하지 않습니다.",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc
        if cached.id != record.resource_id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency response snapshot이 손상되었습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        # Live projection (status may have advanced since enqueue).
        live = self._test_run_to_response(row)
        return FactoryTestRunCreateOutcome(
            result=live,
            http_status=status.HTTP_201_CREATED,
            replayed=True,
        )

    async def create_test_run(
        self,
        *,
        actor_user_id: uuid.UUID,
        package_id: uuid.UUID,
        idempotency_key: str,
    ) -> FactoryTestRunCreateOutcome:
        """Enqueue a durable sandbox Security/Contract TestRun for a package."""

        await self._assert_manage(actor_user_id)
        key = _normalize_idempotency_key(idempotency_key)
        security_policy_version = FACTORY_SECURITY_POLICY_VERSION
        contract_policy_version = FACTORY_CONTRACT_POLICY_VERSION
        req_hash = _test_run_request_hash(
            package_id=package_id,
            security_policy_version=security_policy_version,
            contract_policy_version=contract_policy_version,
        )
        principal_key = str(actor_user_id)

        existing = await self._idempotency.get(
            principal_key=principal_key,
            operation_scope=_TEST_RUN_OPERATION_SCOPE,
            idempotency_key=key,
        )
        if existing is not None:
            return await self._replay_or_conflict_test_run(existing, req_hash)

        package = await self._repo.get_generated_package(package_id)
        if package is None:
            raise AppError(
                code="NOT_FOUND",
                message="Generated Package를 찾을 수 없습니다.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        already = await self._repo.get_test_run_for_package_policies(
            generated_package_id=package.id,
            security_policy_version=security_policy_version,
            contract_policy_version=contract_policy_version,
        )
        if already is not None:
            response = self._test_run_to_response(already)
            now = _utc_now()
            try:
                await self._idempotency.create_completed(
                    principal_key=principal_key,
                    operation_scope=_TEST_RUN_OPERATION_SCOPE,
                    idempotency_key=key,
                    request_hash=req_hash,
                    response_status=status.HTTP_201_CREATED,
                    response_body=response.model_dump(mode="json"),
                    resource_type=_TEST_RUN_RESOURCE_TYPE,
                    resource_id=already.id,
                    completed_at=now,
                )
                await self._session.commit()
            except IntegrityError as exc:
                await self._session.rollback()
                if not _is_idempotency_pk_violation(exc):
                    raise
                raced = await self._idempotency.get(
                    principal_key=principal_key,
                    operation_scope=_TEST_RUN_OPERATION_SCOPE,
                    idempotency_key=key,
                )
                if raced is None:
                    raise AppError(
                        code="RESOURCE_CONFLICT",
                        message="Factory TestRun 생성 중 충돌이 발생했습니다.",
                        status_code=status.HTTP_409_CONFLICT,
                    ) from exc
                return await self._replay_or_conflict_test_run(raced, req_hash)
            return FactoryTestRunCreateOutcome(
                result=response,
                http_status=status.HTTP_201_CREATED,
                replayed=True,
            )

        # Pre-dispatch integrity: same side-effect-free checks as the worker.
        await self._validate_package_for_test_enqueue(package)

        test_run_id = uuid.uuid4()
        now = _utc_now()
        try:
            row = await self._repo.create_test_run(
                test_run_id=test_run_id,
                generated_package_id=package.id,
                build_id=package.build_id,
                job_id=package.job_id,
                analysis_artifact_id=package.analysis_artifact_id,
                package_content_sha256=package.package_content_sha256,
                generator_version=package.generator_version,
                package_schema_version=package.package_schema_version,
                source_sha256=package.source_sha256,
                analyzer_version=package.analyzer_version,
                build_input_hash=package.build_input_hash,
                security_policy_version=security_policy_version,
                contract_policy_version=contract_policy_version,
                status=JobStatus.QUEUED.value,
                requested_by=actor_user_id,
                queued_at=now,
            )
            await self._outbox.create_factory_test_run_dispatch(
                test_run_id=row.id,
                created_at=now,
            )
            response = self._test_run_to_response(row)
            await self._idempotency.create_completed(
                principal_key=principal_key,
                operation_scope=_TEST_RUN_OPERATION_SCOPE,
                idempotency_key=key,
                request_hash=req_hash,
                response_status=status.HTTP_201_CREATED,
                response_body=response.model_dump(mode="json"),
                resource_type=_TEST_RUN_RESOURCE_TYPE,
                resource_id=row.id,
                completed_at=now,
            )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if _is_idempotency_pk_violation(exc) or _is_test_run_unique_violation(exc):
                raced = await self._idempotency.get(
                    principal_key=principal_key,
                    operation_scope=_TEST_RUN_OPERATION_SCOPE,
                    idempotency_key=key,
                )
                if raced is not None:
                    return await self._replay_or_conflict_test_run(raced, req_hash)
                existing_run = await self._repo.get_test_run_for_package_policies(
                    generated_package_id=package.id,
                    security_policy_version=security_policy_version,
                    contract_policy_version=contract_policy_version,
                )
                if existing_run is not None:
                    return FactoryTestRunCreateOutcome(
                        result=self._test_run_to_response(existing_run),
                        http_status=status.HTTP_201_CREATED,
                        replayed=True,
                    )
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="Factory TestRun 생성 중 충돌이 발생했습니다.",
                    status_code=status.HTTP_409_CONFLICT,
                ) from exc
            raise

        return FactoryTestRunCreateOutcome(
            result=response,
            http_status=status.HTTP_201_CREATED,
            replayed=False,
        )

    async def get_test_run(
        self,
        actor_user_id: uuid.UUID,
        test_run_id: uuid.UUID,
    ) -> FactoryTestRunResponse:
        await self._assert_read(actor_user_id)
        row = await self._repo.get_test_run(test_run_id)
        if row is None:
            raise AppError(
                code="NOT_FOUND",
                message="Factory TestRun을 찾을 수 없습니다.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return self._test_run_to_response(row)

    async def list_test_runs_for_package(
        self,
        actor_user_id: uuid.UUID,
        package_id: uuid.UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        sort: str = "-created_at",
    ) -> FactoryTestRunListResponse:
        await self._assert_read(actor_user_id)
        package = await self._repo.get_generated_package(package_id)
        if package is None:
            raise AppError(
                code="NOT_FOUND",
                message="Generated Package를 찾을 수 없습니다.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        rows, total = await self._repo.list_test_runs_for_package(
            package.id,
            page=page,
            page_size=page_size,
            sort=sort,
        )
        return FactoryTestRunListResponse(
            items=[self._test_run_to_response(row) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
            has_next=(page * page_size) < total,
            generated_package_id=package.id,
        )


    def _review_to_response(self, row: Any) -> FactoryReviewResponse:
        return FactoryReviewResponse(
            id=row.id,
            test_run_id=row.test_run_id,
            generated_package_id=row.generated_package_id,
            build_id=row.build_id,
            job_id=row.job_id,
            analysis_artifact_id=row.analysis_artifact_id,
            package_content_sha256=row.package_content_sha256,
            generator_version=row.generator_version,
            package_schema_version=row.package_schema_version,
            source_sha256=row.source_sha256,
            analyzer_version=row.analyzer_version,
            build_input_hash=row.build_input_hash,
            security_policy_version=row.security_policy_version,
            contract_policy_version=row.contract_policy_version,
            test_evidence_hash=row.test_evidence_hash,
            decision=row.decision,
            comment=row.comment,
            review_evidence_hash=row.review_evidence_hash,
            reviewed_by=row.reviewed_by,
            reviewed_at=row.reviewed_at,
            created_at=row.created_at,
        )

    async def _replay_or_conflict_review(
        self,
        record: Any,
        request_hash: str,
    ) -> FactoryReviewCreateOutcome:
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
            record.resource_type != _REVIEW_RESOURCE_TYPE
            or record.resource_id is None
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency resource metadata가 손상되었습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        row = await self._repo.get_review(record.resource_id)
        if row is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency가 가리키는 Factory Review가 없습니다.",
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
            cached = FactoryReviewResponse.model_validate(record.response_body)
        except ValidationError as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency response snapshot이 유효하지 않습니다.",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc
        if cached.id != record.resource_id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency response snapshot이 손상되었습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        return FactoryReviewCreateOutcome(
            result=self._review_to_response(row),
            http_status=status.HTTP_201_CREATED,
            replayed=True,
        )

    async def _assert_package_bytes_match(
        self,
        package: ToolFactoryGeneratedPackage,
        *,
        expected_content_sha256: str,
    ) -> dict[str, Any]:
        """Fail-closed Object Storage size/hash revalidation; returns payload."""

        storage = self._storage_client()
        try:
            raw = await storage.get_bytes(
                bucket=package.object_bucket,
                object_key=package.object_key,
            )
        except ObjectStorageError as exc:
            raise AppError(
                code=exc.code or FACTORY_PACKAGE_OBJECT_STORE_FAILED,
                message=exc.message,
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            ) from exc
        if len(raw) != int(package.package_size_bytes):
            raise AppError(
                code=FACTORY_PACKAGE_HASH_MISMATCH,
                message=(
                    "Generated Package object size does not match "
                    "persisted package_size_bytes."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AppError(
                code=FACTORY_PACKAGE_SNAPSHOT_INVALID,
                message="Generated Package object payload is corrupt.",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc
        if not isinstance(payload, dict):
            raise AppError(
                code=FACTORY_PACKAGE_SNAPSHOT_INVALID,
                message="Generated Package object payload must be an object.",
                status_code=status.HTTP_409_CONFLICT,
            )
        actual = compute_canonical_json_hash(payload)
        if actual != package.package_content_sha256:
            raise AppError(
                code=FACTORY_PACKAGE_HASH_MISMATCH,
                message="Generated Package object hash does not match evidence.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if actual != expected_content_sha256:
            raise AppError(
                code=FACTORY_PACKAGE_LINEAGE_MISMATCH,
                message="Generated Package hash does not match TestRun snapshot.",
                status_code=status.HTTP_409_CONFLICT,
            )
        return payload

    def _assert_package_payload_contract(
        self,
        payload: dict[str, Any],
        *,
        package: ToolFactoryGeneratedPackage,
        expected_generator_version: str,
        expected_package_schema_version: str,
    ) -> list[dict[str, Any]]:
        """Fail-closed schema/kind/generator/tool_count + declarative tools."""

        if payload.get("kind") != PACKAGE_KIND:
            raise AppError(
                code=FACTORY_PACKAGE_SCHEMA_INVALID,
                message="Generated Package kind가 올바르지 않습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if payload.get("schema_version") != _PACKAGE_PAYLOAD_SCHEMA_VERSION:
            raise AppError(
                code=FACTORY_PACKAGE_SCHEMA_INVALID,
                message="Generated Package schema_version이 올바르지 않습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if payload.get("generator_version") != expected_generator_version:
            raise AppError(
                code=FACTORY_PACKAGE_SCHEMA_INVALID,
                message="Generated Package generator_version이 projection과 불일치합니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if payload.get("package_schema_version") != expected_package_schema_version:
            raise AppError(
                code=FACTORY_PACKAGE_SCHEMA_INVALID,
                message="Generated Package package_schema_version이 projection과 불일치합니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        tools = payload.get("tools")
        if not isinstance(tools, list) or not tools:
            raise AppError(
                code=FACTORY_PACKAGE_SCHEMA_INVALID,
                message="Generated Package tools가 비어 있거나 유효하지 않습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        tool_count = payload.get("tool_count")
        if tool_count != len(tools) or tool_count != int(package.tool_count):
            raise AppError(
                code=FACTORY_PACKAGE_TOOL_COUNT_MISMATCH,
                message="Generated Package tool_count가 tools/DB와 불일치합니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        normalized: list[dict[str, Any]] = []
        for entry in tools:
            if not isinstance(entry, dict):
                raise AppError(
                    code=FACTORY_PACKAGE_SCHEMA_INVALID,
                    message="Generated Package tool entry must be an object.",
                    status_code=status.HTTP_409_CONFLICT,
                )
            for field in (
                "candidate_id",
                "operation_key",
                "method",
                "path",
            ):
                if not entry.get(field):
                    raise AppError(
                        code=FACTORY_PACKAGE_SCHEMA_INVALID,
                        message=f"Generated Package tool missing '{field}'.",
                        status_code=status.HTTP_409_CONFLICT,
                    )
            # Reject executable/secret-bearing fields if present.
            for forbidden in (
                "command",
                "commands",
                "shell",
                "subprocess",
                "dependencies",
                "install",
                "credentials",
                "secrets",
                "auth_headers",
                "source",
                "executable",
                "object_key",
                "bucket",
            ):
                if forbidden in entry:
                    raise AppError(
                        code=FACTORY_PACKAGE_SCHEMA_INVALID,
                        message=(
                            f"Generated Package tool must not include '{forbidden}'."
                        ),
                        status_code=status.HTTP_409_CONFLICT,
                    )
            normalized.append(entry)
        return normalized

    def _assert_sealed_test_run_evidence(self, test_run: Any) -> None:
        """Fail-closed: sealed evidence must match TestRun projection columns."""

        evidence = test_run.evidence
        if not isinstance(evidence, dict):
            raise AppError(
                code=FACTORY_REVIEW_EVIDENCE_MISMATCH,
                message="TestRun evidence가 손상되었습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if evidence.get("kind") != _SANDBOX_TEST_EVIDENCE_KIND:
            raise AppError(
                code=FACTORY_REVIEW_EVIDENCE_MISMATCH,
                message="TestRun evidence kind가 올바르지 않습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if evidence.get("schema_version") != _SANDBOX_TEST_EVIDENCE_SCHEMA_VERSION:
            raise AppError(
                code=FACTORY_REVIEW_EVIDENCE_MISMATCH,
                message="TestRun evidence schema_version이 올바르지 않습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if evidence.get("phase") != PHASE_SANDBOX_TEST:
            raise AppError(
                code=FACTORY_REVIEW_EVIDENCE_MISMATCH,
                message="TestRun evidence phase가 올바르지 않습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if evidence.get("result") != "PASS":
            raise AppError(
                code=FACTORY_REVIEW_EVIDENCE_MISMATCH,
                message="Sealed TestRun evidence.result가 PASS가 아닙니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if evidence.get("test_run_id") != str(test_run.id):
            raise AppError(
                code=FACTORY_REVIEW_EVIDENCE_MISMATCH,
                message="Sealed evidence test_run_id가 projection과 불일치합니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if (
            evidence.get("generated_package_id") != str(test_run.generated_package_id)
            or evidence.get("build_id") != str(test_run.build_id)
            or evidence.get("job_id") != str(test_run.job_id)
            or evidence.get("analysis_artifact_id")
            != str(test_run.analysis_artifact_id)
            or evidence.get("package_content_sha256") != test_run.package_content_sha256
            or evidence.get("generator_version") != test_run.generator_version
            or evidence.get("security_policy_version")
            != test_run.security_policy_version
            or evidence.get("contract_policy_version")
            != test_run.contract_policy_version
        ):
            raise AppError(
                code=FACTORY_REVIEW_EVIDENCE_MISMATCH,
                message="Sealed evidence lineage/policy가 projection과 불일치합니다.",
                status_code=status.HTTP_409_CONFLICT,
            )

        sealed_security = evidence.get("security")
        sealed_contract = evidence.get("contract")
        security = test_run.security_result
        contract = test_run.contract_result
        if not isinstance(sealed_security, dict) or not isinstance(security, dict):
            raise AppError(
                code=FACTORY_REVIEW_EVIDENCE_MISMATCH,
                message="security evidence/projection이 유효하지 않습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if not isinstance(sealed_contract, dict) or not isinstance(contract, dict):
            raise AppError(
                code=FACTORY_REVIEW_EVIDENCE_MISMATCH,
                message="contract evidence/projection이 유효하지 않습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if compute_canonical_json_hash(sealed_security) != compute_canonical_json_hash(
            security
        ):
            raise AppError(
                code=FACTORY_REVIEW_EVIDENCE_MISMATCH,
                message="sealed security evidence가 security_result projection과 불일치합니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if compute_canonical_json_hash(sealed_contract) != compute_canonical_json_hash(
            contract
        ):
            raise AppError(
                code=FACTORY_REVIEW_EVIDENCE_MISMATCH,
                message="sealed contract evidence가 contract_result projection과 불일치합니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if sealed_security.get("passed") is not True or security.get("passed") is not True:
            raise AppError(
                code=FACTORY_REVIEW_EVIDENCE_MISMATCH,
                message="security_result.passed가 true가 아닙니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if sealed_contract.get("passed") is not True or contract.get("passed") is not True:
            raise AppError(
                code=FACTORY_REVIEW_EVIDENCE_MISMATCH,
                message="contract_result.passed가 true가 아닙니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if (
            sealed_security.get("policy_version") != test_run.security_policy_version
            or security.get("policy_version") != test_run.security_policy_version
        ):
            raise AppError(
                code=FACTORY_REVIEW_EVIDENCE_MISMATCH,
                message="security policy_version이 projection과 불일치합니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if (
            sealed_contract.get("policy_version") != test_run.contract_policy_version
            or contract.get("policy_version") != test_run.contract_policy_version
        ):
            raise AppError(
                code=FACTORY_REVIEW_EVIDENCE_MISMATCH,
                message="contract policy_version이 projection과 불일치합니다.",
                status_code=status.HTTP_409_CONFLICT,
            )

    async def create_review(
        self,
        *,
        actor_user_id: uuid.UUID,
        test_run_id: uuid.UUID,
        decision: str,
        comment: str | None,
        idempotency_key: str,
    ) -> FactoryReviewCreateOutcome:
        """Create one immutable APPROVE/REJECT Review for a PASS TestRun."""

        await self._assert_manage(actor_user_id)
        if decision not in _REVIEW_DECISIONS:
            raise AppError(
                code="VALIDATION_ERROR",
                message="Review decision must be APPROVE or REJECT.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        key = _normalize_idempotency_key(idempotency_key)
        safe_comment = _normalize_review_comment(comment)
        req_hash = _review_request_hash(
            test_run_id=test_run_id,
            decision=decision,
            comment=safe_comment,
        )
        principal_key = str(actor_user_id)

        existing = await self._idempotency.get(
            principal_key=principal_key,
            operation_scope=_REVIEW_OPERATION_SCOPE,
            idempotency_key=key,
        )
        if existing is not None:
            return await self._replay_or_conflict_review(existing, req_hash)

        test_run = await self._repo.get_test_run_for_update(test_run_id)
        if test_run is None:
            raise AppError(
                code="NOT_FOUND",
                message="Factory TestRun을 찾을 수 없습니다.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        already = await self._repo.get_review_for_test_run(test_run.id)
        if already is not None:
            raise AppError(
                code=FACTORY_REVIEW_ALREADY_EXISTS,
                message="이 TestRun에 대한 Review가 이미 존재합니다.",
                status_code=status.HTTP_409_CONFLICT,
            )

        if test_run.status != JobStatus.SUCCEEDED.value or test_run.result != "PASS":
            raise AppError(
                code=FACTORY_REVIEW_NOT_ELIGIBLE,
                message="Review는 SUCCEEDED/PASS TestRun에서만 허용됩니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if not test_run.evidence_hash or len(test_run.evidence_hash) != 64:
            raise AppError(
                code=FACTORY_REVIEW_NOT_ELIGIBLE,
                message="TestRun evidence_hash가 없어 Review할 수 없습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if not isinstance(test_run.evidence, dict):
            raise AppError(
                code=FACTORY_REVIEW_EVIDENCE_MISMATCH,
                message="TestRun evidence가 손상되었습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        recomputed = compute_canonical_json_hash(test_run.evidence)
        if recomputed != test_run.evidence_hash:
            raise AppError(
                code=FACTORY_REVIEW_EVIDENCE_MISMATCH,
                message="TestRun evidence_hash가 evidence와 일치하지 않습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        self._assert_sealed_test_run_evidence(test_run)

        package = await self._repo.get_generated_package(test_run.generated_package_id)
        if package is None:
            raise AppError(
                code="NOT_FOUND",
                message="Generated Package를 찾을 수 없습니다.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if (
            package.build_id != test_run.build_id
            or package.job_id != test_run.job_id
            or package.analysis_artifact_id != test_run.analysis_artifact_id
            or package.package_content_sha256 != test_run.package_content_sha256
            or package.generator_version != test_run.generator_version
            or package.package_schema_version != test_run.package_schema_version
            or package.source_sha256 != test_run.source_sha256
            or package.analyzer_version != test_run.analyzer_version
            or package.build_input_hash != test_run.build_input_hash
        ):
            raise AppError(
                code=FACTORY_PACKAGE_LINEAGE_MISMATCH,
                message="TestRun lineage가 Generated Package evidence와 일치하지 않습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )

        await self._assert_package_bytes_match(
            package,
            expected_content_sha256=test_run.package_content_sha256,
        )

        review_id = uuid.uuid4()
        now = _utc_now()
        evidence = {
            "schema_version": FACTORY_REVIEW_EVIDENCE_SCHEMA_VERSION,
            "kind": FACTORY_REVIEW_EVIDENCE_KIND,
            "phase": PHASE_REVIEW,
            "review_id": str(review_id),
            "test_run_id": str(test_run.id),
            "generated_package_id": str(test_run.generated_package_id),
            "build_id": str(test_run.build_id),
            "job_id": str(test_run.job_id),
            "analysis_artifact_id": str(test_run.analysis_artifact_id),
            "package_content_sha256": test_run.package_content_sha256,
            "generator_version": test_run.generator_version,
            "package_schema_version": test_run.package_schema_version,
            "security_policy_version": test_run.security_policy_version,
            "contract_policy_version": test_run.contract_policy_version,
            "test_evidence_hash": test_run.evidence_hash,
            "decision": decision,
            "comment": safe_comment,
            "reviewed_by": str(actor_user_id),
            "reviewed_at": now.isoformat(),
        }
        evidence_hash = compute_canonical_json_hash(evidence)

        try:
            row = await self._repo.create_review(
                review_id=review_id,
                test_run_id=test_run.id,
                generated_package_id=test_run.generated_package_id,
                build_id=test_run.build_id,
                job_id=test_run.job_id,
                analysis_artifact_id=test_run.analysis_artifact_id,
                package_content_sha256=test_run.package_content_sha256,
                generator_version=test_run.generator_version,
                package_schema_version=test_run.package_schema_version,
                source_sha256=test_run.source_sha256,
                analyzer_version=test_run.analyzer_version,
                build_input_hash=test_run.build_input_hash,
                security_policy_version=test_run.security_policy_version,
                contract_policy_version=test_run.contract_policy_version,
                test_evidence_hash=test_run.evidence_hash,
                decision=decision,
                comment=safe_comment,
                review_evidence=evidence,
                review_evidence_hash=evidence_hash,
                reviewed_by=actor_user_id,
                reviewed_at=now,
            )
            response = self._review_to_response(row)
            await self._idempotency.create_completed(
                principal_key=principal_key,
                operation_scope=_REVIEW_OPERATION_SCOPE,
                idempotency_key=key,
                request_hash=req_hash,
                response_status=status.HTTP_201_CREATED,
                response_body=response.model_dump(mode="json"),
                resource_type=_REVIEW_RESOURCE_TYPE,
                resource_id=row.id,
                completed_at=now,
            )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if _is_idempotency_pk_violation(exc) or _is_review_unique_violation(exc):
                raced = await self._idempotency.get(
                    principal_key=principal_key,
                    operation_scope=_REVIEW_OPERATION_SCOPE,
                    idempotency_key=key,
                )
                if raced is not None:
                    return await self._replay_or_conflict_review(raced, req_hash)
                existing_review = await self._repo.get_review_for_test_run(test_run_id)
                if existing_review is not None:
                    raise AppError(
                        code=FACTORY_REVIEW_ALREADY_EXISTS,
                        message="이 TestRun에 대한 Review가 이미 존재합니다.",
                        status_code=status.HTTP_409_CONFLICT,
                    ) from exc
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="Factory Review 생성 중 충돌이 발생했습니다.",
                    status_code=status.HTTP_409_CONFLICT,
                ) from exc
            raise

        return FactoryReviewCreateOutcome(
            result=response,
            http_status=status.HTTP_201_CREATED,
            replayed=False,
        )

    async def get_review(
        self,
        actor_user_id: uuid.UUID,
        review_id: uuid.UUID,
    ) -> FactoryReviewResponse:
        await self._assert_read(actor_user_id)
        row = await self._repo.get_review(review_id)
        if row is None:
            raise AppError(
                code="NOT_FOUND",
                message="Factory Review를 찾을 수 없습니다.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return self._review_to_response(row)

    async def get_review_for_test_run(
        self,
        actor_user_id: uuid.UUID,
        test_run_id: uuid.UUID,
    ) -> FactoryReviewResponse:
        await self._assert_read(actor_user_id)
        test_run = await self._repo.get_test_run(test_run_id)
        if test_run is None:
            raise AppError(
                code="NOT_FOUND",
                message="Factory TestRun을 찾을 수 없습니다.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        row = await self._repo.get_review_for_test_run(test_run.id)
        if row is None:
            raise AppError(
                code="NOT_FOUND",
                message="이 TestRun에 대한 Factory Review가 없습니다.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return self._review_to_response(row)

    def _publish_to_response(self, row: Any) -> FactoryPublishResponse:
        tool_ids = [
            uuid.UUID(str(item)) for item in (row.mcp_tool_ids or [])
        ]
        return FactoryPublishResponse(
            id=row.id,
            review_id=row.review_id,
            test_run_id=row.test_run_id,
            generated_package_id=row.generated_package_id,
            build_id=row.build_id,
            job_id=row.job_id,
            analysis_artifact_id=row.analysis_artifact_id,
            package_content_sha256=row.package_content_sha256,
            generator_version=row.generator_version,
            package_schema_version=row.package_schema_version,
            test_evidence_hash=row.test_evidence_hash,
            review_evidence_hash=row.review_evidence_hash,
            mcp_server_id=row.mcp_server_id,
            mcp_tool_ids=tool_ids,
            tool_count=row.tool_count,
            publish_evidence_hash=row.publish_evidence_hash,
            requested_by=row.requested_by,
            created_at=row.created_at,
        )

    async def _replay_or_conflict_publish(
        self,
        record: Any,
        request_hash: str,
    ) -> FactoryPublishCreateOutcome:
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
            record.resource_type != _PUBLISH_RESOURCE_TYPE
            or record.resource_id is None
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency resource metadata가 손상되었습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        row = await self._repo.get_publish(record.resource_id)
        if row is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency가 가리키는 Factory Publish가 없습니다.",
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
            cached = FactoryPublishResponse.model_validate(record.response_body)
        except ValidationError as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency response snapshot이 유효하지 않습니다.",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc
        if cached.id != record.resource_id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency response snapshot이 손상되었습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        return FactoryPublishCreateOutcome(
            result=self._publish_to_response(row),
            http_status=status.HTTP_201_CREATED,
            replayed=True,
        )

    def _assert_sealed_review_evidence(self, review: Any) -> None:
        """Fail-closed: Review evidence hash + sealed fields vs projection."""

        if not isinstance(review.review_evidence, dict):
            raise AppError(
                code=FACTORY_PUBLISH_EVIDENCE_MISMATCH,
                message="Review evidence가 손상되었습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if not review.review_evidence_hash or len(review.review_evidence_hash) != 64:
            raise AppError(
                code=FACTORY_PUBLISH_NOT_ELIGIBLE,
                message="Review evidence_hash가 없어 Publish할 수 없습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        recomputed = compute_canonical_json_hash(review.review_evidence)
        if recomputed != review.review_evidence_hash:
            raise AppError(
                code=FACTORY_PUBLISH_EVIDENCE_MISMATCH,
                message="Review evidence_hash가 evidence와 일치하지 않습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        evidence = review.review_evidence
        if evidence.get("kind") != FACTORY_REVIEW_EVIDENCE_KIND:
            raise AppError(
                code=FACTORY_PUBLISH_EVIDENCE_MISMATCH,
                message="Review evidence kind가 올바르지 않습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if evidence.get("schema_version") != FACTORY_REVIEW_EVIDENCE_SCHEMA_VERSION:
            raise AppError(
                code=FACTORY_PUBLISH_EVIDENCE_MISMATCH,
                message="Review evidence schema_version이 올바르지 않습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if evidence.get("phase") != PHASE_REVIEW:
            raise AppError(
                code=FACTORY_PUBLISH_EVIDENCE_MISMATCH,
                message="Review evidence phase가 올바르지 않습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if evidence.get("decision") != "APPROVE":
            raise AppError(
                code=FACTORY_PUBLISH_EVIDENCE_MISMATCH,
                message="Sealed Review evidence.decision이 APPROVE가 아닙니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if evidence.get("review_id") != str(review.id):
            raise AppError(
                code=FACTORY_PUBLISH_EVIDENCE_MISMATCH,
                message="Sealed Review evidence review_id가 projection과 불일치합니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if (
            evidence.get("test_run_id") != str(review.test_run_id)
            or evidence.get("generated_package_id") != str(review.generated_package_id)
            or evidence.get("build_id") != str(review.build_id)
            or evidence.get("job_id") != str(review.job_id)
            or evidence.get("analysis_artifact_id") != str(review.analysis_artifact_id)
            or evidence.get("package_content_sha256") != review.package_content_sha256
            or evidence.get("generator_version") != review.generator_version
            or evidence.get("package_schema_version") != review.package_schema_version
            or evidence.get("test_evidence_hash") != review.test_evidence_hash
        ):
            raise AppError(
                code=FACTORY_PUBLISH_EVIDENCE_MISMATCH,
                message="Sealed Review evidence lineage가 projection과 불일치합니다.",
                status_code=status.HTTP_409_CONFLICT,
            )

    async def _import_draft_mcp_assets(
        self,
        *,
        actor_user_id: uuid.UUID,
        review: Any,
        package_tools: list[dict[str, Any]],
        publish_id: uuid.UUID,
    ) -> tuple[uuid.UUID, list[uuid.UUID]]:
        """Create DRAFT MCP Server + INACTIVE Tools from declarative contracts.

        Uses MCPServerService canonical deferred-DRAFT path (no repository
        invariant bypass, no fabricated endpoints). Connection Test / Discovery /
        Activate remain blocked until an endpoint is set later.
        """

        tools_repo = MCPToolRepository(self._session)
        server_code = f"fac-pub-{review.id.hex}"
        server_name = f"Factory Import {review.id.hex[:8]}"
        transport_config = {
            "factory_publish": True,
            "connection_deferred": True,
            "source": "TOOL_FACTORY_PUBLISH",
            "publish_id": str(publish_id),
            "review_id": str(review.id),
            "generated_package_id": str(review.generated_package_id),
            "package_content_sha256": review.package_content_sha256,
            "note": (
                "DRAFT import metadata only. endpoint_url is intentionally unset; "
                "Connection Test / Discovery / Activate are out of this slice."
            ),
        }
        server = await MCPServerService(
            self._session
        ).create_factory_deferred_draft_uncommitted(
            name=server_name,
            description=(
                "Tool Factory Publish/Import DRAFT MCP Server "
                f"(review={review.id}). Connection deferred."
            ),
            transport_config=transport_config,
            created_by=actor_user_id,
            code=server_code,
        )

        tool_ids: list[uuid.UUID] = []
        for tool in package_tools:
            remote_name = _declarative_tool_remote_name(tool)
            display = tool.get("summary") or tool.get("operation_id") or remote_name
            description = tool.get("description")
            tags = tool.get("tags") if isinstance(tool.get("tags"), list) else []
            parameters = (
                tool.get("parameters") if isinstance(tool.get("parameters"), list) else []
            )
            raw_descriptor = {
                "candidate_id": tool.get("candidate_id"),
                "operation_key": tool.get("operation_key"),
                "operation_id": tool.get("operation_id"),
                "method": tool.get("method"),
                "path": tool.get("path"),
                "summary": tool.get("summary"),
                "description": tool.get("description"),
                "tags": tags,
                "deprecated": bool(tool.get("deprecated", False)),
                "parameters": parameters,
                "request_schema": tool.get("request_schema"),
                "response_schema": tool.get("response_schema"),
            }
            input_schema = build_mcp_callable_input_schema(
                parameters=parameters,
                request_schema=tool.get("request_schema"),
            )
            content_hash = compute_canonical_json_hash(raw_descriptor)
            mcp_tool = await tools_repo.create_tool(
                mcp_server_id=server.id,
                remote_name=remote_name,
                display_name=str(display)[:255] if display else remote_name,
                description_override=(
                    str(description) if description is not None else None
                ),
                tags=[str(t) for t in tags],
                status=str(MCPToolStatus.INACTIVE),
                created_by=actor_user_id,
            )
            version = await tools_repo.create_version(
                mcp_tool_id=mcp_tool.id,
                version_no=1,
                content_hash=content_hash,
                validation_status=str(ToolVersionValidationStatus.WARNING),
                remote_description=(
                    str(description) if description is not None else None
                ),
                input_schema=input_schema,
                output_schema=tool.get("response_schema"),
                annotations={
                    "factory_import": True,
                    "method": tool.get("method"),
                    "path": tool.get("path"),
                    "candidate_id": tool.get("candidate_id"),
                    "operation_key": tool.get("operation_key"),
                    "deprecated": bool(tool.get("deprecated", False)),
                },
                raw_descriptor=raw_descriptor,
                schema_dialect="factory-declarative-v1",
                validation_errors=[
                    {
                        "code": "FACTORY_IMPORT_UNVALIDATED",
                        "message": (
                            "Imported from Factory package; MCP schema "
                            "validation / activation is a later step."
                        ),
                    }
                ],
                created_by=actor_user_id,
            )
            mcp_tool.current_version_id = version.id
            await self._session.flush()
            tool_ids.append(mcp_tool.id)

        return server.id, tool_ids

    async def publish_review(
        self,
        *,
        actor_user_id: uuid.UUID,
        review_id: uuid.UUID,
        idempotency_key: str,
    ) -> FactoryPublishCreateOutcome:
        """Publish/Import an APPROVE Review into DRAFT MCP assets (exactly once)."""

        await self._assert_manage(actor_user_id)
        key = _normalize_idempotency_key(idempotency_key)
        req_hash = _publish_request_hash(review_id=review_id)
        principal_key = str(actor_user_id)

        existing = await self._idempotency.get(
            principal_key=principal_key,
            operation_scope=_PUBLISH_OPERATION_SCOPE,
            idempotency_key=key,
        )
        if existing is not None:
            return await self._replay_or_conflict_publish(existing, req_hash)

        review = await self._repo.get_review_for_update(review_id)
        if review is None:
            raise AppError(
                code="NOT_FOUND",
                message="Factory Review를 찾을 수 없습니다.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        already = await self._repo.get_publish_for_review(review.id)
        if already is not None:
            raise AppError(
                code=FACTORY_PUBLISH_ALREADY_EXISTS,
                message="이 Review에 대한 Publish가 이미 존재합니다.",
                status_code=status.HTTP_409_CONFLICT,
            )

        if review.decision == "REJECT":
            raise AppError(
                code=FACTORY_PUBLISH_REJECTED_REVIEW,
                message="REJECT Review는 Publish/Import할 수 없습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if review.decision != "APPROVE":
            raise AppError(
                code=FACTORY_PUBLISH_NOT_ELIGIBLE,
                message="Publish는 APPROVE Review에서만 허용됩니다.",
                status_code=status.HTTP_409_CONFLICT,
            )

        self._assert_sealed_review_evidence(review)

        test_run = await self._repo.get_test_run(review.test_run_id)
        if test_run is None:
            raise AppError(
                code="NOT_FOUND",
                message="Factory TestRun을 찾을 수 없습니다.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if test_run.status != JobStatus.SUCCEEDED.value or test_run.result != "PASS":
            raise AppError(
                code=FACTORY_PUBLISH_NOT_ELIGIBLE,
                message="Publish는 SUCCEEDED/PASS TestRun에서만 허용됩니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if not test_run.evidence_hash or len(test_run.evidence_hash) != 64:
            raise AppError(
                code=FACTORY_PUBLISH_NOT_ELIGIBLE,
                message="TestRun evidence_hash가 없어 Publish할 수 없습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if not isinstance(test_run.evidence, dict):
            raise AppError(
                code=FACTORY_PUBLISH_EVIDENCE_MISMATCH,
                message="TestRun evidence가 손상되었습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if compute_canonical_json_hash(test_run.evidence) != test_run.evidence_hash:
            raise AppError(
                code=FACTORY_PUBLISH_EVIDENCE_MISMATCH,
                message="TestRun evidence_hash가 evidence와 일치하지 않습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )
        try:
            self._assert_sealed_test_run_evidence(test_run)
        except AppError as exc:
            if exc.code == FACTORY_REVIEW_EVIDENCE_MISMATCH:
                raise AppError(
                    code=FACTORY_PUBLISH_EVIDENCE_MISMATCH,
                    message=exc.message,
                    status_code=status.HTTP_409_CONFLICT,
                ) from exc
            raise

        if (
            test_run.id != review.test_run_id
            or test_run.generated_package_id != review.generated_package_id
            or test_run.build_id != review.build_id
            or test_run.job_id != review.job_id
            or test_run.analysis_artifact_id != review.analysis_artifact_id
            or test_run.package_content_sha256 != review.package_content_sha256
            or test_run.generator_version != review.generator_version
            or test_run.package_schema_version != review.package_schema_version
            or test_run.evidence_hash != review.test_evidence_hash
        ):
            raise AppError(
                code=FACTORY_PUBLISH_EVIDENCE_MISMATCH,
                message="Review↔TestRun lineage가 일치하지 않습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )

        package = await self._repo.get_generated_package(review.generated_package_id)
        if package is None:
            raise AppError(
                code="NOT_FOUND",
                message="Generated Package를 찾을 수 없습니다.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if (
            package.build_id != review.build_id
            or package.job_id != review.job_id
            or package.analysis_artifact_id != review.analysis_artifact_id
            or package.package_content_sha256 != review.package_content_sha256
            or package.generator_version != review.generator_version
            or package.package_schema_version != review.package_schema_version
            or package.source_sha256 != review.source_sha256
            or package.analyzer_version != review.analyzer_version
            or package.build_input_hash != review.build_input_hash
        ):
            raise AppError(
                code=FACTORY_PACKAGE_LINEAGE_MISMATCH,
                message="Review lineage가 Generated Package evidence와 일치하지 않습니다.",
                status_code=status.HTTP_409_CONFLICT,
            )

        payload = await self._assert_package_bytes_match(
            package,
            expected_content_sha256=review.package_content_sha256,
        )
        package_tools = self._assert_package_payload_contract(
            payload,
            package=package,
            expected_generator_version=review.generator_version,
            expected_package_schema_version=review.package_schema_version,
        )

        publish_id = uuid.uuid4()
        now = _utc_now()
        try:
            server_id, tool_ids = await self._import_draft_mcp_assets(
                actor_user_id=actor_user_id,
                review=review,
                package_tools=package_tools,
                publish_id=publish_id,
            )
            tool_id_strs = [str(tid) for tid in tool_ids]
            evidence = {
                "schema_version": FACTORY_PUBLISH_EVIDENCE_SCHEMA_VERSION,
                "kind": FACTORY_PUBLISH_EVIDENCE_KIND,
                "phase": PHASE_PUBLISH,
                "publish_id": str(publish_id),
                "review_id": str(review.id),
                "test_run_id": str(review.test_run_id),
                "generated_package_id": str(review.generated_package_id),
                "build_id": str(review.build_id),
                "job_id": str(review.job_id),
                "analysis_artifact_id": str(review.analysis_artifact_id),
                "package_content_sha256": review.package_content_sha256,
                "generator_version": review.generator_version,
                "package_schema_version": review.package_schema_version,
                "test_evidence_hash": review.test_evidence_hash,
                "review_evidence_hash": review.review_evidence_hash,
                "mcp_server_id": str(server_id),
                "mcp_tool_ids": tool_id_strs,
                "tool_count": len(tool_ids),
                "requested_by": str(actor_user_id),
                "created_at": now.isoformat(),
                "connection_deferred": True,
            }
            evidence_hash = compute_canonical_json_hash(evidence)
            row = await self._repo.create_publish(
                publish_id=publish_id,
                review_id=review.id,
                test_run_id=review.test_run_id,
                generated_package_id=review.generated_package_id,
                build_id=review.build_id,
                job_id=review.job_id,
                analysis_artifact_id=review.analysis_artifact_id,
                package_content_sha256=review.package_content_sha256,
                generator_version=review.generator_version,
                package_schema_version=review.package_schema_version,
                test_evidence_hash=review.test_evidence_hash,
                review_evidence_hash=review.review_evidence_hash,
                mcp_server_id=server_id,
                mcp_tool_ids=tool_id_strs,
                tool_count=len(tool_ids),
                publish_evidence=evidence,
                publish_evidence_hash=evidence_hash,
                requested_by=actor_user_id,
            )
            response = self._publish_to_response(row)
            await self._idempotency.create_completed(
                principal_key=principal_key,
                operation_scope=_PUBLISH_OPERATION_SCOPE,
                idempotency_key=key,
                request_hash=req_hash,
                response_status=status.HTTP_201_CREATED,
                response_body=response.model_dump(mode="json"),
                resource_type=_PUBLISH_RESOURCE_TYPE,
                resource_id=row.id,
                completed_at=now,
            )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if _is_idempotency_pk_violation(exc) or _is_publish_unique_violation(exc):
                raced = await self._idempotency.get(
                    principal_key=principal_key,
                    operation_scope=_PUBLISH_OPERATION_SCOPE,
                    idempotency_key=key,
                )
                if raced is not None:
                    return await self._replay_or_conflict_publish(raced, req_hash)
                existing_publish = await self._repo.get_publish_for_review(review_id)
                if existing_publish is not None:
                    raise AppError(
                        code=FACTORY_PUBLISH_ALREADY_EXISTS,
                        message="이 Review에 대한 Publish가 이미 존재합니다.",
                        status_code=status.HTTP_409_CONFLICT,
                    ) from exc
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="Factory Publish 생성 중 충돌이 발생했습니다.",
                    status_code=status.HTTP_409_CONFLICT,
                ) from exc
            raise

        return FactoryPublishCreateOutcome(
            result=response,
            http_status=status.HTTP_201_CREATED,
            replayed=False,
        )

    async def get_publish(
        self,
        actor_user_id: uuid.UUID,
        publish_id: uuid.UUID,
    ) -> FactoryPublishResponse:
        await self._assert_read(actor_user_id)
        row = await self._repo.get_publish(publish_id)
        if row is None:
            raise AppError(
                code="NOT_FOUND",
                message="Factory Publish를 찾을 수 없습니다.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return self._publish_to_response(row)

    async def get_publish_for_review(
        self,
        actor_user_id: uuid.UUID,
        review_id: uuid.UUID,
    ) -> FactoryPublishResponse:
        await self._assert_read(actor_user_id)
        review = await self._repo.get_review(review_id)
        if review is None:
            raise AppError(
                code="NOT_FOUND",
                message="Factory Review를 찾을 수 없습니다.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        row = await self._repo.get_publish_for_review(review.id)
        if row is None:
            raise AppError(
                code="NOT_FOUND",
                message="이 Review에 대한 Factory Publish가 없습니다.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return self._publish_to_response(row)
