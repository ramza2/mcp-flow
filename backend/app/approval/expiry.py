"""Bounded ApprovalRequest expiry sweep (FNC-APR-003)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import ApprovalStatus, ExecutionStatus, StepStatus
from app.models.execution import Execution, ExecutionStep
from app.repositories.approval_request import ApprovalRequestRepository

_ERROR_APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
_MAX_BATCH = 500


@dataclass(frozen=True, slots=True)
class ExpiryBatchResult:
    selected: int
    expired: int


class ApprovalExpiryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._requests = ApprovalRequestRepository(session)

    async def expire_due_batch(
        self, *, limit: int, now: datetime | None = None
    ) -> ExpiryBatchResult:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
            or limit > _MAX_BATCH
        ):
            raise AppError(
                code="VALIDATION_ERROR",
                message=f"expiry batch limit must be between 1 and {_MAX_BATCH}.",
                status_code=400,
            )
        ts = now or datetime.now(UTC)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)

        ids = await self._requests.list_expired_pending_ids(now=ts, limit=limit)
        expired = 0
        for request_id in ids:
            request_probe = await self._requests.get(request_id)
            if request_probe is None:
                continue
            if request_probe.status != ApprovalStatus.PENDING.value:
                continue

            execution = (
                await self._session.execute(
                    select(Execution)
                    .where(Execution.id == request_probe.execution_id)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if execution is None:
                continue
            step = (
                await self._session.execute(
                    select(ExecutionStep)
                    .where(ExecutionStep.id == request_probe.step_execution_id)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if step is None or step.execution_id != execution.id:
                continue
            request = await self._requests.lock_for_update(request_id)
            if request is None:
                continue
            if request.status != ApprovalStatus.PENDING.value:
                continue
            expires_at = request.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at > ts:
                continue
            if (
                execution.status != ExecutionStatus.WAITING_APPROVAL.value
                or step.status != StepStatus.WAITING_APPROVAL.value
            ):
                continue

            request.status = ApprovalStatus.EXPIRED.value
            request.resolved_at = ts
            request.lock_version += 1
            step.status = StepStatus.FAILED.value
            step.error_code = _ERROR_APPROVAL_EXPIRED
            step.finished_at = ts
            step.lock_version += 1
            execution.status = ExecutionStatus.FAILED.value
            execution.error_code = _ERROR_APPROVAL_EXPIRED
            execution.finished_at = ts
            execution.worker_id = None
            execution.lease_token = None
            execution.lease_expires_at = None
            execution.heartbeat_at = None
            execution.lock_version += 1
            expired += 1

        await self._session.flush()
        return ExpiryBatchResult(selected=len(ids), expired=expired)
