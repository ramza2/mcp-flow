"""Tool Factory analysis worker: claim Job, fetch source, run analyze_openapi."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.canonical_hash import compute_canonical_json_hash
from app.core.config import get_settings
from app.domain.enums import JobStatus
from app.factory import (
    ARTIFACT_CONTENT_TYPE_JSON,
    ARTIFACT_TYPE_OPENAPI_ANALYSIS,
    PHASE_ANALYZE,
)
from app.factory.errors import (
    FACTORY_SOURCE_OBJECT_CORRUPT,
    FACTORY_SOURCE_OBJECT_FETCH_FAILED,
    FACTORY_SOURCE_OBJECT_MISSING,
    FactoryAnalysisError,
)
from app.factory.openapi_analyzer import analyze_openapi
from app.infrastructure.object_storage import (
    ObjectStorageClient,
    ObjectStorageError,
    get_object_storage,
)
from app.factory.contracts import FactoryOpenAPIAnalysis
from app.models.factory import ToolFactoryJob
from app.repositories.factory import FactoryRepository
from app.services.factory import FactoryService

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class FactoryClaimOutcome:
    claimed: bool
    reason: str
    job: ToolFactoryJob | None
    lease_token: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class FactoryAnalyzeOutcome:
    job_id: uuid.UUID
    status: str
    claimed: bool
    reason: str


class FactoryWorkerService:
    """DB-fenced Factory Job runner. Broker delivery is not business state."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        storage: ObjectStorageClient | None = None,
        lease_seconds: int | None = None,
        worker_id: str = "factory-worker",
    ) -> None:
        self._session = session
        self._repo = FactoryRepository(session)
        self._storage = storage
        settings = get_settings()
        self._lease_seconds = lease_seconds or settings.factory_lease_seconds
        self._worker_id = worker_id

    def _storage_client(self) -> ObjectStorageClient:
        if self._storage is not None:
            return self._storage
        return get_object_storage()

    async def claim(
        self,
        *,
        job_id: uuid.UUID,
        worker_id: str | None = None,
    ) -> FactoryClaimOutcome:
        """Claim QUEUED Job, or reconstruct RUNNING with expired lease."""

        wid = worker_id or self._worker_id
        job = await self._repo.get_job_for_update(job_id)
        if job is None:
            return FactoryClaimOutcome(
                claimed=False,
                reason="NOT_FOUND",
                job=None,
                lease_token=None,
            )
        if job.status in {
            JobStatus.SUCCEEDED.value,
            JobStatus.FAILED.value,
            JobStatus.CANCELLED.value,
            JobStatus.TIMED_OUT.value,
        }:
            return FactoryClaimOutcome(
                claimed=False,
                reason="TERMINAL",
                job=job,
                lease_token=None,
            )

        now = _utc_now()
        if job.status == JobStatus.QUEUED.value:
            pass
        elif job.status == JobStatus.RUNNING.value:
            if job.lease_expires_at is not None and job.lease_expires_at > now:
                return FactoryClaimOutcome(
                    claimed=False,
                    reason="LEASE_HELD",
                    job=job,
                    lease_token=None,
                )
            # Expired RUNNING lease → reconstruct from durable Job + object ref.
        elif job.status == JobStatus.PENDING.value:
            return FactoryClaimOutcome(
                claimed=False,
                reason="NOT_QUEUED",
                job=job,
                lease_token=None,
            )
        else:
            return FactoryClaimOutcome(
                claimed=False,
                reason="UNEXPECTED_STATUS",
                job=job,
                lease_token=None,
            )

        token = uuid.uuid4()
        job.status = JobStatus.RUNNING.value
        job.worker_id = wid
        job.lease_token = token
        job.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
        job.started_at = job.started_at or now
        job.current_phase = PHASE_ANALYZE
        job.lock_version = int(job.lock_version) + 1
        await self._session.flush()
        await self._session.refresh(job)
        return FactoryClaimOutcome(
            claimed=True,
            reason="CLAIMED",
            job=job,
            lease_token=token,
        )

    async def run_claimed(
        self,
        *,
        job: ToolFactoryJob,
        lease_token: uuid.UUID,
    ) -> FactoryAnalyzeOutcome:
        """Fetch/verify source and persist analysis outcome for a claimed Job."""

        # Re-load under same session identity; verify lease still matches.
        current = await self._repo.get_job_for_update(job.id)
        if current is None:
            return FactoryAnalyzeOutcome(
                job_id=job.id,
                status="MISSING",
                claimed=False,
                reason="NOT_FOUND",
            )
        if (
            current.status != JobStatus.RUNNING.value
            or current.lease_token != lease_token
        ):
            return FactoryAnalyzeOutcome(
                job_id=job.id,
                status=current.status,
                claimed=False,
                reason="LEASE_LOST",
            )

        if (
            not current.source_bucket
            or not current.source_object_key
            or current.source_size_bytes is None
        ):
            finished = _utc_now()
            await self._repo.finish_job_failed(
                current,
                error_code=FACTORY_SOURCE_OBJECT_MISSING,
                error_message="Factory Job is missing durable source object metadata.",
                finished_at=finished,
            )
            return FactoryAnalyzeOutcome(
                job_id=current.id,
                status=JobStatus.FAILED.value,
                claimed=True,
                reason="MISSING_METADATA",
            )

        # Restart reconstruction: artifact already present → complete without re-analyze.
        existing = await self._repo.get_analysis_artifact(
            current.id,
            artifact_type=ARTIFACT_TYPE_OPENAPI_ANALYSIS,
        )
        if existing is not None and existing.inline_payload is not None:
            payload = existing.inline_payload
            finished = _utc_now()
            await self._repo.finish_job_success(
                current,
                source_format=str(payload.get("source_format") or current.source_format or "JSON"),
                operation_count=len(payload.get("operations") or []),
                server_count=len(payload.get("servers") or []),
                finished_at=finished,
            )
            analysis = FactoryOpenAPIAnalysis.model_validate(payload)
            await FactoryService(self._session).materialize_candidates_from_analysis(
                job=current,
                artifact=existing,
                analysis=analysis,
            )
            return FactoryAnalyzeOutcome(
                job_id=current.id,
                status=JobStatus.SUCCEEDED.value,
                claimed=True,
                reason="RECONSTRUCTED_ARTIFACT",
            )

        storage = self._storage_client()
        try:
            raw = await storage.get_bytes(
                bucket=current.source_bucket,
                object_key=current.source_object_key,
            )
        except ObjectStorageError as exc:
            finished = _utc_now()
            code = exc.code or FACTORY_SOURCE_OBJECT_FETCH_FAILED
            if code == FACTORY_SOURCE_OBJECT_MISSING:
                message = "Factory source object was not found in Object Storage."
            else:
                message = "Failed to fetch Factory source object."
            await self._repo.finish_job_failed(
                current,
                error_code=code,
                error_message=message,
                finished_at=finished,
            )
            return FactoryAnalyzeOutcome(
                job_id=current.id,
                status=JobStatus.FAILED.value,
                claimed=True,
                reason=code,
            )

        digest = hashlib.sha256(raw).hexdigest()
        if digest != current.source_sha256 or len(raw) != int(current.source_size_bytes):
            finished = _utc_now()
            await self._repo.finish_job_failed(
                current,
                error_code=FACTORY_SOURCE_OBJECT_CORRUPT,
                error_message="Factory source object failed hash/size re-verification.",
                finished_at=finished,
            )
            return FactoryAnalyzeOutcome(
                job_id=current.id,
                status=JobStatus.FAILED.value,
                claimed=True,
                reason=FACTORY_SOURCE_OBJECT_CORRUPT,
            )

        try:
            analysis = analyze_openapi(raw, filename=current.source_name)
        except FactoryAnalysisError as exc:
            finished = _utc_now()
            await self._repo.finish_job_failed(
                current,
                error_code=exc.code,
                error_message=exc.message,
                finished_at=finished,
            )
            return FactoryAnalyzeOutcome(
                job_id=current.id,
                status=JobStatus.FAILED.value,
                claimed=True,
                reason=exc.code,
            )

        payload = analysis.model_dump(mode="json")
        encoded = _canonical_json_bytes(payload)
        content_sha256 = compute_canonical_json_hash(payload)
        artifact = await self._repo.create_analysis_artifact(
            job_id=current.id,
            artifact_type=ARTIFACT_TYPE_OPENAPI_ANALYSIS,
            content_type=ARTIFACT_CONTENT_TYPE_JSON,
            content_sha256=content_sha256,
            size_bytes=len(encoded),
            inline_payload=payload,
        )
        finished = _utc_now()
        await self._repo.finish_job_success(
            current,
            source_format=analysis.source_format,
            operation_count=len(analysis.operations),
            server_count=len(analysis.servers),
            finished_at=finished,
        )
        await FactoryService(self._session).materialize_candidates_from_analysis(
            job=current,
            artifact=artifact,
            analysis=analysis,
        )
        return FactoryAnalyzeOutcome(
            job_id=current.id,
            status=JobStatus.SUCCEEDED.value,
            claimed=True,
            reason="ANALYZED",
        )

    async def process_job(
        self,
        *,
        job_id: uuid.UUID,
        worker_id: str | None = None,
    ) -> FactoryAnalyzeOutcome:
        claim = await self.claim(job_id=job_id, worker_id=worker_id)
        if not claim.claimed or claim.job is None or claim.lease_token is None:
            return FactoryAnalyzeOutcome(
                job_id=job_id,
                status=claim.job.status if claim.job is not None else "MISSING",
                claimed=False,
                reason=claim.reason,
            )
        await self._session.commit()
        # Fresh claim TX committed; continue analysis in a follow-up caller's session
        # when used from Celery. For in-process use, re-open path via run_claimed
        # after commit requires the same session still holds the row — commit ended
        # the transaction but row remains loaded. Re-claim check uses FOR UPDATE.
        return await self.run_claimed(job=claim.job, lease_token=claim.lease_token)
