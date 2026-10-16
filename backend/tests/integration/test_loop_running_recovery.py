"""PostgreSQL tests for Issue #117 flat FOR_EACH / WHILE RUNNING recovery."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from app.domain.enums import ExecutionStatus, RiskClass, StepStatus
from app.execution.recovery import ExecutionRecoveryService, RecoveryDecision
from app.repositories.execution import ExecutionRepository
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.test_execution_creation import _install_no_side_effects
from tests.unit.test_loop_running_recovery import (
    _child_key,
    _loop_parent,
    _materialize_foreach_iters,
    _OkClient,
    _recover,
    _recover_and_run,
    _seed_foreach,
    _start_foreach_running_pinned,
)
from tests.unit.test_workflow_dag_recovery import (
    _expire_lease,
    _pin_all_tool_policies,
    _plant_started_toolcall,
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_foreach_zero_child_and_duplicate_race(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with integration_session_factory() as session:
        eid, _c, now, _w, _tok = await _seed_foreach(
            session, items=[{"id": 1}, {"id": 2}]
        )
        await _start_foreach_running_pinned(session, execution_id=eid, now=now)
        await _expire_lease(session, execution_id=eid, claim_now=now)

    client = _OkClient()
    outcome, _run = await _recover_and_run(
        integration_session_factory, execution_id=eid, client=client
    )
    assert outcome.taken_over is True
    assert len(client.calls) >= 1

    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid)
        parent = _loop_parent(steps)
        iter1 = [
            s for s in steps if s.parent_step_id == parent.id and s.iteration_no == 1
        ]
        assert len(iter1) == 1

    # Duplicate recovery after terminalization / takeover is idempotent.
    async with integration_session_factory() as session:
        again = await _recover(session, execution_id=eid, worker_id="dup")
        await session.commit()
    assert again.decision == RecoveryDecision.NO_OP or again.taken_over is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_unsafe_loop_child_mcp_zero_race(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with integration_session_factory() as session:
        eid, _c, now, old_w, _tok = await _seed_foreach(
            session, items=[{"id": 1}], max_attempts=2
        )
        await _pin_all_tool_policies(
            session,
            execution_id=eid,
            risk_class=RiskClass.NON_IDEMPOTENT_WRITE.value,
            max_attempts=2,
        )
        await _start_foreach_running_pinned(session, execution_id=eid, now=now)
        await _materialize_foreach_iters(
            session, execution_id=eid, through=1, terminal_through=0, now=now
        )
        parent = _loop_parent(await ExecutionRepository(session).list_steps(eid))
        await _plant_started_toolcall(
            session,
            execution_id=eid,
            step_key=_child_key(parent.id, 1),
            worker_id=old_w,
            now=now,
        )
        await _expire_lease(session, execution_id=eid, claim_now=now)

    async def _one(worker: str) -> RecoveryDecision:
        async with integration_session_factory() as session:
            out = await ExecutionRecoveryService(session, lease_seconds=120).recover(
                execution_id=eid,
                worker_id=worker,
                now=now + timedelta(seconds=2),
            )
            await session.commit()
            return out.decision

    d1, d2 = await asyncio.gather(_one("pg-a"), _one("pg-b"))
    assert {d1, d2} == {
        RecoveryDecision.UNKNOWN_OUTCOME,
        RecoveryDecision.NO_OP,
    } or RecoveryDecision.UNKNOWN_OUTCOME in {d1, d2}

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(eid)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        child = next(
            s
            for s in await ExecutionRepository(session).list_steps(eid)
            if s.parent_step_id is not None
        )
        assert child.status == StepStatus.UNKNOWN_OUTCOME.value
