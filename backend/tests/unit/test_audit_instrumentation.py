"""Unit tests for Execution create/cancel Audit instrumentation."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.writer import ACTION_EXECUTION_CANCEL, ACTION_EXECUTION_CREATE
from app.domain.enums import AuditActorType, AuditResult, ExecutionStatus
from app.models.audit import AuditEvent
from app.repositories.execution import ExecutionRepository
from app.services.execution_cancellation import ExecutionCancellationService
from app.services.execution_creation import ExecutionCreationService

from tests.unit.test_execution_cancellation import _grant_execution_cancel
from tests.unit.test_execution_creation import _create, _idem_key, _seed_ready


@pytest.mark.asyncio
async def test_agent_request_execution_create_audits_once(
    db_session: AsyncSession,
) -> None:
    ctx = await _seed_ready(db_session)
    key = _idem_key()
    first = await ExecutionCreationService(db_session).create_from_agent_request(
        agent_request_id=ctx["request_id"],
        requester_id=ctx["requester_id"],
        idempotency_key=key,
    )
    assert first.replayed is False
    exec_id = first.result.id

    replay = await ExecutionCreationService(db_session).create_from_agent_request(
        agent_request_id=ctx["request_id"],
        requester_id=ctx["requester_id"],
        idempotency_key=key,
    )
    assert replay.replayed is True
    assert replay.result.id == exec_id

    rows = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.action == ACTION_EXECUTION_CREATE,
                AuditEvent.execution_id == exec_id,
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_type == AuditActorType.USER.value
    assert row.actor_id == str(ctx["requester_id"])
    assert row.result == AuditResult.SUCCESS.value
    assert row.change_set is not None
    assert row.change_set["source_type"] == "AGENT_REQUEST"
    assert "input_snapshot" not in row.change_set
    assert "plan_snapshot" not in row.change_set
    assert "policy_snapshot" not in row.change_set


@pytest.mark.asyncio
async def test_cancel_audits_first_only(
    db_session: AsyncSession,
) -> None:
    seeded = await _seed_ready(db_session)
    outcome = await _create(db_session, seeded, idempotency_key=_idem_key())
    execution_id = outcome.result.id
    requester_id = seeded["requester_id"]
    await _grant_execution_cancel(db_session, requester_id)

    svc = ExecutionCancellationService(db_session)
    first = await svc.request_user_cancel(
        execution_id, actor_user_id=requester_id, reason="bye"
    )
    assert first.status == ExecutionStatus.CANCELLED.value

    second = await svc.request_user_cancel(
        execution_id, actor_user_id=requester_id, reason="again"
    )
    assert second.status == ExecutionStatus.CANCELLED.value

    rows = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.action == ACTION_EXECUTION_CANCEL,
                AuditEvent.execution_id == execution_id,
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.before_data == {"status": ExecutionStatus.CREATED.value}
    assert row.after_data == {"status": ExecutionStatus.CANCELLED.value}
    assert row.reason == "USER_REQUEST"

    # Sanity: execution still terminal
    refreshed = await ExecutionRepository(db_session).get(execution_id)
    assert refreshed is not None
    assert refreshed.status == ExecutionStatus.CANCELLED.value
