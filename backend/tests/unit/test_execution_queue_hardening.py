"""Focused hardening regressions for Execution Queue / Claim foundation."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import ExecutionSourceType, ExecutionStatus
from app.execution.claim import ExecutionClaimService
from app.execution.queue import ExecutionQueueService
from app.execution.tasks import _is_retryable_database_error, claim_execution_task
from app.repositories.execution import ExecutionRepository
from tests.unit.test_execution_queue_claim import _created_execution


def test_claim_task_retries_only_connection_level_database_errors() -> None:
    transient = OperationalError(
        "SELECT 1",
        {},
        RuntimeError("connection unavailable"),
        connection_invalidated=True,
    )
    durable = IntegrityError(
        "INSERT",
        {},
        RuntimeError("constraint violation"),
    )

    assert _is_retryable_database_error(transient) is True
    assert _is_retryable_database_error(durable) is False
    assert claim_execution_task.max_retries is None


@pytest.mark.asyncio
async def test_stager_ignores_non_agent_request_created_execution(
    db_session: AsyncSession,
) -> None:
    execution_id, _ = await _created_execution(db_session)
    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    execution.source_type = ExecutionSourceType.WORKFLOW_VERSION.value
    await db_session.commit()

    staged = await ExecutionQueueService(db_session).stage_created_batch(limit=10)
    await db_session.commit()

    assert staged == 0
    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.CREATED.value
    assert execution.queued_at is None


@pytest.mark.asyncio
async def test_claim_rejects_non_agent_request_queued_execution(
    db_session: AsyncSession,
) -> None:
    execution_id, _ = await _created_execution(db_session)
    assert await ExecutionQueueService(db_session).stage_created_batch(limit=10) == 1
    await db_session.commit()

    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    execution.source_type = ExecutionSourceType.WORKFLOW_VERSION.value
    await db_session.commit()

    with pytest.raises(AppError) as exc_info:
        await ExecutionClaimService(db_session, lease_seconds=60).claim(
            execution_id=execution_id,
            worker_id="worker-a",
        )
    assert exc_info.value.code == "RESOURCE_CONFLICT"
    await db_session.rollback()

    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.QUEUED.value


@pytest.mark.asyncio
async def test_claim_rejects_plan_hash_tamper_before_running(
    db_session: AsyncSession,
) -> None:
    execution_id, _ = await _created_execution(db_session)
    assert await ExecutionQueueService(db_session).stage_created_batch(limit=10) == 1
    await db_session.commit()

    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    execution.plan_hash = "0" * 64
    await db_session.commit()

    with pytest.raises(AppError) as exc_info:
        await ExecutionClaimService(db_session, lease_seconds=60).claim(
            execution_id=execution_id,
            worker_id="worker-a",
        )
    assert exc_info.value.code == "RESOURCE_CONFLICT"
    await db_session.rollback()

    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.QUEUED.value
