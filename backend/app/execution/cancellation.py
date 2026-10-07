"""Cooperative Execution cancellation coordinator (FNC-EXE-010 / REQ-EXE-009).

Assumes the caller holds an Execution row lock. Owns durable cancellation
transitions shared by the user cancel API and future Schedule REPLACE.
No MCP / Celery / Outbox calls.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import (
    ApprovalStatus,
    ExecutionStatus,
    McpInputRequestStatus,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
)
from app.execution.completion import build_result_summary
from app.models.approval import ApprovalRequest
from app.models.execution import Execution, ExecutionStep, StepAttempt, ToolCall
from app.models.mcp_input_request import MCPInputRequest
from app.repositories.execution import ExecutionRepository


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)

_TERMINAL_EXECUTION = frozenset(
    {
        ExecutionStatus.SUCCEEDED.value,
        ExecutionStatus.PARTIALLY_SUCCEEDED.value,
        ExecutionStatus.FAILED.value,
        ExecutionStatus.TIMED_OUT.value,
        ExecutionStatus.CANCELLED.value,
    }
)
_NON_CANCEL_TERMINAL_EXECUTION = frozenset(
    {
        ExecutionStatus.SUCCEEDED.value,
        ExecutionStatus.PARTIALLY_SUCCEEDED.value,
        ExecutionStatus.FAILED.value,
        ExecutionStatus.TIMED_OUT.value,
    }
)
_TERMINAL_STEP = frozenset(
    {
        StepStatus.SUCCEEDED.value,
        StepStatus.FAILED.value,
        StepStatus.TIMED_OUT.value,
        StepStatus.UNKNOWN_OUTCOME.value,
        StepStatus.SKIPPED.value,
        StepStatus.CANCELLED.value,
    }
)
_IMMEDIATE_EXECUTION_STATUSES = frozenset(
    {
        ExecutionStatus.CREATED.value,
        ExecutionStatus.QUEUED.value,
        ExecutionStatus.WAITING_APPROVAL.value,
        ExecutionStatus.WAITING_INPUT.value,
    }
)


class CancellationMode(StrEnum):
    IDEMPOTENT = "IDEMPOTENT"
    IMMEDIATE = "IMMEDIATE"
    REQUESTED = "REQUESTED"
    RECONCILED = "RECONCILED"


@dataclass(frozen=True, slots=True)
class CancellationOutcome:
    status: str
    cancel_requested_at: datetime | None
    finished_at: datetime | None
    mode: CancellationMode


def _outcome(
    execution: Execution,
    *,
    mode: CancellationMode,
) -> CancellationOutcome:
    cancel_at = execution.cancel_requested_at
    finished_at = execution.finished_at
    return CancellationOutcome(
        status=execution.status,
        cancel_requested_at=_as_utc(cancel_at) if cancel_at is not None else None,
        finished_at=_as_utc(finished_at) if finished_at is not None else None,
        mode=mode,
    )


def is_terminal_execution_status(status: str) -> bool:
    return status in _TERMINAL_EXECUTION


def is_terminal_step_status(status: str) -> bool:
    return status in _TERMINAL_STEP


def cancellation_is_requested(execution: Execution) -> bool:
    return (
        execution.status == ExecutionStatus.CANCEL_REQUESTED.value
        or execution.cancel_requested_at is not None
    )


async def list_started_tool_calls(
    session: AsyncSession, execution_id: uuid.UUID
) -> list[ToolCall]:
    stmt = (
        select(ToolCall)
        .join(StepAttempt, ToolCall.step_attempt_id == StepAttempt.id)
        .join(ExecutionStep, StepAttempt.step_execution_id == ExecutionStep.id)
        .where(
            ExecutionStep.execution_id == execution_id,
            ToolCall.normalized_status == ToolCallNormalizedStatus.STARTED.value,
        )
    )
    return list((await session.execute(stmt)).scalars().all())


async def step_has_started_tool_call(
    session: AsyncSession, step_id: uuid.UUID
) -> bool:
    stmt = (
        select(ToolCall.id)
        .join(StepAttempt, ToolCall.step_attempt_id == StepAttempt.id)
        .where(
            StepAttempt.step_execution_id == step_id,
            ToolCall.normalized_status == ToolCallNormalizedStatus.STARTED.value,
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


def _clear_execution_lease(execution: Execution) -> None:
    execution.worker_id = None
    execution.lease_token = None
    execution.lease_expires_at = None
    execution.heartbeat_at = None


def _cancel_step(step: ExecutionStep, *, now: datetime) -> None:
    if step.status in _TERMINAL_STEP:
        return
    step.status = StepStatus.CANCELLED.value
    step.finished_at = now
    step.error_code = None
    step.error_message = None
    step.lock_version = int(step.lock_version) + 1


def _cancel_attempt(attempt: StepAttempt, *, now: datetime) -> None:
    if attempt.status != StepAttemptStatus.STARTED.value:
        return
    attempt.status = StepAttemptStatus.CANCELLED.value
    attempt.finished_at = now
    attempt.worker_id = None
    attempt.lease_expires_at = None
    attempt.error_code = None
    attempt.error_message = None


async def _cancel_started_attempts_without_inflight_tool_call(
    session: AsyncSession,
    step: ExecutionStep,
    *,
    now: datetime,
) -> None:
    """Cancel STARTED Attempts that have no in-flight (STARTED) ToolCall.

    Historical SUCCEEDED ToolCalls (e.g. MRTR input_required network round)
    do not block Attempt cancellation.
    """
    executions = ExecutionRepository(session)
    attempts = await executions.list_attempts(step.id)
    for attempt in attempts:
        if attempt.status != StepAttemptStatus.STARTED.value:
            continue
        tool_calls = await executions.list_tool_calls(attempt.id)
        if any(
            tc.normalized_status == ToolCallNormalizedStatus.STARTED.value
            for tc in tool_calls
        ):
            continue
        _cancel_attempt(attempt, now=now)


async def _cancel_pending_approvals(
    session: AsyncSession, execution_id: uuid.UUID, *, now: datetime
) -> None:
    stmt = (
        select(ApprovalRequest)
        .where(
            ApprovalRequest.execution_id == execution_id,
            ApprovalRequest.status == ApprovalStatus.PENDING.value,
        )
        .with_for_update()
    )
    rows = list((await session.execute(stmt)).scalars().all())
    for row in rows:
        row.status = ApprovalStatus.CANCELLED.value
        row.resolved_at = now
        row.lock_version = int(row.lock_version) + 1


async def _reject_open_mrtr_requests(
    session: AsyncSession,
    execution_id: uuid.UUID,
    *,
    now: datetime,
    answered_by: uuid.UUID | None,
) -> None:
    stmt = (
        select(MCPInputRequest)
        .where(
            MCPInputRequest.execution_id == execution_id,
            MCPInputRequest.status == McpInputRequestStatus.OPEN.value,
        )
        .with_for_update()
    )
    rows = list((await session.execute(stmt)).scalars().all())
    for row in rows:
        row.status = McpInputRequestStatus.REJECTED.value
        row.answered_at = now
        row.answered_by = answered_by
        row.response_payload = None


async def _cancel_safe_nonterminal_steps(
    session: AsyncSession,
    steps: list[ExecutionStep],
    *,
    now: datetime,
    preserve_step_ids: set[uuid.UUID],
) -> None:
    for step in steps:
        if step.id in preserve_step_ids:
            continue
        if step.status in _TERMINAL_STEP:
            continue
        if step.status == StepStatus.RUNNING.value:
            if await step_has_started_tool_call(session, step.id):
                continue
            await _cancel_started_attempts_without_inflight_tool_call(
                session, step, now=now
            )
        elif step.status in {
            StepStatus.WAITING_INPUT.value,
            StepStatus.WAITING_APPROVAL.value,
        }:
            await _cancel_started_attempts_without_inflight_tool_call(
                session, step, now=now
            )
        _cancel_step(step, now=now)


def _set_cancel_metadata_once(
    execution: Execution,
    *,
    now: datetime,
    requested_by: uuid.UUID | None,
    reason: str | None,
) -> None:
    if execution.cancel_requested_at is not None:
        return
    execution.cancel_requested_at = now
    execution.cancel_requested_by = requested_by
    execution.cancel_reason = reason


def _finalize_cancelled(
    execution: Execution,
    steps: list[ExecutionStep],
    *,
    now: datetime,
) -> None:
    execution.status = ExecutionStatus.CANCELLED.value
    execution.finished_at = now
    execution.error_code = None
    execution.error_message = None
    execution.result_summary = build_result_summary(
        status=ExecutionStatus.CANCELLED.value,
        steps=steps,
    )
    _clear_execution_lease(execution)
    execution.lock_version = int(execution.lock_version) + 1


async def settle_input_required_after_cancel_locked(
    session: AsyncSession,
    *,
    execution: Execution,
    step: ExecutionStep,
    attempt: StepAttempt,
    tool_call: ToolCall,
    now: datetime,
    persist_meta: dict,
    response_bytes: int | None,
    first_byte_at: datetime | None,
) -> CancellationOutcome:
    """Persist a truthful completed MRTR network round under CANCEL_REQUESTED.

    Does not create MCPInputRequest / WAITING_INPUT. Caller owns the TX and
    must already hold Execution/Step/Attempt/ToolCall locks.
    """
    tool_call.normalized_status = ToolCallNormalizedStatus.SUCCEEDED.value
    tool_call.response_meta = persist_meta
    tool_call.response_bytes = response_bytes
    tool_call.first_byte_at = first_byte_at
    tool_call.finished_at = now

    if attempt.status == StepAttemptStatus.STARTED.value:
        _cancel_attempt(attempt, now=now)
    _cancel_step(step, now=now)

    if execution.status != ExecutionStatus.CANCEL_REQUESTED.value:
        previous_status = execution.status
        if execution.cancel_requested_at is None:
            execution.cancel_requested_at = now
        execution.status = ExecutionStatus.CANCEL_REQUESTED.value
        execution.lock_version = int(execution.lock_version) + 1
        from app.execution.events import ExecutionEventWriter

        await ExecutionEventWriter(session).emit_execution_status_changed(
            execution, previous_status=previous_status, occurred_at=now
        )

    await session.flush()
    return await reconcile_cancel_requested_locked(session, execution, now=now)


async def apply_cancellation_locked(
    session: AsyncSession,
    execution: Execution,
    *,
    now: datetime,
    requested_by: uuid.UUID | None,
    reason: str | None,
) -> CancellationOutcome:
    """Apply cancellation under an already-locked Execution row.

    Assumes the caller holds the Execution row lock and owns the surrounding
    transaction. This helper does NOT commit, rollback, or open a nested
    transaction — Schedule REPLACE (#56) must compose it inside its own TX.
    """
    if execution.status == ExecutionStatus.CANCELLED.value:
        return _outcome(execution, mode=CancellationMode.IDEMPOTENT)
    if execution.status == ExecutionStatus.CANCEL_REQUESTED.value:
        return _outcome(execution, mode=CancellationMode.IDEMPOTENT)
    if execution.status in _NON_CANCEL_TERMINAL_EXECUTION:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Terminal Execution cannot be cancelled.",
            status_code=409,
        )

    executions = ExecutionRepository(session)
    steps = await executions.list_steps(execution.id)
    # Lock steps deterministically for concurrent cancel/resume races.
    for step in sorted(steps, key=lambda s: str(s.id)):
        await executions.lock_step(step.id)
    steps = await executions.list_steps(execution.id)

    _set_cancel_metadata_once(
        execution, now=now, requested_by=requested_by, reason=reason
    )

    started_calls = await list_started_tool_calls(session, execution.id)
    # Preserve only RUNNING steps that still have an in-flight ToolCall.
    preserve_ids: set[uuid.UUID] = set()
    if started_calls:
        attempt_ids = {c.step_attempt_id for c in started_calls}
        for step in steps:
            if step.status != StepStatus.RUNNING.value:
                continue
            attempts = await executions.list_attempts(step.id)
            if any(a.id in attempt_ids for a in attempts):
                preserve_ids.add(step.id)

    if execution.status in _IMMEDIATE_EXECUTION_STATUSES or (
        execution.status == ExecutionStatus.RUNNING.value and not started_calls
    ):
        await _cancel_pending_approvals(session, execution.id, now=now)
        await _reject_open_mrtr_requests(
            session, execution.id, now=now, answered_by=requested_by
        )
        await _cancel_safe_nonterminal_steps(
            session, steps, now=now, preserve_step_ids=set()
        )
        steps = await executions.list_steps(execution.id)
        previous_status = execution.status
        _finalize_cancelled(execution, steps, now=now)
        from app.execution.events import ExecutionEventWriter

        await ExecutionEventWriter(session).emit_execution_status_changed(
            execution, previous_status=previous_status, occurred_at=now
        )
        return _outcome(execution, mode=CancellationMode.IMMEDIATE)

    # RUNNING with at least one STARTED ToolCall → cooperative CANCEL_REQUESTED.
    await _cancel_pending_approvals(session, execution.id, now=now)
    await _reject_open_mrtr_requests(
        session, execution.id, now=now, answered_by=requested_by
    )
    await _cancel_safe_nonterminal_steps(
        session, steps, now=now, preserve_step_ids=preserve_ids
    )
    previous_status = execution.status
    execution.status = ExecutionStatus.CANCEL_REQUESTED.value
    execution.lock_version = int(execution.lock_version) + 1
    from app.execution.events import ExecutionEventWriter

    await ExecutionEventWriter(session).emit_execution_status_changed(
        execution, previous_status=previous_status, occurred_at=now
    )
    # Preserve lease for the in-flight owner.
    return _outcome(execution, mode=CancellationMode.REQUESTED)


async def reconcile_cancel_requested_locked(
    session: AsyncSession,
    execution: Execution,
    *,
    now: datetime,
) -> CancellationOutcome:
    """If CANCEL_REQUESTED and no STARTED ToolCall remains, terminalize.

    UNKNOWN_OUTCOME / mandatory-fatal Step evidence takes precedence over
    CANCELLED — cancellation must never hide ambiguous external side effects.
    """
    if execution.status == ExecutionStatus.CANCELLED.value:
        return _outcome(execution, mode=CancellationMode.IDEMPOTENT)
    if execution.status == ExecutionStatus.FAILED.value:
        # Prior fatal settle under cancel metadata — idempotent.
        return _outcome(execution, mode=CancellationMode.IDEMPOTENT)
    if execution.status != ExecutionStatus.CANCEL_REQUESTED.value:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Execution is not CANCEL_REQUESTED.",
            status_code=409,
        )
    started = await list_started_tool_calls(session, execution.id)
    if started:
        return _outcome(execution, mode=CancellationMode.REQUESTED)

    executions = ExecutionRepository(session)
    steps = await executions.list_steps(execution.id)
    for step in sorted(steps, key=lambda s: str(s.id)):
        await executions.lock_step(step.id)
    steps = await executions.list_steps(execution.id)

    unknown = [
        s for s in steps if s.status == StepStatus.UNKNOWN_OUTCOME.value
    ]
    if unknown:
        # Preserve cancel metadata; fail closed on ambiguous side effects.
        await _cancel_pending_approvals(session, execution.id, now=now)
        await _reject_open_mrtr_requests(
            session,
            execution.id,
            now=now,
            answered_by=execution.cancel_requested_by,
        )
        await _cancel_safe_nonterminal_steps(
            session, steps, now=now, preserve_step_ids=set()
        )
        steps = await executions.list_steps(execution.id)
        cause = unknown[0]
        previous_status = execution.status
        execution.status = ExecutionStatus.FAILED.value
        execution.finished_at = now
        execution.error_code = cause.error_code
        execution.error_message = cause.error_message
        execution.result_summary = build_result_summary(
            status=ExecutionStatus.FAILED.value,
            steps=steps,
        )
        _clear_execution_lease(execution)
        execution.lock_version = int(execution.lock_version) + 1
        from app.execution.events import ExecutionEventWriter

        await ExecutionEventWriter(session).emit_execution_status_changed(
            execution, previous_status=previous_status, occurred_at=now
        )
        return _outcome(execution, mode=CancellationMode.RECONCILED)

    await _cancel_pending_approvals(session, execution.id, now=now)
    await _reject_open_mrtr_requests(
        session,
        execution.id,
        now=now,
        answered_by=execution.cancel_requested_by,
    )
    await _cancel_safe_nonterminal_steps(
        session, steps, now=now, preserve_step_ids=set()
    )
    steps = await executions.list_steps(execution.id)
    previous_status = execution.status
    _finalize_cancelled(execution, steps, now=now)
    from app.execution.events import ExecutionEventWriter

    await ExecutionEventWriter(session).emit_execution_status_changed(
        execution, previous_status=previous_status, occurred_at=now
    )
    return _outcome(execution, mode=CancellationMode.RECONCILED)


async def cancel_prepared_invocation_before_send(
    session: AsyncSession,
    *,
    execution: Execution,
    step: ExecutionStep,
    attempt: StepAttempt,
    tool_call: ToolCall,
    now: datetime,
) -> CancellationOutcome:
    """B2 gate: cancel prepared Attempt/ToolCall with zero MCP, then reconcile."""
    if tool_call.normalized_status == ToolCallNormalizedStatus.STARTED.value:
        tool_call.normalized_status = ToolCallNormalizedStatus.CANCELLED.value
        tool_call.finished_at = now
    if attempt.status == StepAttemptStatus.STARTED.value:
        _cancel_attempt(attempt, now=now)
    _cancel_step(step, now=now)
    if execution.status != ExecutionStatus.CANCEL_REQUESTED.value:
        # Unexpected RUNNING+cancel_requested_at corruption: fail closed to CANCEL_REQUESTED.
        previous_status = execution.status
        if execution.cancel_requested_at is None:
            execution.cancel_requested_at = now
        execution.status = ExecutionStatus.CANCEL_REQUESTED.value
        execution.lock_version = int(execution.lock_version) + 1
        from app.execution.events import ExecutionEventWriter

        await ExecutionEventWriter(session).emit_execution_status_changed(
            execution, previous_status=previous_status, occurred_at=now
        )
    # Flush so reconcile's STARTED ToolCall query sees this cancellation.
    await session.flush()
    return await reconcile_cancel_requested_locked(session, execution, now=now)
