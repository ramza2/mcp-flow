"""Explicit same-TX ExecutionEvent writer (docs/05 §13.8, docs/06 §16).

Caller owns the transaction — this module never commits.
No SQLAlchemy flush listeners / DB triggers: callers append at status transitions.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    ExecutionEventVisibility,
    ExecutionStatus,
    StepStatus,
)
from app.models.execution import Execution, ExecutionStep
from app.models.execution_event import ExecutionEvent
from app.repositories.execution_event import ExecutionEventRepository

PAYLOAD_VERSION = 1

# Canonical lifecycle → event_type (docs/06 §16). Step TIMED_OUT / CANCELLED /
# UNKNOWN_OUTCOME intentionally unmapped — no invented types this slice.
EXECUTION_STATUS_EVENT_TYPES: dict[str, str] = {
    ExecutionStatus.CREATED.value: "execution.created",
    ExecutionStatus.QUEUED.value: "execution.queued",
    ExecutionStatus.RUNNING.value: "execution.started",
    ExecutionStatus.WAITING_INPUT.value: "execution.waiting_input",
    ExecutionStatus.WAITING_APPROVAL.value: "execution.waiting_approval",
    ExecutionStatus.CANCEL_REQUESTED.value: "execution.cancel_requested",
    ExecutionStatus.SUCCEEDED.value: "execution.succeeded",
    ExecutionStatus.PARTIALLY_SUCCEEDED.value: "execution.partially_succeeded",
    ExecutionStatus.FAILED.value: "execution.failed",
    ExecutionStatus.CANCELLED.value: "execution.cancelled",
    ExecutionStatus.TIMED_OUT.value: "execution.timed_out",
}

STEP_STATUS_EVENT_TYPES: dict[str, str] = {
    StepStatus.READY.value: "execution.step.ready",
    StepStatus.RUNNING.value: "execution.step.started",
    StepStatus.WAITING_INPUT.value: "execution.step.waiting_input",
    StepStatus.WAITING_APPROVAL.value: "execution.step.waiting_approval",
    StepStatus.SUCCEEDED.value: "execution.step.succeeded",
    StepStatus.FAILED.value: "execution.step.failed",
    StepStatus.SKIPPED.value: "execution.step.skipped",
}

# Deferred producers (standard catalog; not emitted this PR):
# - execution.step.progress
# - execution.step.retrying
# - artifact.created


def execution_lifecycle_payload(execution: Execution) -> dict[str, Any]:
    return {
        "execution_id": str(execution.id),
        "status": execution.status,
    }


def step_lifecycle_payload(
    *, execution_id: uuid.UUID, step: ExecutionStep
) -> dict[str, Any]:
    return {
        "execution_id": str(execution_id),
        "step_execution_id": str(step.id),
        "status": step.status,
    }


def approval_lifecycle_payload(
    *,
    execution_id: uuid.UUID,
    step_execution_id: uuid.UUID,
    approval_request_id: uuid.UUID,
    status: str,
    decision: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "execution_id": str(execution_id),
        "step_execution_id": str(step_execution_id),
        "approval_request_id": str(approval_request_id),
        "status": status,
    }
    if decision is not None:
        body["decision"] = decision
    return body


class ExecutionEventWriter:
    """Append sanitized lifecycle events. Does NOT commit."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = ExecutionEventRepository(session)

    async def append(
        self,
        *,
        execution_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
        step_execution_id: uuid.UUID | None = None,
        visibility: ExecutionEventVisibility | str = ExecutionEventVisibility.USER,
        payload_version: int = PAYLOAD_VERSION,
        occurred_at: datetime | None = None,
        event_id: uuid.UUID | None = None,
    ) -> ExecutionEvent:
        event_type_value = str(event_type).strip()
        if not event_type_value or len(event_type_value) > 128:
            raise ValueError("event_type must be 1..128 non-blank characters.")
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object.")
        visibility_value = (
            visibility.value
            if isinstance(visibility, ExecutionEventVisibility)
            else str(visibility)
        )
        if visibility_value not in {
            ExecutionEventVisibility.USER.value,
            ExecutionEventVisibility.OPERATOR.value,
            ExecutionEventVisibility.INTERNAL.value,
        }:
            raise ValueError(f"Invalid visibility: {visibility_value}")
        if payload_version < 1:
            raise ValueError("payload_version must be >= 1.")

        ts = occurred_at or datetime.now(UTC)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        else:
            ts = ts.astimezone(UTC)

        return await self._repo.append(
            event_id=event_id or uuid.uuid4(),
            execution_id=execution_id,
            step_execution_id=step_execution_id,
            event_type=event_type_value,
            visibility=visibility_value,
            payload=dict(payload),
            payload_version=payload_version,
            occurred_at=ts,
        )

    async def emit_execution_status_changed(
        self,
        execution: Execution,
        *,
        previous_status: str | None,
        occurred_at: datetime | None = None,
    ) -> ExecutionEvent | None:
        """Emit USER lifecycle event when Execution status actually changed."""
        current = execution.status
        if previous_status is not None and previous_status == current:
            return None
        event_type = EXECUTION_STATUS_EVENT_TYPES.get(current)
        if event_type is None:
            return None
        return await self.append(
            execution_id=execution.id,
            event_type=event_type,
            payload=execution_lifecycle_payload(execution),
            occurred_at=occurred_at,
        )

    async def emit_step_status_changed(
        self,
        step: ExecutionStep,
        *,
        previous_status: str | None,
        occurred_at: datetime | None = None,
    ) -> ExecutionEvent | None:
        """Emit USER step lifecycle event when Step status actually changed."""
        current = step.status
        if previous_status is not None and previous_status == current:
            return None
        event_type = STEP_STATUS_EVENT_TYPES.get(current)
        if event_type is None:
            return None
        return await self.append(
            execution_id=step.execution_id,
            step_execution_id=step.id,
            event_type=event_type,
            payload=step_lifecycle_payload(
                execution_id=step.execution_id, step=step
            ),
            occurred_at=occurred_at,
        )

    async def emit_approval_requested(
        self,
        *,
        execution_id: uuid.UUID,
        step_execution_id: uuid.UUID,
        approval_request_id: uuid.UUID,
        status: str,
        occurred_at: datetime | None = None,
    ) -> ExecutionEvent:
        return await self.append(
            execution_id=execution_id,
            step_execution_id=step_execution_id,
            event_type="approval.requested",
            payload=approval_lifecycle_payload(
                execution_id=execution_id,
                step_execution_id=step_execution_id,
                approval_request_id=approval_request_id,
                status=status,
            ),
            occurred_at=occurred_at,
        )

    async def emit_approval_decided(
        self,
        *,
        execution_id: uuid.UUID,
        step_execution_id: uuid.UUID,
        approval_request_id: uuid.UUID,
        status: str,
        decision: str | None = None,
        occurred_at: datetime | None = None,
    ) -> ExecutionEvent:
        return await self.append(
            execution_id=execution_id,
            step_execution_id=step_execution_id,
            event_type="approval.decided",
            payload=approval_lifecycle_payload(
                execution_id=execution_id,
                step_execution_id=step_execution_id,
                approval_request_id=approval_request_id,
                status=status,
                decision=decision,
            ),
            occurred_at=occurred_at,
        )


