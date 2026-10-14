"""Factory sandbox TestRun worker: claim, reload package, security+contract."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.canonical_hash import compute_canonical_json_hash
from app.core.config import get_settings
from app.domain.enums import JobStatus
from app.execution.claim import _as_utc
from app.factory import PHASE_SANDBOX_TEST
from app.factory.errors import (
    FACTORY_PACKAGE_OBJECT_STORE_FAILED,
    FACTORY_SOURCE_OBJECT_MISSING,
)
from app.factory.sandbox_contract import (
    SandboxContractError,
    run_contract_checks,
)
from app.factory.sandbox_security import (
    SandboxSecurityError,
    run_security_checks,
)
from app.infrastructure.object_storage import (
    ObjectStorageClient,
    ObjectStorageError,
    get_object_storage,
)
from app.models.factory import ToolFactoryTestRun
from app.repositories.factory import FactoryRepository

logger = logging.getLogger(__name__)

_EVIDENCE_KIND = "FACTORY_SANDBOX_TEST_EVIDENCE"
_EVIDENCE_SCHEMA_VERSION = 1


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class FactoryTestClaimOutcome:
    claimed: bool
    reason: str
    test_run: ToolFactoryTestRun | None
    lease_token: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class FactoryTestRunOutcome:
    test_run_id: uuid.UUID
    status: str
    claimed: bool
    reason: str


class FactoryTestWorkerService:
    """DB-fenced sandbox TestRun runner. Broker delivery is not business state."""

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
        test_run_id: uuid.UUID,
        worker_id: str | None = None,
    ) -> FactoryTestClaimOutcome:
        wid = worker_id or self._worker_id
        row = await self._repo.get_test_run_for_update(test_run_id)
        if row is None:
            return FactoryTestClaimOutcome(
                claimed=False,
                reason="NOT_FOUND",
                test_run=None,
                lease_token=None,
            )
        if row.status in {
            JobStatus.SUCCEEDED.value,
            JobStatus.FAILED.value,
            JobStatus.CANCELLED.value,
            JobStatus.TIMED_OUT.value,
        }:
            return FactoryTestClaimOutcome(
                claimed=False,
                reason="TERMINAL",
                test_run=row,
                lease_token=None,
            )

        now = _utc_now()
        if row.status == JobStatus.QUEUED.value:
            pass
        elif row.status == JobStatus.RUNNING.value:
            if (
                row.lease_expires_at is not None
                and _as_utc(row.lease_expires_at) > _as_utc(now)
            ):
                return FactoryTestClaimOutcome(
                    claimed=False,
                    reason="LEASE_HELD",
                    test_run=row,
                    lease_token=None,
                )
        else:
            return FactoryTestClaimOutcome(
                claimed=False,
                reason="UNEXPECTED_STATUS",
                test_run=row,
                lease_token=None,
            )

        # Idempotent restart: terminal evidence already present.
        if row.evidence_hash and row.result in {"PASS", "FAIL"}:
            return FactoryTestClaimOutcome(
                claimed=False,
                reason="TERMINAL",
                test_run=row,
                lease_token=None,
            )

        token = uuid.uuid4()
        row.status = JobStatus.RUNNING.value
        row.worker_id = wid
        row.lease_token = token
        row.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
        row.started_at = row.started_at or now
        row.lock_version = int(row.lock_version) + 1
        await self._session.flush()
        await self._session.refresh(row)
        return FactoryTestClaimOutcome(
            claimed=True,
            reason="CLAIMED",
            test_run=row,
            lease_token=token,
        )

    async def process_test_run(
        self,
        *,
        test_run_id: uuid.UUID,
        worker_id: str | None = None,
    ) -> FactoryTestRunOutcome:
        claim = await self.claim(test_run_id=test_run_id, worker_id=worker_id)
        if not claim.claimed or claim.test_run is None or claim.lease_token is None:
            status = claim.test_run.status if claim.test_run is not None else "MISSING"
            return FactoryTestRunOutcome(
                test_run_id=test_run_id,
                status=status,
                claimed=False,
                reason=claim.reason,
            )
        # Durable lease before Object Storage / validation work (matches Job path).
        await self._session.commit()
        return await self.run_claimed(
            test_run=claim.test_run,
            lease_token=claim.lease_token,
        )

    async def run_claimed(
        self,
        *,
        test_run: ToolFactoryTestRun,
        lease_token: uuid.UUID,
    ) -> FactoryTestRunOutcome:
        current = await self._repo.get_test_run_for_update(test_run.id)
        if current is None:
            return FactoryTestRunOutcome(
                test_run_id=test_run.id,
                status="MISSING",
                claimed=False,
                reason="NOT_FOUND",
            )
        if (
            current.status != JobStatus.RUNNING.value
            or current.lease_token != lease_token
        ):
            return FactoryTestRunOutcome(
                test_run_id=current.id,
                status=current.status,
                claimed=False,
                reason="LEASE_LOST",
            )

        package_row = await self._repo.get_generated_package(
            current.generated_package_id
        )
        if package_row is None:
            finished = _utc_now()
            await self._fail(
                current,
                code="NOT_FOUND",
                message="Generated Package for TestRun is missing.",
                finished_at=finished,
            )
            return FactoryTestRunOutcome(
                test_run_id=current.id,
                status=JobStatus.FAILED.value,
                claimed=True,
                reason="PACKAGE_MISSING",
            )

        # Lineage agreement between TestRun snapshot and durable package row.
        if (
            package_row.build_id != current.build_id
            or package_row.job_id != current.job_id
            or package_row.analysis_artifact_id != current.analysis_artifact_id
            or package_row.package_content_sha256 != current.package_content_sha256
            or package_row.generator_version != current.generator_version
            or package_row.package_schema_version != current.package_schema_version
            or package_row.source_sha256 != current.source_sha256
            or package_row.analyzer_version != current.analyzer_version
            or package_row.build_input_hash != current.build_input_hash
        ):
            finished = _utc_now()
            await self._fail(
                current,
                code="FACTORY_PACKAGE_LINEAGE_MISMATCH",
                message="TestRun lineage does not match Generated Package evidence.",
                finished_at=finished,
            )
            return FactoryTestRunOutcome(
                test_run_id=current.id,
                status=JobStatus.FAILED.value,
                claimed=True,
                reason="LINEAGE_MISMATCH",
            )

        storage = self._storage_client()
        try:
            raw = await storage.get_bytes(
                bucket=package_row.object_bucket,
                object_key=package_row.object_key,
            )
        except ObjectStorageError as exc:
            finished = _utc_now()
            code = exc.code or FACTORY_PACKAGE_OBJECT_STORE_FAILED
            if code == FACTORY_SOURCE_OBJECT_MISSING:
                code = FACTORY_SOURCE_OBJECT_MISSING
            await self._fail(
                current,
                code=code,
                message=exc.message,
                finished_at=finished,
            )
            return FactoryTestRunOutcome(
                test_run_id=current.id,
                status=JobStatus.FAILED.value,
                claimed=True,
                reason=code,
            )

        try:
            package = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            finished = _utc_now()
            await self._fail(
                current,
                code="FACTORY_PACKAGE_SCHEMA_INVALID",
                message="Generated Package object payload is corrupt.",
                finished_at=finished,
            )
            return FactoryTestRunOutcome(
                test_run_id=current.id,
                status=JobStatus.FAILED.value,
                claimed=True,
                reason="CORRUPT_PAYLOAD",
            )
        if not isinstance(package, dict):
            finished = _utc_now()
            await self._fail(
                current,
                code="FACTORY_PACKAGE_SCHEMA_INVALID",
                message="Generated Package object payload must be a JSON object.",
                finished_at=finished,
            )
            return FactoryTestRunOutcome(
                test_run_id=current.id,
                status=JobStatus.FAILED.value,
                claimed=True,
                reason="CORRUPT_PAYLOAD",
            )

        try:
            security_result = run_security_checks(
                raw=raw,
                package=package,
                expected_size_bytes=package_row.package_size_bytes,
                expected_content_sha256=package_row.package_content_sha256,
                expected_generator_version=package_row.generator_version,
                expected_package_schema_version=package_row.package_schema_version,
                expected_build_id=str(package_row.build_id),
                expected_build_input_hash=package_row.build_input_hash,
                expected_job_id=str(package_row.job_id),
                expected_analysis_artifact_id=str(package_row.analysis_artifact_id),
                expected_source_sha256=package_row.source_sha256,
                expected_analyzer_version=package_row.analyzer_version,
                expected_tool_count=package_row.tool_count,
                policy_version=current.security_policy_version,
            )
            contract_result = run_contract_checks(
                package=package,
                expected_job_id=str(package_row.job_id),
                expected_analysis_artifact_id=str(package_row.analysis_artifact_id),
                expected_source_sha256=package_row.source_sha256,
                expected_analyzer_version=package_row.analyzer_version,
                expected_build_id=str(package_row.build_id),
                expected_build_input_hash=package_row.build_input_hash,
                policy_version=current.contract_policy_version,
            )
        except (SandboxSecurityError, SandboxContractError) as exc:
            finished = _utc_now()
            await self._fail(
                current,
                code=exc.code,
                message=exc.message,
                finished_at=finished,
            )
            return FactoryTestRunOutcome(
                test_run_id=current.id,
                status=JobStatus.FAILED.value,
                claimed=True,
                reason=exc.code,
            )

        evidence = {
            "schema_version": _EVIDENCE_SCHEMA_VERSION,
            "kind": _EVIDENCE_KIND,
            "phase": PHASE_SANDBOX_TEST,
            "test_run_id": str(current.id),
            "generated_package_id": str(current.generated_package_id),
            "build_id": str(current.build_id),
            "job_id": str(current.job_id),
            "analysis_artifact_id": str(current.analysis_artifact_id),
            "package_content_sha256": current.package_content_sha256,
            "generator_version": current.generator_version,
            "security_policy_version": current.security_policy_version,
            "contract_policy_version": current.contract_policy_version,
            "security": security_result,
            "contract": contract_result,
            "result": "PASS",
        }
        evidence_hash = compute_canonical_json_hash(evidence)
        finished = _utc_now()
        await self._repo.finish_test_run_succeeded(
            current,
            security_result=security_result,
            contract_result=contract_result,
            evidence=evidence,
            evidence_hash=evidence_hash,
            finished_at=finished,
        )
        return FactoryTestRunOutcome(
            test_run_id=current.id,
            status=JobStatus.SUCCEEDED.value,
            claimed=True,
            reason="SUCCEEDED",
        )

    async def _fail(
        self,
        row: ToolFactoryTestRun,
        *,
        code: str,
        message: str,
        finished_at: datetime,
    ) -> None:
        evidence = {
            "schema_version": _EVIDENCE_SCHEMA_VERSION,
            "kind": _EVIDENCE_KIND,
            "phase": PHASE_SANDBOX_TEST,
            "test_run_id": str(row.id),
            "generated_package_id": str(row.generated_package_id),
            "build_id": str(row.build_id),
            "job_id": str(row.job_id),
            "analysis_artifact_id": str(row.analysis_artifact_id),
            "package_content_sha256": row.package_content_sha256,
            "generator_version": row.generator_version,
            "security_policy_version": row.security_policy_version,
            "contract_policy_version": row.contract_policy_version,
            "result": "FAIL",
            "error_code": code,
            "error_message": message,
        }
        evidence_hash = compute_canonical_json_hash(evidence)
        await self._repo.finish_test_run_failed(
            row,
            error_code=code,
            error_message=message,
            finished_at=finished_at,
            evidence=evidence,
            evidence_hash=evidence_hash,
        )
