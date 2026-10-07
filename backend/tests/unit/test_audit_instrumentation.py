"""Unit tests for Execution/Workflow/Approval/Schedule Audit instrumentation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.audit.writer import (
    ACTION_APPROVAL_DECISION,
    ACTION_EXECUTION_CANCEL,
    ACTION_EXECUTION_CREATE,
    ACTION_SCHEDULE_TRIGGER,
)
from app.core.errors import AppError
from app.core.middleware import set_request_id
from app.domain.enums import (
    AuditActorType,
    AuditResult,
    ExecutionStatus,
    ScheduleMisfirePolicy,
    ScheduleOverlapPolicy,
)
from app.models.audit import AuditEvent
from app.models.outbox import OutboxEvent
from app.repositories.execution import ExecutionRepository
from app.scheduler.runtime import ScheduleRuntimeService
from app.services.execution_cancellation import ExecutionCancellationService
from app.services.execution_creation import ExecutionCreationService
from app.services.schedule_trigger import ScheduleTriggerService
from app.services.workflow_execution_creation import WorkflowExecutionCreationService
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.test_execution_cancellation import _grant_execution_cancel
from tests.unit.test_execution_creation import _create, _idem_key, _seed_ready
from tests.unit.test_schedule_runtime import _activate_interval_schedule, _ensure_tool_execute
from tests.unit.test_schedule_service import _seed_schedule_manager, _seed_workflow_target
from tests.unit.test_workflow_execution_creation import _seed_ready_workflow


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
async def test_workflow_execution_create_audits_once_with_request_id(
    db_session: AsyncSession,
) -> None:
    from app.schemas.workflow import WorkflowExecutionCreateRequest

    ctx = await _seed_ready_workflow(db_session)
    set_request_id("audit-test-123")
    key = _idem_key()
    first = await WorkflowExecutionCreationService(
        db_session
    ).create_from_workflow_version(
        workflow_id=ctx["workflow_id"],
        version_id=ctx["version_id"],
        requester_id=ctx["requester_id"],
        idempotency_key=key,
        body=WorkflowExecutionCreateRequest(inputs={}),
    )
    assert first.replayed is False
    exec_id = first.result.id

    replay = await WorkflowExecutionCreationService(
        db_session
    ).create_from_workflow_version(
        workflow_id=ctx["workflow_id"],
        version_id=ctx["version_id"],
        requester_id=ctx["requester_id"],
        idempotency_key=key,
        body=WorkflowExecutionCreateRequest(inputs={}),
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
    assert row.resource_type == "EXECUTION"
    assert row.resource_id == str(exec_id)
    assert row.execution_id == exec_id
    assert row.request_id == "audit-test-123"
    assert row.change_set is not None
    assert row.change_set["source_type"] == "WORKFLOW_VERSION"
    assert row.change_set["trigger_type"] == "USER"
    blob = str(row.change_set) + str(row.before_data) + str(row.after_data)
    for forbidden in (
        "plan_snapshot",
        "input_snapshot",
        "policy_snapshot",
        "SECRET_REF",
        "secret_id",
    ):
        assert forbidden not in blob


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

    refreshed = await ExecutionRepository(db_session).get(execution_id)
    assert refreshed is not None
    assert refreshed.status == ExecutionStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_approval_decision_audits_once_on_duplicate(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.approval.decision import ApprovalDecisionService
    from app.domain.enums import ApprovalStatus, StepStatus

    from tests.unit.test_approval_decision_resume import (
        _create_approver,
        _enter_waiting,
    )

    execution_id, approval_id, _requester = await _enter_waiting(
        db_session_factory, monkeypatch, decision_mode="ANY"
    )
    async with db_session_factory() as session:
        actor = await _create_approver(session)
        await session.commit()
        outcome = await ApprovalDecisionService(session).decide(
            approval_id=approval_id,
            actor_user_id=actor,
            decision="APPROVE",
            comment="looks fine with secret password=abc",
        )
        assert outcome.approval_status == ApprovalStatus.APPROVED.value
        assert outcome.resume_enqueued is True
        assert outcome.step_status == StepStatus.WAITING_APPROVAL.value

        with pytest.raises(AppError) as exc:
            await ApprovalDecisionService(session).decide(
                approval_id=approval_id,
                actor_user_id=actor,
                decision="APPROVE",
            )
        assert exc.value.status_code == 409
        assert exc.value.code == "RESOURCE_CONFLICT"

        rows = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.action == ACTION_APPROVAL_DECISION,
                    AuditEvent.resource_id == str(approval_id),
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.actor_type == AuditActorType.USER.value
        assert row.actor_id == str(actor)
        assert row.resource_type == "APPROVAL_REQUEST"
        assert row.execution_id == execution_id
        assert row.change_set is not None
        assert row.change_set["decision"] == "APPROVE"
        assert row.change_set["step_execution_id"]
        blob = str(row.change_set) + str(row.before_data) + str(row.after_data)
        assert "context_snapshot" not in blob
        assert "password=abc" not in blob
        assert "looks fine" not in blob
        assert "secret_id" not in blob
        assert "comment" not in blob

        # Resume Outbox still present exactly once
        events = (
            await session.execute(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == "EXECUTION_APPROVAL_RESUME",
                    OutboxEvent.aggregate_id == execution_id,
                )
            )
        ).scalars().all()
        assert len(events) == 1


@pytest.mark.asyncio
async def test_schedule_trigger_and_replay_audits_once(
    db_session: AsyncSession,
) -> None:
    owner_id = await _seed_schedule_manager(db_session, with_workflow_execute=True)
    _workflow_id, version_id = await _seed_workflow_target(db_session, owner_id)
    await _ensure_tool_execute(db_session, owner_id)
    now = datetime.now(UTC).replace(microsecond=0)
    schedule_id = await _activate_interval_schedule(
        db_session,
        owner_id=owner_id,
        workflow_version_id=version_id,
        misfire=ScheduleMisfirePolicy.SKIP,
        overlap=ScheduleOverlapPolicy.ALLOW,
        next_run_at=now + timedelta(hours=1),
    )
    await db_session.commit()

    key = f"trig-{uuid.uuid4().hex}"
    first = await ScheduleTriggerService(db_session).trigger(
        schedule_id, actor_user_id=owner_id, idempotency_key=key
    )
    assert first.replayed is False
    occ_id = first.occurrence.id
    exec_id = first.execution_id

    replay = await ScheduleTriggerService(db_session).trigger(
        schedule_id, actor_user_id=owner_id, idempotency_key=key
    )
    assert replay.replayed is True
    assert replay.occurrence.id == occ_id
    assert replay.execution_id == exec_id

    trigger_rows = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.action == ACTION_SCHEDULE_TRIGGER,
                AuditEvent.resource_id == str(schedule_id),
            )
        )
    ).scalars().all()
    assert len(trigger_rows) == 1
    row = trigger_rows[0]
    assert row.actor_type == AuditActorType.USER.value
    assert row.actor_id == str(owner_id)
    assert row.resource_type == "SCHEDULE"
    assert row.change_set is not None
    assert row.change_set["occurrence_id"] == str(occ_id)
    assert row.change_set["execution_created"] is (exec_id is not None)
    assert "input_template" not in str(row.change_set)

    if exec_id is not None:
        create_rows = (
            await db_session.execute(
                select(AuditEvent).where(
                    AuditEvent.action == ACTION_EXECUTION_CREATE,
                    AuditEvent.execution_id == exec_id,
                )
            )
        ).scalars().all()
        assert len(create_rows) == 1
        assert create_rows[0].actor_type == AuditActorType.USER.value


@pytest.mark.asyncio
async def test_scheduled_system_execution_create_audits_once(
    db_session: AsyncSession,
) -> None:
    owner_id = await _seed_schedule_manager(db_session, with_workflow_execute=True)
    _workflow_id, version_id = await _seed_workflow_target(db_session, owner_id)
    await _ensure_tool_execute(db_session, owner_id)
    now = datetime.now(UTC).replace(microsecond=0)
    due = now - timedelta(seconds=30)
    schedule_id = await _activate_interval_schedule(
        db_session,
        owner_id=owner_id,
        workflow_version_id=version_id,
        misfire=ScheduleMisfirePolicy.SKIP,
        overlap=ScheduleOverlapPolicy.ALLOW,
        next_run_at=due,
    )
    await db_session.commit()

    runtime = ScheduleRuntimeService(db_session, misfire_grace_seconds=120)
    result = await runtime.process_due_schedule(schedule_id, now=now)
    await db_session.commit()
    assert result.executions_created >= 1

    # Second pass: reconcile / no new due should not duplicate create audits.
    result2 = await runtime.process_due_schedule(schedule_id, now=now)
    await db_session.commit()

    rows = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.action == ACTION_EXECUTION_CREATE,
                AuditEvent.actor_type == AuditActorType.SYSTEM.value,
                AuditEvent.actor_id == "scheduler",
            )
        )
    ).scalars().all()
    assert len(rows) == result.executions_created
    for row in rows:
        assert row.request_id is None
        assert row.trace_id is None
        assert row.execution_id is not None
        assert row.resource_type == "EXECUTION"
        assert row.resource_id == str(row.execution_id)
        assert row.change_set is not None
        assert row.change_set["source_type"] == "SCHEDULE_OCCURRENCE"
        assert row.change_set["trigger_type"] == "SCHEDULE"
    # Second process_due did not add more SYSTEM create events
    assert result2.executions_created == 0
    rows_after = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.action == ACTION_EXECUTION_CREATE,
                AuditEvent.actor_type == AuditActorType.SYSTEM.value,
                AuditEvent.actor_id == "scheduler",
            )
        )
    ).scalars().all()
    assert len(rows_after) == len(rows)