async def emit_execution_status_changed(
    session: AsyncSession,
    execution: Execution,
    *,
    previous_status: str | None,
    occurred_at: datetime | None = None,
) -> ExecutionEvent | None:
    return await ExecutionEventWriter(session).emit_execution_status_changed(
        execution, previous_status=previous_status, occurred_at=occurred_at
    )


async def emit_step_status_changed(
    session: AsyncSession,
    step: ExecutionStep,
    *,
    previous_status: str | None,
    occurred_at: datetime | None = None,
) -> ExecutionEvent | None:
    return await ExecutionEventWriter(session).emit_step_status_changed(
        step, previous_status=previous_status, occurred_at=occurred_at
    )


async def emit_step_transitions(
    session: AsyncSession,
    *,
    previous_by_id: dict[uuid.UUID, str],
    steps: list[ExecutionStep],
    occurred_at: datetime | None = None,
) -> None:
    """Emit step lifecycle events for statuses that actually changed."""
    writer = ExecutionEventWriter(session)
    for step in steps:
        previous = previous_by_id.get(step.id)
        if previous is None:
            continue
        await writer.emit_step_status_changed(
            step, previous_status=previous, occurred_at=occurred_at
        )


def snapshot_step_statuses(steps: list[ExecutionStep]) -> dict[uuid.UUID, str]:
    return {step.id: step.status for step in steps}
