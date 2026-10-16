"""Expired RUNNING Workflow/Schedule DAG recovery (Issues #115 / #117).

Whole-Execution classification must complete before any new MCP call. Flat
FOR_EACH / WHILE LOOP Plans are supported via durable checkpoint validation
(``loop_recovery``) before takeover; progression reuses ExecutionOrchestrator.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import (
    AuthorableStepType,
    ExecutionSourceType,
    ExecutionStatus,
    RiskClass,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
)
from app.execution.completion import is_continuable_known_failure
from app.execution.dag import cancel_unused_ready_tools, skip_all_pending
from app.execution.lineage import assert_resume_attempt_lineage, assert_tool_step_lineage
from app.execution.loop_recovery import (
    assert_execution_loop_recovery_checkpoints,
    assert_loop_recovery_ownership,
    plan_supports_flat_loop_running_recovery,
)
from app.execution.loop_reconcile import runtime_plan_step
from app.execution.orchestrator import assert_execution_plan_lineage
from app.execution.policy_selection import get_expected_tool_policy_snapshot
from app.execution.runtime_preflight import _assert_schedule_occurrence_lineage
from app.models.execution import Execution, ExecutionStep, StepAttempt, ToolCall
from app.repositories.execution import ExecutionRepository
from app.schemas.execution_plan import ExecutionPlanV1

# Local copies of recovery risk helpers to avoid circular imports with recovery.py.
_SAFE_RETRY_RISKS = frozenset(
    {RiskClass.READ_ONLY.value, RiskClass.IDEMPOTENT_WRITE.value}
)
_UNSAFE_RISKS = frozenset(
    {
        RiskClass.NON_IDEMPOTENT_WRITE.value,
        RiskClass.DESTRUCTIVE.value,
        RiskClass.UNKNOWN.value,
    }
)


def _is_safe_retry_risk(risk_class: str) -> bool:
    return risk_class in _SAFE_RETRY_RISKS


def _is_unsafe_ambiguous_risk(risk_class: str) -> bool:
    return risk_class in _UNSAFE_RISKS


def _can_safe_retry(*, attempt_count: int, max_attempts: int) -> bool:
    if max_attempts < 1 or attempt_count < 0:
        return False
    return attempt_count < max_attempts

_ERROR_CODE_LEASE_EXPIRED = "WORKER_LEASE_EXPIRED"
_ERROR_CODE_INCONSISTENT = "RECOVERY_INCONSISTENT_EVIDENCE"
_ERROR_LAYER = "WORKER"

_SUPPORTED_STEP_TYPES = frozenset(
    {
        AuthorableStepType.TOOL.value,
        AuthorableStepType.CONDITION.value,
        AuthorableStepType.JOIN.value,
        AuthorableStepType.APPROVAL.value,
        AuthorableStepType.LOOP.value,
    }
)

_NON_LOOP_STEP_TYPES = frozenset(
    {
        AuthorableStepType.TOOL.value,
        AuthorableStepType.CONDITION.value,
        AuthorableStepType.JOIN.value,
        AuthorableStepType.APPROVAL.value,
    }
)

_STEP_TERMINAL = frozenset(
    {
        StepStatus.SUCCEEDED.value,
        StepStatus.FAILED.value,
        StepStatus.TIMED_OUT.value,
        StepStatus.CANCELLED.value,
        StepStatus.SKIPPED.value,
        StepStatus.UNKNOWN_OUTCOME.value,
    }
)

_ATTEMPT_TERMINAL = frozenset(
    {
        StepAttemptStatus.SUCCEEDED.value,
        StepAttemptStatus.FAILED.value,
        StepAttemptStatus.TIMED_OUT.value,
        StepAttemptStatus.CANCELLED.value,
        StepAttemptStatus.UNKNOWN_OUTCOME.value,
    }
)

_LEASE_FREE_STEP = frozenset(
    {
        StepStatus.WAITING_APPROVAL.value,
        StepStatus.WAITING_INPUT.value,
    }
)


class ToolStepRecoveryKind(StrEnum):
    READY = "READY"
    RESUME = "RESUME"
    SAFE_RETRY = "SAFE_RETRY"
    FAIL_EXHAUSTED = "FAIL_EXHAUSTED"
    FATAL_UNKNOWN = "FATAL_UNKNOWN"
    TERMINAL_OK = "TERMINAL_OK"
    LOCAL = "LOCAL"


@dataclass(frozen=True, slots=True)
class ToolStepClassification:
    step_id: uuid.UUID
    kind: ToolStepRecoveryKind
    on_error: str | None = None
    attempt_id: uuid.UUID | None = None
    tool_call_id: uuid.UUID | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowDagRecoveryResult:
    decision: str
    taken_over: bool
    invoke_runner: bool
    worker_id: str | None
    lease_token: uuid.UUID | None
    reason: str


def plan_has_loop(plan: ExecutionPlanV1) -> bool:
    return any(step.type == AuthorableStepType.LOOP for step in plan.steps)


def plan_supports_workflow_running_recovery(plan_snapshot: Any) -> bool:
    """True for non-LOOP DAGs or runtime-supported flat FOR_EACH/WHILE Plans."""
    try:
        plan = ExecutionPlanV1.model_validate(plan_snapshot)
    except Exception:
        return False
    if plan_has_loop(plan):
        return plan_supports_flat_loop_running_recovery(plan)
    for step in plan.steps:
        if str(step.type) not in _NON_LOOP_STEP_TYPES:
            return False
    return True


async def assert_workflow_schedule_recovery_source_lineage(
    session: AsyncSession,
    execution: Execution,
) -> None:
    """Fail-closed source lineage for Workflow/Schedule RUNNING recovery.

    Does not create or rebind ScheduleOccurrence rows. Reuses the existing
    ScheduleOccurrence lineage contract for SCHEDULE_OCCURRENCE.
    """
    if execution.source_type == ExecutionSourceType.WORKFLOW_VERSION.value:
        if (
            execution.workflow_version_id is None
            or execution.schedule_occurrence_id is not None
            or execution.agent_request_id is not None
            or execution.agent_version_id is not None
            or execution.plan_validation_run_id is not None
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="WORKFLOW_VERSION Execution source lineage is inconsistent.",
                status_code=409,
            )
        return

    if execution.source_type == ExecutionSourceType.SCHEDULE_OCCURRENCE.value:
        try:
            await _assert_schedule_occurrence_lineage(session, execution)
        except AppError as exc:
            if exc.code in {
                "RESOURCE_CONFLICT",
                "EXECUTION_PRECONDITION_FAILED",
            }:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=exc.message,
                    status_code=409,
                ) from exc
            raise
        return

    raise AppError(
        code="RESOURCE_CONFLICT",
        message=(
            f"Unsupported source_type for Workflow RUNNING recovery: "
            f"{execution.source_type!r}."
        ),
        status_code=409,
    )


def _tool_policy_risk_and_attempts(policy_snapshot: dict[str, Any]) -> tuple[str, int]:
    """Extract risk_class/max_attempts from Agent-shaped or nested safe snapshot."""
    tool_policy = policy_snapshot.get("tool_policy")
    if isinstance(tool_policy, dict):
        risk_class = tool_policy.get("risk_class")
        max_attempts = tool_policy.get("max_attempts")
    else:
        risk_class = policy_snapshot.get("risk_class")
        max_attempts = policy_snapshot.get("max_attempts")
    try:
        RiskClass(risk_class)
    except (TypeError, ValueError) as exc:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Pinned ToolPolicy risk_class is invalid.",
            status_code=409,
        ) from exc
    if (
        not isinstance(max_attempts, int)
        or isinstance(max_attempts, bool)
        or max_attempts < 1
    ):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Pinned ToolPolicy max_attempts is invalid.",
            status_code=409,
        )
    return str(risk_class), max_attempts


class WorkflowDagRecovery:
    """Classify + apply expired RUNNING Workflow/Schedule DAG recovery."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        lease_seconds: int,
        assign_lease,
        clear_lease,
        terminalize_orphan,
    ) -> None:
        self._session = session
        self._lease_seconds = lease_seconds
        self._executions = ExecutionRepository(session)
        self._assign_lease = assign_lease
        self._clear_lease = clear_lease
        self._terminalize_orphan = terminalize_orphan

    async def recover(
        self,
        *,
        execution: Execution,
        worker_id: str,
        now: datetime,
    ) -> WorkflowDagRecoveryResult:
        steps = await self._executions.list_steps(execution.id)
        for step in sorted(steps, key=lambda s: s.step_key):
            await self._executions.lock_step(step.id)
        steps = await self._executions.list_steps(execution.id)
        steps_by_id = {s.id: s for s in steps}

        try:
            await assert_workflow_schedule_recovery_source_lineage(
                self._session, execution
            )
            plan = assert_execution_plan_lineage(execution)
            ownership = None
            if plan_has_loop(plan):
                ownership = assert_loop_recovery_ownership(plan)
                assert_execution_loop_recovery_checkpoints(
                    execution=execution,
                    plan=plan,
                    steps=steps,
                    ownership=ownership,
                )
            else:
                for step in steps:
                    if step.step_type not in _NON_LOOP_STEP_TYPES:
                        raise AppError(
                            code="RESOURCE_CONFLICT",
                            message=(
                                f"Unsupported Step type {step.step_type!r} for "
                                "Workflow RUNNING recovery."
                            ),
                            status_code=409,
                        )
            for step in steps:
                if step.step_type not in _SUPPORTED_STEP_TYPES:
                    raise AppError(
                        code="RESOURCE_CONFLICT",
                        message=(
                            f"Unsupported Step type {step.step_type!r} for "
                            "Workflow RUNNING recovery."
                        ),
                        status_code=409,
                    )
                if step.status in _LEASE_FREE_STEP:
                    raise AppError(
                        code="RESOURCE_CONFLICT",
                        message=(
                            f"Step {step.step_key!r} is {step.status}; "
                            "lease-free waits are not RUNNING recovery candidates."
                        ),
                        status_code=409,
                    )

            plan_by_id = {s.id: s for s in plan.steps}
            classifications: list[ToolStepClassification] = []
            for step in sorted(steps, key=lambda s: s.step_key):
                classifications.append(
                    await self._classify_step(
                        execution=execution,
                        step=step,
                        steps=steps,
                        plan=plan,
                        plan_by_id=plan_by_id,
                    )
                )
        except AppError as exc:
            if exc.code not in {
                "RESOURCE_CONFLICT",
                "EXECUTION_PRECONDITION_FAILED",
            }:
                raise
            await self._fail_closed(
                execution=execution,
                steps=list(steps_by_id.values()),
                now=now,
                error_code=_ERROR_CODE_INCONSISTENT,
                message=exc.message,
                upgrade_started_tool_calls=True,
            )
            await self._session.flush()
            return WorkflowDagRecoveryResult(
                decision="FAIL_INCONSISTENT",
                taken_over=False,
                invoke_runner=False,
                worker_id=None,
                lease_token=None,
                reason=exc.message,
            )

        fatal = [c for c in classifications if c.kind == ToolStepRecoveryKind.FATAL_UNKNOWN]
        if fatal:
            await self._fail_closed(
                execution=execution,
                steps=list(steps_by_id.values()),
                now=now,
                error_code=_ERROR_CODE_LEASE_EXPIRED,
                message=(
                    fatal[0].message
                    or "Unsafe or UNKNOWN ToolCall evidence blocks DAG recovery."
                ),
                upgrade_started_tool_calls=True,
                preferred_unknown_step_ids={c.step_id for c in fatal},
            )
            await self._session.flush()
            return WorkflowDagRecoveryResult(
                decision="UNKNOWN_OUTCOME",
                taken_over=True,
                invoke_runner=False,
                worker_id=None,
                lease_token=None,
                reason="WORKFLOW_DAG_FATAL_UNKNOWN",
            )

        # Apply non-fatal mutations before lease (still MCP 0).
        # If any exhausted Step uses FAIL_EXECUTION, do not apply sibling SAFE_RETRY
        # first — fail-fast with MCP 0 after terminalizing exhausted evidence only.
        exhausted_fail_execution = False
        for c in classifications:
            if c.kind != ToolStepRecoveryKind.FAIL_EXHAUSTED:
                continue
            on_error = c.on_error or "FAIL_EXECUTION"
            if on_error == "FAIL_EXECUTION" or not is_continuable_known_failure(
                on_error=on_error, step_status=StepStatus.FAILED.value
            ):
                exhausted_fail_execution = True
                break

        if exhausted_fail_execution:
            for c in classifications:
                if c.kind != ToolStepRecoveryKind.FAIL_EXHAUSTED:
                    continue
                await self._apply_fail_exhausted(
                    step=steps_by_id[c.step_id], classification=c, now=now
                )

        if exhausted_fail_execution:
            by_key = {s.step_key: s for s in await self._executions.list_steps(execution.id)}
            skip_all_pending(by_key=by_key, now=now)
            cancel_unused_ready_tools(by_key=by_key, now=now)
            from app.execution.events import ExecutionEventWriter

            previous_exec = execution.status
            execution.status = ExecutionStatus.FAILED.value
            execution.error_code = _ERROR_CODE_LEASE_EXPIRED
            execution.error_message = (
                "Worker lease expired with started ToolCall evidence; "
                "max_attempts exhausted so remote call is not retried."
            )
            execution.finished_at = now
            self._clear_lease(execution)
            execution.lock_version += 1
            await ExecutionEventWriter(self._session).emit_execution_status_changed(
                execution, previous_status=previous_exec, occurred_at=now
            )
            await self._session.flush()
            return WorkflowDagRecoveryResult(
                decision="FAIL_EXHAUSTED",
                taken_over=True,
                invoke_runner=False,
                worker_id=None,
                lease_token=None,
                reason="WORKFLOW_DAG_FAIL_EXHAUSTED",
            )

        # Continuable exhausted + safe retries: mutate all evidence, then one lease.
        for c in classifications:
            step = steps_by_id[c.step_id]
            if c.kind == ToolStepRecoveryKind.SAFE_RETRY:
                await self._apply_safe_retry(step=step, classification=c, now=now)
            elif c.kind == ToolStepRecoveryKind.FAIL_EXHAUSTED:
                await self._apply_fail_exhausted(step=step, classification=c, now=now)

        token = self._assign_lease(execution, worker_id=worker_id, now=now)
        # Transfer RESUME Attempt ownership onto the fresh Execution lease.
        for c in classifications:
            if c.kind != ToolStepRecoveryKind.RESUME or c.attempt_id is None:
                continue
            attempt = await self._executions.get_attempt_with_lock(c.attempt_id)
            if attempt is None or attempt.status != StepAttemptStatus.STARTED.value:
                continue
            attempt.worker_id = worker_id
            attempt.lease_expires_at = execution.lease_expires_at

        await self._session.flush()
        has_safe_retry = any(
            c.kind == ToolStepRecoveryKind.SAFE_RETRY for c in classifications
        )
        has_resume = any(c.kind == ToolStepRecoveryKind.RESUME for c in classifications)
        if has_safe_retry:
            decision = "SAFE_RETRY"
            reason = "WORKFLOW_DAG_SAFE_RETRY"
        elif has_resume:
            decision = "RESUME_ATTEMPT"
            reason = "WORKFLOW_DAG_RESUME"
        else:
            decision = "TAKEOVER_READY"
            reason = "WORKFLOW_DAG_TAKEOVER"
        return WorkflowDagRecoveryResult(
            decision=decision,
            taken_over=True,
            invoke_runner=True,
            worker_id=worker_id,
            lease_token=token,
            reason=reason,
        )

    async def _classify_step(
        self,
        *,
        execution: Execution,
        step: ExecutionStep,
        steps: list[ExecutionStep],
        plan: ExecutionPlanV1,
        plan_by_id: dict[str, Any],
    ) -> ToolStepClassification:
        if step.step_type != AuthorableStepType.TOOL.value:
            if step.status == StepStatus.UNKNOWN_OUTCOME.value:
                return ToolStepClassification(
                    step_id=step.id,
                    kind=ToolStepRecoveryKind.FATAL_UNKNOWN,
                    message=(
                        f"Durable UNKNOWN_OUTCOME on local Step {step.step_key!r} "
                        "blocks clean DAG resume."
                    ),
                )
            if step.status in _STEP_TERMINAL:
                return ToolStepClassification(
                    step_id=step.id, kind=ToolStepRecoveryKind.TERMINAL_OK
                )
            return ToolStepClassification(
                step_id=step.id, kind=ToolStepRecoveryKind.LOCAL
            )

        if step.status == StepStatus.UNKNOWN_OUTCOME.value:
            return ToolStepClassification(
                step_id=step.id,
                kind=ToolStepRecoveryKind.FATAL_UNKNOWN,
                message=(
                    f"Durable UNKNOWN_OUTCOME on TOOL Step {step.step_key!r} "
                    "is never converted to READY/retry."
                ),
            )
        if step.status in _STEP_TERMINAL:
            # Child UNKNOWN under terminal Step still fails closed.
            if await self._step_has_unknown_child(step.id):
                return ToolStepClassification(
                    step_id=step.id,
                    kind=ToolStepRecoveryKind.FATAL_UNKNOWN,
                    message=(
                        f"UNKNOWN_OUTCOME child evidence under TOOL Step "
                        f"{step.step_key!r} blocks clean cancellation/resume."
                    ),
                )
            return ToolStepClassification(
                step_id=step.id, kind=ToolStepRecoveryKind.TERMINAL_OK
            )

        # Downstream PENDING TOOL Steps are inactive until wave promotion.
        # Leave them untouched after whole-DAG validation (no MCP).
        if step.status == StepStatus.PENDING.value:
            if await self._step_has_unknown_child(step.id):
                return ToolStepClassification(
                    step_id=step.id,
                    kind=ToolStepRecoveryKind.FATAL_UNKNOWN,
                    message=(
                        f"UNKNOWN_OUTCOME child evidence under PENDING TOOL "
                        f"Step {step.step_key!r}."
                    ),
                )
            attempts = await self._executions.list_attempts(step.id)
            if any(a.status == StepAttemptStatus.STARTED.value for a in attempts):
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        f"PENDING TOOL Step {step.step_key!r} has STARTED Attempt."
                    ),
                    status_code=409,
                )
            for attempt in attempts:
                tool_calls = await self._executions.list_tool_calls(attempt.id)
                if any(
                    tc.normalized_status == ToolCallNormalizedStatus.STARTED.value
                    for tc in tool_calls
                ):
                    raise AppError(
                        code="RESOURCE_CONFLICT",
                        message=(
                            f"PENDING TOOL Step {step.step_key!r} has "
                            "STARTED ToolCall evidence."
                        ),
                        status_code=409,
                    )
            return ToolStepClassification(
                step_id=step.id, kind=ToolStepRecoveryKind.LOCAL
            )

        plan_step = runtime_plan_step(step, plan_by_id)
        on_error = plan_step.on_error
        lineage = assert_tool_step_lineage(execution, step, steps=steps)
        policy_snapshot = get_expected_tool_policy_snapshot(
            execution,
            plan_step_id=lineage.plan_step.id,
            tool_version_id=lineage.tool_version_id,
        )
        risk_class, max_attempts = _tool_policy_risk_and_attempts(policy_snapshot)

        attempts = await self._executions.list_attempts(step.id)
        started = [a for a in attempts if a.status == StepAttemptStatus.STARTED.value]

        if step.status == StepStatus.READY.value:
            if started:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=f"READY TOOL Step {step.step_key!r} has STARTED Attempt.",
                    status_code=409,
                )
            for attempt in attempts:
                if attempt.status not in _ATTEMPT_TERMINAL:
                    raise AppError(
                        code="RESOURCE_CONFLICT",
                        message=(
                            f"READY TOOL Step {step.step_key!r} has "
                            "non-terminal Attempt."
                        ),
                        status_code=409,
                    )
                tool_calls = await self._executions.list_tool_calls(attempt.id)
                if any(
                    tc.normalized_status == ToolCallNormalizedStatus.STARTED.value
                    for tc in tool_calls
                ):
                    raise AppError(
                        code="RESOURCE_CONFLICT",
                        message=(
                            f"READY TOOL Step {step.step_key!r} has "
                            "STARTED ToolCall evidence."
                        ),
                        status_code=409,
                    )
                if any(
                    tc.normalized_status
                    == ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value
                    for tc in tool_calls
                ):
                    return ToolStepClassification(
                        step_id=step.id,
                        kind=ToolStepRecoveryKind.FATAL_UNKNOWN,
                        message=(
                            f"UNKNOWN_OUTCOME ToolCall under READY Step "
                            f"{step.step_key!r}."
                        ),
                    )
            return ToolStepClassification(
                step_id=step.id, kind=ToolStepRecoveryKind.READY, on_error=on_error
            )

        if step.status != StepStatus.RUNNING.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"TOOL Step {step.step_key!r} status {step.status!r} "
                    "is not recoverable."
                ),
                status_code=409,
            )

        if len(started) == 0:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"RUNNING TOOL Step {step.step_key!r} has no STARTED Attempt.",
                status_code=409,
            )
        if len(started) > 1:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"RUNNING TOOL Step {step.step_key!r} has multiple "
                    "STARTED Attempts."
                ),
                status_code=409,
            )
        attempt = started[0]
        tool_calls = await self._executions.list_tool_calls(attempt.id)
        started_calls = [
            tc
            for tc in tool_calls
            if tc.normalized_status == ToolCallNormalizedStatus.STARTED.value
        ]
        if any(
            tc.normalized_status == ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value
            for tc in tool_calls
        ):
            return ToolStepClassification(
                step_id=step.id,
                kind=ToolStepRecoveryKind.FATAL_UNKNOWN,
                attempt_id=attempt.id,
                message=(
                    f"UNKNOWN_OUTCOME ToolCall under RUNNING Step "
                    f"{step.step_key!r}."
                ),
            )
        if len(started_calls) > 1:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"STARTED Attempt on Step {step.step_key!r} has multiple "
                    "STARTED ToolCalls."
                ),
                status_code=409,
            )
        if len(started_calls) == 0:
            if tool_calls:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        f"Attempt on Step {step.step_key!r} has non-STARTED "
                        "ToolCall evidence only."
                    ),
                    status_code=409,
                )
            assert_resume_attempt_lineage(
                execution=execution,
                step=step,
                attempt=attempt,
                steps=steps,
                worker_id=None,
            )
            return ToolStepClassification(
                step_id=step.id,
                kind=ToolStepRecoveryKind.RESUME,
                on_error=on_error,
                attempt_id=attempt.id,
            )

        tool_call = started_calls[0]
        if tool_call.mcp_tool_version_id != step.mcp_tool_version_id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"ToolCall ToolVersion lineage mismatch on Step "
                    f"{step.step_key!r}."
                ),
                status_code=409,
            )
        if _is_unsafe_ambiguous_risk(risk_class):
            return ToolStepClassification(
                step_id=step.id,
                kind=ToolStepRecoveryKind.FATAL_UNKNOWN,
                on_error=on_error,
                attempt_id=attempt.id,
                tool_call_id=tool_call.id,
                message=(
                    f"Unsafe STARTED ToolCall on Step {step.step_key!r} "
                    f"(risk_class={risk_class})."
                ),
            )
        if _is_safe_retry_risk(risk_class):
            if _can_safe_retry(
                attempt_count=step.attempt_count, max_attempts=max_attempts
            ):
                return ToolStepClassification(
                    step_id=step.id,
                    kind=ToolStepRecoveryKind.SAFE_RETRY,
                    on_error=on_error,
                    attempt_id=attempt.id,
                    tool_call_id=tool_call.id,
                )
            return ToolStepClassification(
                step_id=step.id,
                kind=ToolStepRecoveryKind.FAIL_EXHAUSTED,
                on_error=on_error,
                attempt_id=attempt.id,
                tool_call_id=tool_call.id,
            )
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=f"Unsupported risk_class for recovery: {risk_class}.",
            status_code=409,
        )

    async def _step_has_unknown_child(self, step_id: uuid.UUID) -> bool:
        attempts = await self._executions.list_attempts(step_id)
        for attempt in attempts:
            if attempt.status == StepAttemptStatus.UNKNOWN_OUTCOME.value:
                return True
            tool_calls = await self._executions.list_tool_calls(attempt.id)
            if any(
                tc.normalized_status == ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value
                for tc in tool_calls
            ):
                return True
        return False

    async def _apply_safe_retry(
        self,
        *,
        step: ExecutionStep,
        classification: ToolStepClassification,
        now: datetime,
    ) -> None:
        assert classification.attempt_id is not None
        assert classification.tool_call_id is not None
        attempt = await self._executions.get_attempt_with_lock(classification.attempt_id)
        tool_call = await self._executions.get_tool_call_with_lock(
            classification.tool_call_id
        )
        assert attempt is not None and tool_call is not None
        self._terminalize_orphan(
            attempt=attempt,
            tool_call=tool_call,
            now=now,
            terminal=StepAttemptStatus.FAILED.value,
            is_retryable=True,
            message=(
                "Worker lease expired after tools/call evidence was started; "
                "safe retry class allows a new Attempt."
            ),
        )
        from app.execution.events import ExecutionEventWriter

        previous_step = step.status
        step.status = StepStatus.READY.value
        step.error_code = None
        step.error_message = None
        step.finished_at = None
        step.lock_version += 1
        await ExecutionEventWriter(self._session).emit_step_status_changed(
            step, previous_status=previous_step, occurred_at=now
        )

    async def _apply_fail_exhausted(
        self,
        *,
        step: ExecutionStep,
        classification: ToolStepClassification,
        now: datetime,
    ) -> None:
        assert classification.attempt_id is not None
        assert classification.tool_call_id is not None
        attempt = await self._executions.get_attempt_with_lock(classification.attempt_id)
        tool_call = await self._executions.get_tool_call_with_lock(
            classification.tool_call_id
        )
        assert attempt is not None and tool_call is not None
        self._terminalize_orphan(
            attempt=attempt,
            tool_call=tool_call,
            now=now,
            terminal=StepAttemptStatus.FAILED.value,
            is_retryable=False,
            message=(
                "Worker lease expired after tools/call evidence was started; "
                "max_attempts exhausted."
            ),
        )
        from app.execution.events import ExecutionEventWriter

        previous_step = step.status
        step.status = StepStatus.FAILED.value
        step.error_code = _ERROR_CODE_LEASE_EXPIRED
        step.error_message = (
            "Worker lease expired with started ToolCall evidence; "
            "max_attempts exhausted so remote call is not retried."
        )
        step.finished_at = now
        step.lock_version += 1
        await ExecutionEventWriter(self._session).emit_step_status_changed(
            step, previous_status=previous_step, occurred_at=now
        )

    async def _fail_closed(
        self,
        *,
        execution: Execution,
        steps: list[ExecutionStep],
        now: datetime,
        error_code: str,
        message: str,
        upgrade_started_tool_calls: bool,
        preferred_unknown_step_ids: set[uuid.UUID] | None = None,
    ) -> None:
        preferred_unknown_step_ids = preferred_unknown_step_ids or set()
        started_pairs: list[tuple[StepAttempt, ToolCall]] = []
        if upgrade_started_tool_calls:
            for step_row in steps:
                attempts = await self._executions.list_attempts(step_row.id)
                for attempt_row in attempts:
                    tool_calls = await self._executions.list_tool_calls(attempt_row.id)
                    for tc in tool_calls:
                        if (
                            tc.normalized_status
                            == ToolCallNormalizedStatus.STARTED.value
                        ):
                            started_pairs.append((attempt_row, tc))

        for attempt_row, tool_call_row in started_pairs:
            locked_attempt = await self._executions.get_attempt_with_lock(
                attempt_row.id
            )
            if locked_attempt is not None:
                locked_attempt.status = StepAttemptStatus.UNKNOWN_OUTCOME.value
                locked_attempt.error_layer = _ERROR_LAYER
                locked_attempt.error_code = error_code
                locked_attempt.error_message = message
                locked_attempt.is_retryable = False
                if locked_attempt.finished_at is None:
                    locked_attempt.finished_at = now
                locked_attempt.worker_id = None
                locked_attempt.lease_expires_at = None
            locked_tc = await self._executions.get_tool_call_with_lock(tool_call_row.id)
            if locked_tc is not None:
                locked_tc.normalized_status = (
                    ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value
                )
                locked_tc.finished_at = now

        for step_row in steps:
            if step_row.status in _STEP_TERMINAL:
                if (
                    step_row.status != StepStatus.UNKNOWN_OUTCOME.value
                    and (
                        step_row.id in preferred_unknown_step_ids
                        or await self._step_has_unknown_child(step_row.id)
                    )
                ):
                    locked = await self._executions.lock_step(step_row.id)
                    if (
                        locked is not None
                        and locked.status != StepStatus.UNKNOWN_OUTCOME.value
                    ):
                        previous = locked.status
                        locked.status = StepStatus.UNKNOWN_OUTCOME.value
                        locked.error_code = error_code
                        locked.error_message = message
                        if locked.finished_at is None:
                            locked.finished_at = now
                        locked.lock_version += 1
                        from app.execution.events import ExecutionEventWriter

                        await ExecutionEventWriter(
                            self._session
                        ).emit_step_status_changed(
                            locked, previous_status=previous, occurred_at=now
                        )
                continue
            locked = await self._executions.lock_step(step_row.id)
            if locked is None or locked.status in _STEP_TERMINAL:
                continue
            # Prefer UNKNOWN when this step had STARTED ToolCall or was classified fatal.
            use_unknown = (
                locked.id in preferred_unknown_step_ids
                or any(
                    a.step_execution_id == locked.id
                    for a, _tc in started_pairs
                )
                or await self._step_has_unknown_child(locked.id)
            )
            previous = locked.status
            locked.status = (
                StepStatus.UNKNOWN_OUTCOME.value
                if use_unknown
                else StepStatus.FAILED.value
            )
            locked.error_code = error_code
            locked.error_message = message
            locked.finished_at = now
            locked.lock_version += 1
            from app.execution.events import ExecutionEventWriter

            await ExecutionEventWriter(self._session).emit_step_status_changed(
                locked, previous_status=previous, occurred_at=now
            )

        by_key = {s.step_key: s for s in await self._executions.list_steps(execution.id)}
        skip_all_pending(by_key=by_key, now=now)
        cancel_unused_ready_tools(by_key=by_key, now=now)

        from app.execution.events import ExecutionEventWriter

        previous_exec = execution.status
        execution.status = ExecutionStatus.FAILED.value
        execution.error_code = error_code
        execution.error_message = message
        execution.finished_at = now
        self._clear_lease(execution)
        execution.lock_version += 1
        await ExecutionEventWriter(self._session).emit_execution_status_changed(
            execution, previous_status=previous_exec, occurred_at=now
        )
