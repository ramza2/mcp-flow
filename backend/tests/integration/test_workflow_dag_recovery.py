"""PostgreSQL tests for Issue #115 non-LOOP Workflow/Schedule DAG recovery."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.domain.enums import (
    ExecutionSourceType,
    ExecutionStatus,
    RiskClass,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
)
from app.execution.claim import ExecutionClaimService
from app.execution.queue import ExecutionQueueService
from app.execution.recovery import ExecutionRecoveryService, RecoveryDecision
from app.execution.workflow_dag_recovery import plan_supports_workflow_running_recovery
from app.repositories.execution import ExecutionRepository
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.test_execution_creation import _idem_key, _install_no_side_effects
from tests.unit.test_workflow_dag_recovery import (
    _loop_plan,
    _parallel_plan,
    _pin_all_tool_policies,
    _pin_step_risk,
    _plant_started_toolcall,
)
from tests.unit.test_workflow_execution_creation import (
    _create,
    _publish_and_activate,
    _seed_authorized_workflow_user,
    _seed_ready_workflow,
)
from tests.unit.test_workflow_registry import (
    _create_draft_version,
    _create_workflow,
    _tool_plan,
)


async def _seed_claimed_workflow(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    plan_builder,
    max_attempts: int = 2,
    worker_id: str = "pg-wf-old",
    inputs: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, datetime, str, uuid.UUID]:
    async with session_factory() as session:
        ctx = await _seed_ready_workflow(
            session, policy_kwargs={"max_attempts": max_attempts}
        )
        workflow = await _create_workflow(session)
        plan = plan_builder(workflow.id, ctx["tool_version_id"])
        version = await _create_draft_version(session, workflow.id, plan=plan)
        await _publish_and_activate(session, workflow.id, version.id)
        user_id = await _seed_authorized_workflow_user(
            session, workflow_id=workflow.id, tool_ids=[ctx["tool_id"]]
        )
        await session.commit()
        outcome = await _create(
            session,
            {
                "workflow_id": workflow.id,
                "version_id": version.id,
                "requester_id": user_id,
            },
            idempotency_key=_idem_key(),
            inputs=inputs,
        )
        await session.commit()
        execution_id = outcome.result.id
        await _pin_all_tool_policies(
            session,
            execution_id=execution_id,
            risk_class=RiskClass.READ_ONLY.value,
            max_attempts=max_attempts,
        )
        await ExecutionQueueService(session).stage_created_batch(limit=20)
        # Mark outbox published so sibling suites stay clean.
        from app.models.outbox import OutboxEvent
        from sqlalchemy import select

        now = datetime.now(UTC)
        rows = (
            await session.execute(
                select(OutboxEvent).where(
                    OutboxEvent.aggregate_id == execution_id,
                    OutboxEvent.published_at.is_(None),
                )
            )
        ).scalars().all()
        for row in rows:
            row.published_at = now
        await session.commit()
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id=worker_id, now=now
        )
        await session.commit()
        assert claim.claimed and claim.lease_token is not None
        return execution_id, now, worker_id, claim.lease_token


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_discovery_workflow_includes_flat_loop_excludes_nested(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    from tests.unit.test_workflow_dag_recovery import _nested_loop_plan

    wf_id, now, _w, _tok = await _seed_claimed_workflow(
        integration_session_factory,
        plan_builder=lambda wid, tv: _tool_plan(wid, tv),
    )
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(wf_id)
        assert execution is not None
        assert execution.source_type == ExecutionSourceType.WORKFLOW_VERSION.value
        execution.lease_expires_at = now - timedelta(seconds=1)
        await session.commit()

    loop_id, now3, _w3, _tok3 = await _seed_claimed_workflow(
        integration_session_factory,
        plan_builder=_loop_plan,
        worker_id="pg-loop-old",
        inputs={"items": [{"id": 1}]},
    )
    async with integration_session_factory() as session:
        loop_exec = await ExecutionRepository(session).get(loop_id)
        assert loop_exec is not None
        assert plan_supports_workflow_running_recovery(loop_exec.plan_snapshot) is True
        loop_exec.lease_expires_at = now3 - timedelta(seconds=1)
        await session.commit()

        # Nested LOOP: plant lightweight unsupported row (not publishable).
        # Insert as QUEUED first — PG check forbids RUNNING without lease.
        nested_snap = _nested_loop_plan(uuid.uuid4(), uuid.uuid4())
        assert plan_supports_workflow_running_recovery(nested_snap) is False
        from app.schemas.execution_plan import EXECUTION_PLAN_SCHEMA_VERSION

        nested = await ExecutionRepository(session).create_execution(
            source_type=ExecutionSourceType.WORKFLOW_VERSION.value,
            trigger_type="USER",
            requester_id=loop_exec.requester_id,
            agent_request_id=None,
            agent_version_id=None,
            plan_validation_run_id=None,
            status=ExecutionStatus.QUEUED.value,
            plan_schema_version=EXECUTION_PLAN_SCHEMA_VERSION,
            plan_snapshot=nested_snap,
            plan_hash="b" * 64,
            input_snapshot={},
            policy_snapshot=dict(loop_exec.policy_snapshot),
            trace_id=None,
            requested_at=now3,
            workflow_version_id=loop_exec.workflow_version_id,
        )
        nested.status = ExecutionStatus.RUNNING.value
        nested.worker_id = "nested-old"
        nested.lease_token = uuid.uuid4()
        nested.lease_expires_at = now3 - timedelta(seconds=1)
        nested.started_at = now3
        await session.commit()
        nested_id = nested.id

    async with integration_session_factory() as session:
        first = await ExecutionRecoveryService(
            session, lease_seconds=60
        ).list_expired_running_ids(limit=50, now=now)
        assert wf_id in first
        assert loop_id in first
        assert nested_id not in first
        # Outbox restart: rediscovery remains durable in PostgreSQL.
        second = await ExecutionRecoveryService(
            session, lease_seconds=60
        ).list_expired_running_ids(limit=50, now=now)
        assert wf_id in second
        assert loop_id in second
        assert nested_id not in second


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_parallel_safe_unsafe_race_single_winner_mcp_zero(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, now, old_worker, _tok = await _seed_claimed_workflow(
        integration_session_factory,
        plan_builder=_parallel_plan,
        max_attempts=2,
    )
    async with integration_session_factory() as session:
        await _pin_step_risk(
            session,
            execution_id=execution_id,
            step_key="b",
            risk_class=RiskClass.NON_IDEMPOTENT_WRITE.value,
        )
        await _plant_started_toolcall(
            session,
            execution_id=execution_id,
            step_key="a",
            worker_id=old_worker,
            now=now,
        )
        await _plant_started_toolcall(
            session,
            execution_id=execution_id,
            step_key="b",
            worker_id=old_worker,
            now=now,
        )
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        execution.lease_expires_at = now - timedelta(seconds=1)
        await session.commit()

    async def _recover(worker: str) -> RecoveryDecision:
        async with integration_session_factory() as session:
            outcome = await ExecutionRecoveryService(session, lease_seconds=60).recover(
                execution_id=execution_id,
                worker_id=worker,
                now=now + timedelta(seconds=2),
            )
            await session.commit()
            return outcome.decision

    decisions = await asyncio.gather(
        _recover("pg-winner"),
        _recover("pg-loser"),
    )
    assert RecoveryDecision.UNKNOWN_OUTCOME in decisions
    assert RecoveryDecision.NO_OP in decisions or decisions.count(
        RecoveryDecision.UNKNOWN_OUTCOME
    ) == 1

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        assert by_key["a"].status == StepStatus.UNKNOWN_OUTCOME.value
        assert by_key["b"].status == StepStatus.UNKNOWN_OUTCOME.value
        for key in ("a", "b"):
            attempts = await ExecutionRepository(session).list_attempts(by_key[key].id)
            assert attempts[0].status == StepAttemptStatus.UNKNOWN_OUTCOME.value
            tcs = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
            assert (
                tcs[0].normalized_status
                == ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_duplicate_recovery_idempotent_takeover(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, now, _w, old_token = await _seed_claimed_workflow(
        integration_session_factory,
        plan_builder=lambda wid, tv: _tool_plan(wid, tv),
    )
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        execution.lease_expires_at = now - timedelta(seconds=1)
        await session.commit()
        first = await ExecutionRecoveryService(session, lease_seconds=60).recover(
            execution_id=execution_id,
            worker_id="pg-dup-1",
            now=now + timedelta(seconds=1),
        )
        await session.commit()
        assert first.decision == RecoveryDecision.TAKEOVER_READY
        assert first.lease_token != old_token
        second = await ExecutionRecoveryService(session, lease_seconds=60).recover(
            execution_id=execution_id,
            worker_id="pg-dup-2",
            now=now + timedelta(seconds=2),
        )
        await session.commit()
        assert second.decision == RecoveryDecision.NO_OP
