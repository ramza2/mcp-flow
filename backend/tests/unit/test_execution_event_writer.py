"""Unit tests for ExecutionEventWriter lifecycle mapping / idempotency."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from app.domain.enums import (
    ExecutionEventVisibility,
    ExecutionStatus,
    StepStatus,
)
from app.execution.events import (
    EXECUTION_STATUS_EVENT_TYPES,
    STEP_STATUS_EVENT_TYPES,
    ExecutionEventWriter,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.helpers.execution_ops import seed_execution


@pytest.mark.asyncio
async def test_writer_maps_execution_and_step_statuses(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session:
        from tests.helpers.execution_ops import seed_user

        user_id = await seed_user(session)
        execution = await seed_execution(
            session,
            requester_id=user_id,
            status=ExecutionStatus.CREATED.value,
        )
        from app.repositories.execution import ExecutionRepository

        step = await ExecutionRepository(session).create_step(
            execution_id=execution.id,
            step_key="s1",
            step_type="TOOL",
            mcp_tool_version_id=None,
            parent_step_id=None,
            sequence_hint=1,
            status=StepStatus.READY.value,
            step_snapshot={"id": "s1", "type": "TOOL"},
            lock_version=1,
        )

        writer = ExecutionEventWriter(session)
        created = await writer.emit_execution_status_changed(
            execution, previous_status=None
        )
        assert created is not None
        assert created.event_type == "execution.created"
        assert created.visibility == ExecutionEventVisibility.USER.value
        assert created.payload == {
            "execution_id": str(execution.id),
            "status": ExecutionStatus.CREATED.value,
        }

        # Idempotent: same status → no event
        assert (
            await writer.emit_execution_status_changed(
                execution, previous_status=execution.status
            )
            is None
        )

        previous = execution.status
        execution.status = ExecutionStatus.QUEUED.value
        queued = await writer.emit_execution_status_changed(
            execution, previous_status=previous
        )
        assert queued is not None
        assert queued.event_type == "execution.queued"

        ready = await writer.emit_step_status_changed(step, previous_status=None)
        assert ready is not None
        assert ready.event_type == "execution.step.ready"

        # Unmapped step statuses emit nothing
        step.status = StepStatus.TIMED_OUT.value
        assert (
            await writer.emit_step_status_changed(
                step, previous_status=StepStatus.READY.value
            )
            is None
        )
        step.status = StepStatus.CANCELLED.value
        assert (
            await writer.emit_step_status_changed(
                step, previous_status=StepStatus.READY.value
            )
            is None
        )
        step.status = StepStatus.UNKNOWN_OUTCOME.value
        assert (
            await writer.emit_step_status_changed(
                step, previous_status=StepStatus.READY.value
            )
            is None
        )
        await session.commit()


def test_catalog_covers_required_lifecycle() -> None:
    assert set(EXECUTION_STATUS_EVENT_TYPES) == {
        "CREATED",
        "QUEUED",
        "RUNNING",
        "WAITING_INPUT",
        "WAITING_APPROVAL",
        "CANCEL_REQUESTED",
        "SUCCEEDED",
        "PARTIALLY_SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "TIMED_OUT",
    }
    assert set(STEP_STATUS_EVENT_TYPES) == {
        "READY",
        "RUNNING",
        "WAITING_INPUT",
        "WAITING_APPROVAL",
        "SUCCEEDED",
        "FAILED",
        "SKIPPED",
    }
    assert "execution.step.progress" not in STEP_STATUS_EVENT_TYPES.values()
    assert "execution.step.retrying" not in STEP_STATUS_EVENT_TYPES.values()


@pytest.mark.asyncio
async def test_approval_events_minimal_payload(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session:
        from tests.helpers.execution_ops import seed_user

        user_id = await seed_user(session)
        execution = await seed_execution(session, requester_id=user_id)
        step_id = uuid.uuid4()
        req_id = uuid.uuid4()
        writer = ExecutionEventWriter(session)
        requested = await writer.emit_approval_requested(
            execution_id=execution.id,
            step_execution_id=step_id,
            approval_request_id=req_id,
            status="PENDING",
            occurred_at=datetime.now(UTC),
        )
        assert requested.event_type == "approval.requested"
        assert set(requested.payload) <= {
            "execution_id",
            "step_execution_id",
            "approval_request_id",
            "status",
            "decision",
        }
        assert "context_snapshot" not in requested.payload
        decided = await writer.emit_approval_decided(
            execution_id=execution.id,
            step_execution_id=step_id,
            approval_request_id=req_id,
            status="APPROVED",
            decision="APPROVE",
        )
        assert decided.event_type == "approval.decided"
        assert decided.payload["decision"] == "APPROVE"
        await session.commit()
