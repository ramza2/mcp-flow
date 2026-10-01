"""Same-Execution approval resume claim (FNC-APR-004).

WAITING_APPROVAL + APPROVED evidence → RUNNING + READY with a fresh lease.
Supports ToolPolicy (single-TOOL) and authorable APPROVAL Step contexts.
Does not create Attempt/ToolCall. Duplicate delivery after claim is a DB no-op.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.approval.evidence import (
    assert_current_context_matches_request,
    is_authorable_step_context,
)
from app.core.errors import AppError
from app.domain.enums import (
    ApprovalStatus,
    AuthorableStepType,
    ExecutionSourceType,
    ExecutionStatus,
    StepStatus,
)
from app.execution.claim import _normalize_worker_id
from app.execution.completion import build_result_summary
from app.execution.dag import cancel_unused_ready_tools, skip_all_pending
from app.execution.runtime_preflight import (
    assert_answered_plan_confirmation,
    assert_current_tool_executable,
)
from app.models.approval import ApprovalRequest
from app.models.execution import Execution, ExecutionStep
from app.repositories.approval_request import ApprovalRequestRepository
from app.repositories.plan_validation import PlanValidationRepository
from app.schemas.execution_plan import (
    DETERMINISTIC_TOOL_STEP_ID,
    ApprovalStepConfigV1,
    ExecutionPlanStep,
    ExecutionPlanV1,
    ToolStepConfigV1,
    compute_plan_hash,
)


@dataclass(frozen=True, slots=True)
class ApprovalResumeClaimOutcome:
    execution_id: uuid.UUID
    approval_request_id: uuid.UUID
    claimed: bool
    status: str | None
    worker_id: str | None
    lease_token: uuid.UUID | None
    lease_expires_at: datetime | None
    ready_step_id: uuid.UUID | None = None
    reason: str | None = None


class ApprovalResumeClaimService:
    def __init__(self, session: AsyncSession, *, lease_seconds: int) -> None:
        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be a positive integer")
        self._session = session
        self._lease_seconds = lease_seconds
        self._requests = ApprovalRequestRepository(session)

    async def claim(
        self,
        *,
        execution_id: uuid.UUID,
        approval_request_id: uuid.UUID,
        worker_id: str,
        now: datetime | None = None,
    ) -> ApprovalResumeClaimOutcome:
        worker = _normalize_worker_id(worker_id)
        ts = now or datetime.now(UTC)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)

        stmt = select(Execution).where(Execution.id == execution_id).with_for_update()
        execution = (await self._session.execute(stmt)).scalar_one_or_none()
        if execution is None:
            return ApprovalResumeClaimOutcome(
                execution_id=execution_id,
                approval_request_id=approval_request_id,
                claimed=False,
                status=None,
                worker_id=None,
                lease_token=None,
                lease_expires_at=None,
                reason="MISSING",
            )

        # Duplicate / late delivery after claim or terminal: DB no-op.
        if execution.status != ExecutionStatus.WAITING_APPROVAL.value:
            return ApprovalResumeClaimOutcome(
                execution_id=execution.id,
                approval_request_id=approval_request_id,
                claimed=False,
                status=execution.status,
                worker_id=execution.worker_id,
                lease_token=None,
                lease_expires_at=execution.lease_expires_at,
                reason="STALE_DELIVERY",
            )

        step_stmt = (
            select(ExecutionStep)
            .where(ExecutionStep.execution_id == execution.id)
            .order_by(ExecutionStep.sequence_hint.asc(), ExecutionStep.step_key.asc())
            .with_for_update()
        )
        steps = list((await self._session.execute(step_stmt)).scalars().all())

        request = await self._requests.lock_for_update(approval_request_id)
        if request is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="ApprovalRequest not found for resume.",
                status_code=409,
            )

        step = next(
            (s for s in steps if s.id == request.step_execution_id),
            None,
        )
        if step is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="ApprovalRequest Step not found on Execution.",
                status_code=409,
            )

        authorable = (
            is_authorable_step_context(request.context_snapshot)
            or step.step_type == AuthorableStepType.APPROVAL.value
        )
        if authorable:
            return await self._claim_authorable(
                execution=execution,
                step=step,
                steps=steps,
                request=request,
                worker=worker,
                now=ts,
            )

        # Existing ToolPolicy path — AgentRequest foundation single TOOL only.
        if len(steps) != 1:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="AgentRequest foundation Execution must contain exactly one Step.",
                status_code=409,
            )

        tool_config = self._assert_structural_resume_preconditions(
            execution=execution, step=step, request=request
        )
        pending = await self._requests.find_pending_for_step(
            execution_id=execution.id, step_execution_id=step.id
        )
        if pending is not None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="PENDING ApprovalRequest still exists; cannot resume.",
                status_code=409,
            )

        try:
            await self._assert_mutable_resume_preflight(
                execution=execution,
                step=step,
                request=request,
                tool_config=tool_config,
            )
        except AppError:
            self._terminalize_resume_precondition_failed(
                execution=execution, step=step, steps=steps, now=ts, authorable=False
            )
            await self._session.flush()
            return ApprovalResumeClaimOutcome(
                execution_id=execution.id,
                approval_request_id=request.id,
                claimed=False,
                status=execution.status,
                worker_id=None,
                lease_token=None,
                lease_expires_at=None,
                reason="PRECONDITION_FAILED",
            )

        return await self._apply_fresh_claim(
            execution=execution,
            step=step,
            request=request,
            worker=worker,
            now=ts,
        )

    async def _claim_authorable(
        self,
        *,
        execution: Execution,
        step: ExecutionStep,
        steps: list[ExecutionStep],
        request: ApprovalRequest,
        worker: str,
        now: datetime,
    ) -> ApprovalResumeClaimOutcome:
        try:
            self._assert_authorable_structural_preconditions(
                execution=execution, step=step, request=request
            )
        except AppError:
            self._terminalize_resume_precondition_failed(
                execution=execution, step=step, steps=steps, now=now, authorable=True
            )
            await self._session.flush()
            return ApprovalResumeClaimOutcome(
                execution_id=execution.id,
                approval_request_id=request.id,
                claimed=False,
                status=execution.status,
                worker_id=None,
                lease_token=None,
                lease_expires_at=None,
                reason="PRECONDITION_FAILED",
            )

        pending = await self._requests.find_pending_for_step(
            execution_id=execution.id, step_execution_id=step.id
        )
        if pending is not None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="PENDING ApprovalRequest still exists; cannot resume.",
                status_code=409,
            )

        try:
            await assert_current_context_matches_request(
                self._session,
                request=request,
                execution=execution,
                step=step,
                steps=steps,
            )
        except AppError:
            self._terminalize_resume_precondition_failed(
                execution=execution, step=step, steps=steps, now=now, authorable=True
            )
            await self._session.flush()
            return ApprovalResumeClaimOutcome(
                execution_id=execution.id,
                approval_request_id=request.id,
                claimed=False,
                status=execution.status,
                worker_id=None,
                lease_token=None,
                lease_expires_at=None,
                reason="PRECONDITION_FAILED",
            )

        return await self._apply_fresh_claim(
            execution=execution,
            step=step,
            request=request,
            worker=worker,
            now=now,
        )

    async def _apply_fresh_claim(
        self,
        *,
        execution: Execution,
        step: ExecutionStep,
        request: ApprovalRequest,
        worker: str,
        now: datetime,
    ) -> ApprovalResumeClaimOutcome:
        token = uuid.uuid4()
        expires_at = now + timedelta(seconds=self._lease_seconds)
        execution.status = ExecutionStatus.RUNNING.value
        execution.worker_id = worker
        execution.lease_token = token
        execution.lease_expires_at = expires_at
        execution.heartbeat_at = now
        # Preserve started_at / requested_at / queued_at / snapshots / attempt_count.
        execution.lock_version += 1

        step.status = StepStatus.READY.value
        if step.ready_at is None:
            step.ready_at = now
        step.lock_version += 1
        await self._session.flush()

        return ApprovalResumeClaimOutcome(
            execution_id=execution.id,
            approval_request_id=request.id,
            claimed=True,
            status=execution.status,
            worker_id=worker,
            lease_token=token,
            lease_expires_at=expires_at,
            ready_step_id=step.id,
        )

    def _assert_authorable_structural_preconditions(
        self,
        *,
        execution: Execution,
        step: ExecutionStep,
        request: ApprovalRequest,
    ) -> None:
        if execution.source_type not in {
            ExecutionSourceType.AGENT_REQUEST.value,
            ExecutionSourceType.MANUAL_TOOL_TEST.value,
        }:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Authorable approval resume supports AGENT_REQUEST / "
                    "MANUAL_TOOL_TEST Executions only."
                ),
                status_code=409,
            )
        if (
            request.execution_id != execution.id
            or request.step_execution_id != step.id
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="ApprovalRequest does not belong to this Execution/Step.",
                status_code=409,
            )
        if request.status != ApprovalStatus.APPROVED.value or request.resolved_at is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Approval resume requires APPROVED request with resolved_at.",
                status_code=409,
            )
        if step.status != StepStatus.WAITING_APPROVAL.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Approval resume requires Step WAITING_APPROVAL "
                    f"(got {step.status})."
                ),
                status_code=409,
            )
        if step.step_type != AuthorableStepType.APPROVAL.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Authorable approval resume requires APPROVAL Step type.",
                status_code=409,
            )
        if not is_authorable_step_context(request.context_snapshot):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Authorable approval resume requires approval_step_context.v1.",
                status_code=409,
            )
        if any(
            value is not None
            for value in (
                execution.worker_id,
                execution.lease_token,
                execution.lease_expires_at,
                execution.heartbeat_at,
            )
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="WAITING_APPROVAL Execution unexpectedly owns a lease.",
                status_code=409,
            )
        try:
            plan = ExecutionPlanV1.model_validate(execution.plan_snapshot)
            plan_step = ExecutionPlanStep.model_validate(step.step_snapshot)
            cfg = ApprovalStepConfigV1.model_validate(plan_step.config)
        except Exception as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution plan/step snapshot is invalid for authorable resume.",
                status_code=409,
            ) from exc
        if (
            execution.plan_schema_version != plan.schema_version
            or compute_plan_hash(execution.plan_snapshot) != execution.plan_hash
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution plan snapshot/hash lineage is inconsistent.",
                status_code=409,
            )
        expected = next((p for p in plan.steps if p.id == step.step_key), None)
        if (
            expected is None
            or expected.model_dump(mode="json") != step.step_snapshot
            or plan_step.type != AuthorableStepType.APPROVAL
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="APPROVAL Step is not an exact immutable Plan projection.",
                status_code=409,
            )
        if step.mcp_tool_version_id is not None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="APPROVAL Step must have null mcp_tool_version_id.",
                status_code=409,
            )
        if request.approval_policy_id != cfg.approval_policy_id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="ApprovalRequest policy id does not match APPROVAL Step config.",
                status_code=409,
            )

    def _assert_structural_resume_preconditions(
        self,
        *,
        execution: Execution,
        step: ExecutionStep,
        request: ApprovalRequest,
    ) -> ToolStepConfigV1:
        if execution.source_type != ExecutionSourceType.AGENT_REQUEST.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Approval resume supports AgentRequest Executions only.",
                status_code=409,
            )
        if (
            request.execution_id != execution.id
            or request.step_execution_id != step.id
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="ApprovalRequest does not belong to this Execution/Step.",
                status_code=409,
            )
        if request.status != ApprovalStatus.APPROVED.value or request.resolved_at is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Approval resume requires APPROVED request with resolved_at.",
                status_code=409,
            )
        if step.status != StepStatus.WAITING_APPROVAL.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Approval resume requires Step WAITING_APPROVAL "
                    f"(got {step.status})."
                ),
                status_code=409,
            )
        if any(
            value is not None
            for value in (
                execution.worker_id,
                execution.lease_token,
                execution.lease_expires_at,
                execution.heartbeat_at,
            )
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="WAITING_APPROVAL Execution unexpectedly owns a lease.",
                status_code=409,
            )

        if (
            step.step_key != DETERMINISTIC_TOOL_STEP_ID
            or step.step_type != AuthorableStepType.TOOL.value
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Approval resume Step is not the foundation TOOL Step.",
                status_code=409,
            )
        try:
            plan = ExecutionPlanV1.model_validate(execution.plan_snapshot)
            plan_step = ExecutionPlanStep.model_validate(step.step_snapshot)
            tool_config = ToolStepConfigV1.model_validate(plan_step.config)
        except Exception as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution plan/step snapshot is invalid for resume.",
                status_code=409,
            ) from exc
        if (
            execution.plan_schema_version != plan.schema_version
            or compute_plan_hash(execution.plan_snapshot) != execution.plan_hash
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution plan snapshot/hash lineage is inconsistent.",
                status_code=409,
            )
        if len(plan.steps) != 1 or plan.steps[0].model_dump(mode="json") != step.step_snapshot:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution plan/step snapshot lineage is inconsistent.",
                status_code=409,
            )
        if step.mcp_tool_version_id != tool_config.tool_version_id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Step mcp_tool_version_id does not match TOOL config.",
                status_code=409,
            )
        return tool_config

    async def _assert_mutable_resume_preflight(
        self,
        *,
        execution: Execution,
        step: ExecutionStep,
        request: ApprovalRequest,
        tool_config: ToolStepConfigV1,
    ) -> None:
        assert execution.agent_version_id is not None
        authz = await assert_current_tool_executable(
            self._session,
            requester_id=execution.requester_id,
            agent_version_id=execution.agent_version_id,
            tool_version_id=tool_config.tool_version_id,
            expected_policy_snapshot=dict(execution.policy_snapshot),
            plan_timeout_seconds=_plan_timeout_seconds(step),
        )
        if (
            authz.grant.requires_confirmation
            or authz.tool_policy.requires_confirmation
        ):
            await _assert_confirmation_evidence(
                self._session, execution, authz.policy_snapshot
            )

        await assert_current_context_matches_request(
            self._session,
            request=request,
            execution=execution,
            step=step,
            tool_policy=authz.tool_policy,
            approval_policy=authz.approval_policy,
        )

    @staticmethod
    def _terminalize_resume_precondition_failed(
        *,
        execution: Execution,
        step: ExecutionStep,
        steps: list[ExecutionStep],
        now: datetime,
        authorable: bool,
    ) -> None:
        error_code = "APPROVAL_RESUME_PRECONDITION_FAILED"
        step.status = StepStatus.FAILED.value
        step.error_code = error_code
        step.finished_at = now
        step.lock_version += 1
        by_key = {s.step_key: s for s in steps}
        by_key[step.step_key] = step
        if authorable and len(steps) > 1:
            skip_all_pending(by_key=by_key, now=now)
            cancel_unused_ready_tools(by_key=by_key, now=now)
        execution.status = ExecutionStatus.FAILED.value
        execution.error_code = error_code
        execution.result_summary = build_result_summary(
            status=ExecutionStatus.FAILED.value,
            steps=list(by_key.values()),
            plan=None,
        )
        execution.finished_at = now
        execution.worker_id = None
        execution.lease_token = None
        execution.lease_expires_at = None
        execution.heartbeat_at = None
        execution.lock_version += 1


def _plan_timeout_seconds(step: ExecutionStep) -> int | None:
    snapshot = step.step_snapshot if isinstance(step.step_snapshot, dict) else None
    timeout = snapshot.get("timeout_seconds") if snapshot is not None else None
    if timeout is None:
        return None
    if not isinstance(timeout, int) or isinstance(timeout, bool):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Step timeout_seconds is invalid.",
            status_code=409,
        )
    return timeout


async def _assert_confirmation_evidence(
    session: AsyncSession, execution: Execution, policy_snapshot: dict
) -> None:
    if execution.agent_request_id is None or execution.plan_validation_run_id is None:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="PLAN_CONFIRMATION evidence lineage missing on Execution.",
            status_code=409,
        )
    validation = await PlanValidationRepository(session).get_by_id(
        execution.plan_validation_run_id
    )
    if validation is None:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="Pinned PlanValidationRun not found for confirmation check.",
            status_code=409,
        )
    await assert_answered_plan_confirmation(
        session,
        agent_request_id=execution.agent_request_id,
        requester_id=execution.requester_id,
        plan_generation_run_id=validation.plan_generation_run_id,
        plan_hash=execution.plan_hash,
        policy_snapshot=policy_snapshot,
    )
