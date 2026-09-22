"""SQLite create_all must apply PENDING-only partial unique for approval_requests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.domain.enums import ApprovalStatus
from app.models.approval import ApprovalRequest
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.execution import ExecutionRepository
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.unit.test_execution_creation import _create, _idem_key, _seed_ready


def _request(
    *,
    execution_id: uuid.UUID,
    step_id: uuid.UUID,
    approval_id: uuid.UUID,
    requester_id: uuid.UUID,
    status: str,
    now: datetime,
) -> ApprovalRequest:
    return ApprovalRequest(
        execution_id=execution_id,
        step_execution_id=step_id,
        approval_policy_id=approval_id,
        status=status,
        decision_mode="ANY",
        required_approvals=1,
        approval_scope=None,
        context_snapshot={"schema_version": "approval_context.v1"},
        context_hash="a" * 64,
        requested_at=now,
        expires_at=now + timedelta(hours=1),
        resolved_at=None if status == ApprovalStatus.PENDING.value else now,
        requested_by=requester_id,
    )


@pytest.mark.asyncio
async def test_sqlite_pending_partial_unique_allows_history(
    db_session: AsyncSession,
) -> None:
    approval = await ApprovalPolicyRepository(db_session).create(
        code=f"ap-{uuid.uuid4().hex[:8]}",
        name="SQLite Partial Unique",
    )
    await db_session.flush()
    seeded = await _seed_ready(
        db_session,
        policy_requires_approval=True,
        approval_policy_id=approval.id,
    )
    created = await _create(db_session, seeded, idempotency_key=_idem_key())
    await db_session.commit()

    execution_id = created.result.id
    step = (await ExecutionRepository(db_session).list_steps(execution_id))[0]
    now = datetime.now(UTC)
    requester_id = seeded["requester_id"]

    first = _request(
        execution_id=execution_id,
        step_id=step.id,
        approval_id=approval.id,
        requester_id=requester_id,
        status=ApprovalStatus.PENDING.value,
        now=now,
    )
    db_session.add(first)
    await db_session.flush()

    async with db_session.begin_nested():
        duplicate = _request(
            execution_id=execution_id,
            step_id=step.id,
            approval_id=approval.id,
            requester_id=requester_id,
            status=ApprovalStatus.PENDING.value,
            now=now,
        )
        db_session.add(duplicate)
        with pytest.raises(IntegrityError):
            await db_session.flush()

    # Resolve first PENDING → historical APPROVED allows another PENDING later.
    first.status = ApprovalStatus.APPROVED.value
    first.resolved_at = now
    await db_session.flush()

    rejected = _request(
        execution_id=execution_id,
        step_id=step.id,
        approval_id=approval.id,
        requester_id=requester_id,
        status=ApprovalStatus.REJECTED.value,
        now=now,
    )
    db_session.add(rejected)
    await db_session.flush()

    expired = _request(
        execution_id=execution_id,
        step_id=step.id,
        approval_id=approval.id,
        requester_id=requester_id,
        status=ApprovalStatus.EXPIRED.value,
        now=now,
    )
    db_session.add(expired)
    await db_session.flush()

    next_pending = _request(
        execution_id=execution_id,
        step_id=step.id,
        approval_id=approval.id,
        requester_id=requester_id,
        status=ApprovalStatus.PENDING.value,
        now=now,
    )
    db_session.add(next_pending)
    await db_session.flush()
    await db_session.commit()
