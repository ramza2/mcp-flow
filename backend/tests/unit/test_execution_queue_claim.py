"""Unit regressions for Execution Queue / Claim foundation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import ExecutionStatus, StepStatus
from app.execution.claim import ExecutionClaimService
from app.execution.queue import ExecutionQueueService, OutboxRelayService
from app.infrastructure.celery_app import celery_app
from app.models.execution import Execution, ExecutionStep
from app.models.outbox import OutboxEvent
from app.repositories.execution import ExecutionRepository

from tests.unit.test_execution_creation import _create, _idem_key, _seed_ready


class FakePublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    def publish_execution(
        self,
        *,
        execution_id: uuid.UUID,
        outbox_event_id: uuid.UUID,
    ) -> None:
        self.calls.append((execution_id, outbox_event_id))
        if self.fail:
            raise ConnectionError("broker unavailable")


async def _created_execution(session: AsyncSession) -> tuple[uuid.UUID, str]:
    seeded = await _seed_ready(session)
    key = _idem_key()
    outcome = await _create(session, seeded, idempotency_key=key)
    assert outcome.result.status == ExecutionStatus.CREATED
    return outcome.result.id, key


@pytest.mark.asyncio
async def test_stage_created_execution_is_atomic_and_keeps_step_pending(
    db_session: AsyncSession,
) -> None:
    execution_id, _ = await _created_execution(db_session)
    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    before_version = execution.lock_version

    staged = await ExecutionQueueService(db_session).stage_created_batch(limit=10)
    await db_session.commit()

    assert staged == 1
    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.QUEUED.value
    assert execution.queued_at is not None
    assert execution.lock_version == before_version + 1
    steps = await ExecutionRepository(db_session).list_steps(execution_id)
    assert len(steps) == 1
    assert steps[0].status == StepStatus.PENDING.value

    outbox = (
        await db_session.execute(
            select(OutboxEvent).where(OutboxEvent.aggregate_id == execution_id)
        )
    ).scalar_one()
    assert outbox.event_type == "EXECUTION_DISPATCH"
    assert outbox.aggregate_type == "EXECUTION"
    assert outbox.dedupe_key == f"execution:{execution_id}:initial"
    assert outbox.payload == {"execution_id": str(execution_id)}
    assert outbox.published_at is None


@pytest.mark.asyncio
async def test_stager_ignores_non_created_execution(db_session: AsyncSession) -> None:
    execution_id, _ = await _created_execution(db_session)
    assert await ExecutionQueueService(db_session).stage_created_batch(limit=10) == 1
    await db_session.commit()
    assert await ExecutionQueueService(db_session).stage_created_batch(limit=10) == 0
    await db_session.commit()
    count = (
        await db_session.execute(
            select(OutboxEvent).where(OutboxEvent.aggregate_id == execution_id)
        )
    ).scalars().all()
    assert len(count) == 1


@pytest.mark.asyncio
async def test_outbox_publish_failure_is_retryable_same_row(db_session: AsyncSession) -> None:
    execution_id, _ = await _created_execution(db_session)
    await ExecutionQueueService(db_session).stage_created_batch(limit=10)
    await db_session.commit()

    bad = FakePublisher(fail=True)
    result = await OutboxRelayService(db_session).publish_batch(publisher=bad, limit=10)
    await db_session.commit()
    assert result.failed == 1
    row = (
        await db_session.execute(
            select(OutboxEvent).where(OutboxEvent.aggregate_id == execution_id)
        )
    ).scalar_one()
    row_id = row.id
    assert row.published_at is None
    assert row.publish_attempt_count == 1
    assert row.last_error_code == "PUBLISH_FAILED"

    good = FakePublisher()
    result = await OutboxRelayService(db_session).publish_batch(publisher=good, limit=10)
    await db_session.commit()
    assert result.published == 1
    row = await db_session.get(OutboxEvent, row_id)
    assert row is not None
    assert row.published_at is not None
    assert row.publish_attempt_count == 2
    assert row.last_error_code is None
    assert good.calls == [(execution_id, row_id)]


@pytest.mark.asyncio
async def test_outbox_corrupt_payload_fails_closed_not_publish_retry(
    db_session: AsyncSession,
) -> None:
    execution_id, _ = await _created_execution(db_session)
    await ExecutionQueueService(db_session).stage_created_batch(limit=10)
    await db_session.commit()
    row = (
        await db_session.execute(
            select(OutboxEvent).where(OutboxEvent.aggregate_id == execution_id)
        )
    ).scalar_one()
    row.payload = {"execution_id": str(execution_id), "plan": "forbidden"}
    await db_session.commit()

    with pytest.raises(AppError) as exc_info:
        await OutboxRelayService(db_session).publish_batch(
            publisher=FakePublisher(), limit=10
        )
    assert exc_info.value.code == "RESOURCE_CONFLICT"
    await db_session.rollback()
    row = await db_session.get(OutboxEvent, row.id)
    assert row is not None
    assert row.publish_attempt_count == 0
    assert row.published_at is None


@pytest.mark.asyncio
async def test_claim_moves_execution_running_and_initial_step_ready(
    db_session: AsyncSession,
) -> None:
    execution_id, _ = await _created_execution(db_session)
    await ExecutionQueueService(db_session).stage_created_batch(limit=10)
    await db_session.commit()

    before = await ExecutionRepository(db_session).get(execution_id)
    assert before is not None
    execution_version = before.lock_version
    step = (await ExecutionRepository(db_session).list_steps(execution_id))[0]
    step_version = step.lock_version

    now = datetime.now(UTC)
    outcome = await ExecutionClaimService(db_session, lease_seconds=60).claim(
        execution_id=execution_id,
        worker_id=" worker-a ",
        now=now,
    )
    await db_session.commit()

    assert outcome.claimed is True
    assert outcome.worker_id == "worker-a"
    assert outcome.lease_token is not None
    assert outcome.lease_expires_at == now + timedelta(seconds=60)
    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.RUNNING.value
    assert execution.worker_id == "worker-a"
    assert execution.heartbeat_at == now
    assert execution.started_at == now
    assert execution.lock_version == execution_version + 1
    step = (await ExecutionRepository(db_session).list_steps(execution_id))[0]
    assert step.status == StepStatus.READY.value
    assert step.ready_at == now
    assert step.started_at is None
    assert step.attempt_count == 0
    assert step.resolved_input is None
    assert step.lock_version == step_version + 1


@pytest.mark.asyncio
async def test_duplicate_claim_delivery_is_noop(db_session: AsyncSession) -> None:
    execution_id, _ = await _created_execution(db_session)
    await ExecutionQueueService(db_session).stage_created_batch(limit=10)
    await db_session.commit()
    service = ExecutionClaimService(db_session, lease_seconds=60)
    first = await service.claim(execution_id=execution_id, worker_id="worker-a")
    await db_session.commit()
    assert first.claimed is True
    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    token = execution.lease_token
    started = execution.started_at
    step = (await ExecutionRepository(db_session).list_steps(execution_id))[0]
    ready = step.ready_at

    second = await service.claim(execution_id=execution_id, worker_id="worker-b")
    await db_session.commit()
    assert second.claimed is False
    assert second.reason == "STALE_DELIVERY"
    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    assert execution.worker_id == "worker-a"
    assert execution.lease_token == token
    assert execution.started_at == started
    step = (await ExecutionRepository(db_session).list_steps(execution_id))[0]
    assert step.ready_at == ready


@pytest.mark.asyncio
async def test_lease_renew_requires_matching_unexpired_token(db_session: AsyncSession) -> None:
    execution_id, _ = await _created_execution(db_session)
    await ExecutionQueueService(db_session).stage_created_batch(limit=10)
    await db_session.commit()
    service = ExecutionClaimService(db_session, lease_seconds=60)
    claim_time = datetime.now(UTC)
    claimed = await service.claim(
        execution_id=execution_id,
        worker_id="worker-a",
        now=claim_time,
    )
    await db_session.commit()
    assert claimed.lease_token is not None

    renew_time = claim_time + timedelta(seconds=10)
    expiry = await service.renew_lease(
        execution_id=execution_id,
        worker_id="worker-a",
        lease_token=claimed.lease_token,
        now=renew_time,
    )
    await db_session.commit()
    assert expiry == renew_time + timedelta(seconds=60)

    with pytest.raises(AppError):
        await service.renew_lease(
            execution_id=execution_id,
            worker_id="worker-a",
            lease_token=uuid.uuid4(),
            now=renew_time,
        )
    await db_session.rollback()

    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    execution.lease_expires_at = renew_time - timedelta(seconds=1)
    await db_session.commit()
    with pytest.raises(AppError):
        await service.renew_lease(
            execution_id=execution_id,
            worker_id="worker-a",
            lease_token=claimed.lease_token,
            now=renew_time,
        )


def test_celery_execution_queue_configuration() -> None:
    assert celery_app.conf.task_default_queue == "execution"
    assert celery_app.conf.task_routes["mcpflow.execution.claim"]["queue"] == "execution"
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert celery_app.conf.task_ignore_result is True
