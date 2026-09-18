"""PostgreSQL integration tests for TOOL Step Attempt foundation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.core.errors import AppError
from app.domain.enums import ExecutionStatus, StepAttemptStatus, StepStatus
from app.execution.claim import ExecutionClaimService
from app.execution.queue import ExecutionQueueService
from app.execution.tool_step_attempt import ToolStepAttemptService
from app.repositories.execution import ExecutionRepository
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.test_execution_creation import _create, _idem_key, _seed_ready


async def _claim_ready(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    worker_id: str = "pg-worker-a",
) -> dict[str, Any]:
    async with session_factory() as session:
        seeded = await _seed_ready(session)
        created = await _create(session, seeded, idempotency_key=_idem_key())
        execution_id = created.result.id

    async with session_factory() as session:
        staged = await ExecutionQueueService(session).stage_created_batch(limit=10)
        assert staged == 1
        await session.commit()

    async with session_factory() as session:
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id=worker_id
        )
        assert claim.claimed is True
        assert claim.lease_token is not None
        await session.commit()
        return {
            "execution_id": execution_id,
            "step_id": claim.ready_step_ids[0],
            "worker_id": claim.worker_id,
            "lease_token": claim.lease_token,
        }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_start_attempt_happy_path(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    claimed = await _claim_ready(integration_session_factory)

    async with integration_session_factory() as session:
        outcome = await ToolStepAttemptService(session).start(
            execution_id=claimed["execution_id"],
            step_execution_id=claimed["step_id"],
            worker_id=claimed["worker_id"],
            lease_token=claimed["lease_token"],
        )
        await session.commit()
        assert outcome.attempt_no == 1
        assert outcome.replayed is False

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(claimed["execution_id"])
        assert execution is not None
        assert execution.status == ExecutionStatus.RUNNING.value
        step = (await ExecutionRepository(session).list_steps(claimed["execution_id"]))[
            0
        ]
        assert step.status == StepStatus.RUNNING.value
        assert step.attempt_count == 1
        assert step.started_at is not None
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 1
        assert attempts[0].status == StepAttemptStatus.STARTED.value
        assert attempts[0].attempt_no == 1
        assert attempts[0].worker_id == claimed["worker_id"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_duplicate_start_no_extra_attempt(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    claimed = await _claim_ready(integration_session_factory)

    async with integration_session_factory() as session:
        first = await ToolStepAttemptService(session).start(
            execution_id=claimed["execution_id"],
            step_execution_id=claimed["step_id"],
            worker_id=claimed["worker_id"],
            lease_token=claimed["lease_token"],
        )
        await session.commit()

    async with integration_session_factory() as session:
        second = await ToolStepAttemptService(session).start(
            execution_id=claimed["execution_id"],
            step_execution_id=claimed["step_id"],
            worker_id=claimed["worker_id"],
            lease_token=claimed["lease_token"],
        )
        await session.commit()
        assert second.replayed is True
        assert second.attempt_id == first.attempt_id
        attempts = await ExecutionRepository(session).list_attempts(claimed["step_id"])
        assert len(attempts) == 1
        step = (await ExecutionRepository(session).list_steps(claimed["execution_id"]))[
            0
        ]
        assert step.attempt_count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_wrong_worker_and_expired_lease(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    claimed = await _claim_ready(integration_session_factory)

    async with integration_session_factory() as session:
        with pytest.raises(AppError) as exc:
            await ToolStepAttemptService(session).start(
                execution_id=claimed["execution_id"],
                step_execution_id=claimed["step_id"],
                worker_id="intruder",
                lease_token=claimed["lease_token"],
            )
        assert exc.value.status_code == 409

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(claimed["execution_id"])
        assert execution is not None
        execution.lease_expires_at = datetime.now(UTC) - timedelta(seconds=5)
        await session.commit()

    async with integration_session_factory() as session:
        with pytest.raises(AppError) as exc:
            await ToolStepAttemptService(session).start(
                execution_id=claimed["execution_id"],
                step_execution_id=claimed["step_id"],
                worker_id=claimed["worker_id"],
                lease_token=claimed["lease_token"],
            )
        assert exc.value.status_code == 409
        attempts = await ExecutionRepository(session).list_attempts(claimed["step_id"])
        assert attempts == []
        step = (await ExecutionRepository(session).list_steps(claimed["execution_id"]))[
            0
        ]
        assert step.status == StepStatus.READY.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_concurrent_start_one_attempt(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    claimed = await _claim_ready(integration_session_factory)

    async def _run() -> Any:
        async with integration_session_factory() as session:
            try:
                outcome = await ToolStepAttemptService(session).start(
                    execution_id=claimed["execution_id"],
                    step_execution_id=claimed["step_id"],
                    worker_id=claimed["worker_id"],
                    lease_token=claimed["lease_token"],
                )
                await session.commit()
                return outcome
            except AppError as exc:
                await session.rollback()
                return exc

    results = await asyncio.gather(_run(), _run())
    successes = [r for r in results if not isinstance(r, AppError)]
    errors = [r for r in results if isinstance(r, AppError)]
    assert len(successes) >= 1
    for err in errors:
        assert err.status_code == 409
        assert err.status_code != 500

    async with integration_session_factory() as session:
        attempts = await ExecutionRepository(session).list_attempts(claimed["step_id"])
        assert len(attempts) == 1
        assert attempts[0].attempt_no == 1
        step = (await ExecutionRepository(session).list_steps(claimed["execution_id"]))[
            0
        ]
        assert step.status == StepStatus.RUNNING.value
        assert step.attempt_count == 1
        ids = {s.attempt_id for s in successes}
        assert len(ids) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_restart_recovers_started_attempt(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    claimed = await _claim_ready(integration_session_factory)

    async with integration_session_factory() as session:
        outcome = await ToolStepAttemptService(session).start(
            execution_id=claimed["execution_id"],
            step_execution_id=claimed["step_id"],
            worker_id=claimed["worker_id"],
            lease_token=claimed["lease_token"],
        )
        await session.commit()
        attempt_id = outcome.attempt_id

    async with integration_session_factory() as session:
        attempts = await ExecutionRepository(session).list_attempts(claimed["step_id"])
        assert len(attempts) == 1
        assert attempts[0].id == attempt_id
        assert attempts[0].status == StepAttemptStatus.STARTED.value
        step = (await ExecutionRepository(session).list_steps(claimed["execution_id"]))[
            0
        ]
        assert step.status == StepStatus.RUNNING.value
        assert step.resolved_input is not None
