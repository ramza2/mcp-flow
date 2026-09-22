"""Approval decision aggregation and same-Execution resume enqueue (FNC-APR-003/004)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.approval.evidence import (
    assert_current_context_matches_request,
    snapshotted_allow_self_approval,
    snapshotted_reject_comment_required,
)
from app.core.errors import AppError
from app.domain.enums import (
    ApprovalDecisionMode,
    ApprovalDecisionValue,
    ApprovalStatus,
    ExecutionStatus,
    StepStatus,
    UserStatus,
)
from app.models.approval import ApprovalDecision, ApprovalRequest
from app.models.execution import Execution, ExecutionStep
from app.repositories.approval_decision import ApprovalDecisionRepository
from app.repositories.approval_request import ApprovalRequestRepository
from app.repositories.outbox import OutboxRepository
from app.repositories.role import UserRoleRepository
from app.repositories.user import UserRepository
from app.services.authorization import AuthorizationResolver

DecisionLiteral = Literal["APPROVE", "REJECT"]

_ERROR_APPROVAL_REJECTED = "APPROVAL_REJECTED"
_ERROR_APPROVAL_EXPIRED = "APPROVAL_EXPIRED"


@dataclass(frozen=True, slots=True)
class ApprovalDecisionOutcome:
    decision_id: uuid.UUID
    approval_request_id: uuid.UUID
    approval_status: str
    execution_id: uuid.UUID
    step_execution_id: uuid.UUID
    execution_status: str
    step_status: str
    decision: str
    decided_by: uuid.UUID
    decided_at: datetime
    resume_enqueued: bool


def validate_approver_scope(scope: Any) -> list[str] | None:
    """Return required role_codes, or None when decide-permission alone is enough.

    Supported shapes only: null, {}, {"role_codes": ["ROLE_A", ...]}.
    Unknown keys / wrong types / blank codes → fail closed 409.
    """
    if scope is None:
        return None
    if not isinstance(scope, dict):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ApprovalRequest approval_scope is malformed.",
            status_code=409,
        )
    if scope == {}:
        return None
    if set(scope.keys()) != {"role_codes"}:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ApprovalRequest approval_scope has unsupported keys.",
            status_code=409,
        )
    codes = scope["role_codes"]
    if not isinstance(codes, list) or not codes:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ApprovalRequest approval_scope.role_codes must be a non-empty list.",
            status_code=409,
        )
    normalized: list[str] = []
    for item in codes:
        if not isinstance(item, str) or not item.strip():
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="ApprovalRequest approval_scope.role_codes contains a blank or non-string code.",
                status_code=409,
            )
        normalized.append(item.strip())
    return normalized


def aggregate_decision(
    *,
    mode: str,
    required_approvals: int,
    decisions: list[ApprovalDecision],
) -> str | None:
    """Return APPROVED / REJECTED when terminal, else None (remain PENDING)."""
    if required_approvals < 1:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ApprovalRequest.required_approvals must be >= 1.",
            status_code=409,
        )
    approve_count = sum(
        1 for d in decisions if d.decision == ApprovalDecisionValue.APPROVE.value
    )
    reject_count = sum(
        1 for d in decisions if d.decision == ApprovalDecisionValue.REJECT.value
    )

    if mode == ApprovalDecisionMode.ANY.value:
        if approve_count >= 1:
            return ApprovalStatus.APPROVED.value
        if reject_count >= 1:
            return ApprovalStatus.REJECTED.value
        return None

    if mode == ApprovalDecisionMode.ALL.value:
        if reject_count >= 1:
            return ApprovalStatus.REJECTED.value
        if approve_count >= required_approvals:
            return ApprovalStatus.APPROVED.value
        return None

    if mode == ApprovalDecisionMode.QUORUM.value:
        if approve_count >= required_approvals:
            return ApprovalStatus.APPROVED.value
        if reject_count >= required_approvals:
            return ApprovalStatus.REJECTED.value
        return None

    raise AppError(
        code="RESOURCE_CONFLICT",
        message=f"Unsupported decision_mode {mode!r}.",
        status_code=409,
    )


class ApprovalDecisionService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._requests = ApprovalRequestRepository(session)
        self._decisions = ApprovalDecisionRepository(session)
        self._outbox = OutboxRepository(session)
        self._users = UserRepository(session)
        self._user_roles = UserRoleRepository(session)
        self._authz = AuthorizationResolver(session)

    async def decide(
        self,
        *,
        approval_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        decision: DecisionLiteral,
        comment: str | None = None,
        now: datetime | None = None,
    ) -> ApprovalDecisionOutcome:
        ts = now or datetime.now(UTC)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)

        if decision not in (
            ApprovalDecisionValue.APPROVE.value,
            ApprovalDecisionValue.REJECT.value,
        ):
            raise AppError(
                code="VALIDATION_ERROR",
                message="decision must be APPROVE or REJECT.",
                status_code=422,
            )

        trimmed_comment = comment.strip() if isinstance(comment, str) else comment
        if trimmed_comment == "":
            trimmed_comment = None

        # 1. Lookup request lineage (no lock yet).
        request = await self._requests.get(approval_id)
        if request is None:
            raise AppError(
                code="RESOURCE_NOT_FOUND",
                message="ApprovalRequest not found.",
                status_code=404,
            )

        # 2. Lock Execution → 3. Step → 4. ApprovalRequest
        execution = await self._lock_execution(request.execution_id)
        if execution is None:
            raise AppError(
                code="RESOURCE_NOT_FOUND",
                message="Execution not found for ApprovalRequest.",
                status_code=404,
            )
        step = await self._lock_step(request.step_execution_id)
        if step is None or step.execution_id != execution.id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="ApprovalRequest Step lineage is inconsistent.",
                status_code=409,
            )
        locked = await self._requests.lock_for_update(approval_id)
        if locked is None:
            raise AppError(
                code="RESOURCE_NOT_FOUND",
                message="ApprovalRequest not found.",
                status_code=404,
            )
        request = locked

        # 5. Revalidate after locks
        await self._assert_actor_authorized(actor_user_id)

        if request.status != ApprovalStatus.PENDING.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"ApprovalRequest is not PENDING (got {request.status}).",
                status_code=409,
            )

        expires_at = request.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= ts:
            await self._terminalize_expired(
                request=request, execution=execution, step=step, now=ts
            )
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="ApprovalRequest has expired.",
                status_code=409,
            )

        if (
            execution.status != ExecutionStatus.WAITING_APPROVAL.value
            or step.status != StepStatus.WAITING_APPROVAL.value
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Approval decision requires Execution and Step WAITING_APPROVAL "
                    f"(execution={execution.status}, step={step.status})."
                ),
                status_code=409,
            )

        role_codes = validate_approver_scope(request.approval_scope)
        if role_codes is not None:
            await self._assert_role_scope(actor_user_id, role_codes)

        snapshot = await assert_current_context_matches_request(
            self._session,
            request=request,
            execution=execution,
            step=step,
        )

        allow_self = snapshotted_allow_self_approval(snapshot)
        if not allow_self and actor_user_id == request.requested_by:
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Self-approval is not allowed for this ApprovalRequest.",
                status_code=403,
            )

        reject_comment_required = snapshotted_reject_comment_required(snapshot)
        if (
            decision == ApprovalDecisionValue.REJECT.value
            and reject_comment_required
            and trimmed_comment is None
        ):
            raise AppError(
                code="VALIDATION_ERROR",
                message="Reject comment is required.",
                status_code=422,
            )

        existing = await self._decisions.find_by_actor(
            approval_request_id=request.id, decided_by=actor_user_id
        )
        if existing is not None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Actor has already decided on this ApprovalRequest.",
                status_code=409,
            )

        try:
            row = await self._decisions.create(
                approval_request_id=request.id,
                decided_by=actor_user_id,
                decision=decision,
                comment=trimmed_comment,
                context_hash=request.context_hash,
                decided_at=ts,
            )
        except IntegrityError as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Actor has already decided on this ApprovalRequest.",
                status_code=409,
            ) from exc

        all_decisions = await self._decisions.list_for_request(request.id)
        terminal = aggregate_decision(
            mode=request.decision_mode,
            required_approvals=request.required_approvals,
            decisions=all_decisions,
        )

        resume_enqueued = False
        if terminal == ApprovalStatus.APPROVED.value:
            request.status = ApprovalStatus.APPROVED.value
            request.resolved_at = ts
            request.lock_version += 1
            # Execution/Step remain WAITING_APPROVAL; API does NOT set RUNNING.
            await self._outbox.create_execution_approval_resume(
                execution_id=execution.id,
                approval_request_id=request.id,
                created_at=ts,
            )
            resume_enqueued = True
        elif terminal == ApprovalStatus.REJECTED.value:
            request.status = ApprovalStatus.REJECTED.value
            request.resolved_at = ts
            request.lock_version += 1
            self._terminalize_rejected(
                execution=execution, step=step, now=ts
            )
        # else: intermediate — remain PENDING / WAITING_APPROVAL, no Outbox

        await self._session.flush()

        return ApprovalDecisionOutcome(
            decision_id=row.id,
            approval_request_id=request.id,
            approval_status=request.status,
            execution_id=execution.id,
            step_execution_id=step.id,
            execution_status=execution.status,
            step_status=step.status,
            decision=row.decision,
            decided_by=row.decided_by,
            decided_at=row.decided_at,
            resume_enqueued=resume_enqueued,
        )

    async def _assert_actor_authorized(self, actor_user_id: uuid.UUID) -> None:
        user = await self._users.get(actor_user_id)
        if user is None or user.status != UserStatus.ACTIVE.value:
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Actor is not an ACTIVE user.",
                status_code=403,
            )
        if not await self._authz.has_permission(actor_user_id, "approval.decide"):
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Missing approval.decide permission.",
                status_code=403,
            )

    async def _assert_role_scope(
        self, actor_user_id: uuid.UUID, role_codes: list[str]
    ) -> None:
        roles = await self._user_roles.list_roles(actor_user_id)
        actor_codes = {role.code for role in roles}
        if not actor_codes.intersection(role_codes):
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Actor does not match ApprovalRequest approver_scope.role_codes.",
                status_code=403,
            )

    async def _lock_execution(self, execution_id: uuid.UUID) -> Execution | None:
        stmt = select(Execution).where(Execution.id == execution_id).with_for_update()
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _lock_step(self, step_id: uuid.UUID) -> ExecutionStep | None:
        stmt = (
            select(ExecutionStep).where(ExecutionStep.id == step_id).with_for_update()
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    def _terminalize_rejected(
        *, execution: Execution, step: ExecutionStep, now: datetime
    ) -> None:
        step.status = StepStatus.FAILED.value
        step.error_code = _ERROR_APPROVAL_REJECTED
        step.finished_at = now
        step.lock_version += 1
        execution.status = ExecutionStatus.FAILED.value
        execution.error_code = _ERROR_APPROVAL_REJECTED
        execution.finished_at = now
        execution.worker_id = None
        execution.lease_token = None
        execution.lease_expires_at = None
        execution.heartbeat_at = None
        execution.lock_version += 1

    async def _terminalize_expired(
        self,
        *,
        request: ApprovalRequest,
        execution: Execution,
        step: ExecutionStep,
        now: datetime,
    ) -> None:
        request.status = ApprovalStatus.EXPIRED.value
        request.resolved_at = now
        request.lock_version += 1
        step.status = StepStatus.FAILED.value
        step.error_code = _ERROR_APPROVAL_EXPIRED
        step.finished_at = now
        step.lock_version += 1
        execution.status = ExecutionStatus.FAILED.value
        execution.error_code = _ERROR_APPROVAL_EXPIRED
        execution.finished_at = now
        execution.worker_id = None
        execution.lease_token = None
        execution.lease_expires_at = None
        execution.heartbeat_at = None
        execution.lock_version += 1
        await self._session.flush()
