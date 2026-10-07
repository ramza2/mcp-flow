"""Bounded ApprovalRequest expiry sweep (FNC-APR-003)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.approval.evidence import is_authorable_step_context
from app.core.errors import AppError
from app.domain.enums import ApprovalStatus, ExecutionStatus, StepStatus
from app.execution.completion import build_result_summary
from app.execution.dag import cancel_unused_ready_tools, skip_all_pending
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

            from app.execution.events import ExecutionEventWriter

            event_writer = ExecutionEventWriter(self._session)
            previous_step_status = step.status
            previous_exec_status = execution.status
            request.status = ApprovalStatus.EXPIRED.value
            request.resolved_at = ts
            request.lock_version += 1
            step.status = StepStatus.FAILED.value
            step.error_code = _ERROR_APPROVAL_EXPIRED
            step.finished_at = ts
            step.lock_version += 1

            all_steps = list(
                (
                    await self._session.execute(
                        select(ExecutionStep)
                        .where(ExecutionStep.execution_id == execution.id)
                        .order_by(
                            ExecutionStep.sequence_hint.asc(),
                            ExecutionStep.step_key.asc(),
                        )
                    )
                ).scalars().all()
            )
            pending_before = {
                s.id: s.status
                for s in all_steps
                if s.status == StepStatus.PENDING.value
            }
            by_key = {s.step_key: s for s in all_steps}
            by_key[step.step_key] = step
            authorable = is_authorable_step_context(request.context_snapshot)
            # Authorable expiry is mandatory-fatal (ignore on_error).
            if authorable and len(all_steps) > 1:
                skip_all_pending(by_key=by_key, now=ts)
                cancel_unused_ready_tools(by_key=by_key, now=ts)

            execution.status = ExecutionStatus.FAILED.value
            execution.error_code = _ERROR_APPROVAL_EXPIRED
            if authorable:
                execution.error_message = "Authorable APPROVAL Step expired."
            execution.result_summary = build_result_summary(
                status=ExecutionStatus.FAILED.value,
                steps=list(by_key.values()),
                plan=None,
            )
            execution.finished_at = ts
            execution.worker_id = None
            execution.lease_token = None
            execution.lease_expires_at = None
            execution.heartbeat_at = None
            execution.lock_version += 1
            await event_writer.emit_approval_decided(
                execution_id=execution.id,
                step_execution_id=step.id,
                approval_request_id=request.id,
                status=request.status,
                occurred_at=ts,
            )
            await event_writer.emit_step_status_changed(
                step, previous_status=previous_step_status, occurred_at=ts
            )
            for skipped in by_key.values():
                if (
                    skipped.id in pending_before
                    and skipped.status == StepStatus.SKIPPED.value
                ):
                    await event_writer.emit_step_status_changed(
                        skipped,
                        previous_status=pending_before[skipped.id],
                        occurred_at=ts,
                    )
            await event_writer.emit_execution_status_changed(
                execution, previous_status=previous_exec_status, occurred_at=ts
            )
            expired += 1

        await self._session.flush()
        return ExpiryBatchResult(selected=len(ids), expired=expired)
