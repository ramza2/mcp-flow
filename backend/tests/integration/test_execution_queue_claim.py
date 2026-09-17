"""PostgreSQL integration tests for Execution Queue / Claim foundation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError
from app.domain.enums import ExecutionStatus, StepStatus
from app.execution.claim import ExecutionClaimService
from app.execution.queue import ExecutionQueueService, OutboxRelayService
from app.models.execution import Execution
from app.models.outbox import OutboxEvent
from app.repositories.execution import ExecutionRepository
from app.services.execution_creation import ExecutionCreationService

from tests.integration.test_execution_creation import _create, _idem_key, _seed_ready


class FakePublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[object, object]] = []

    def publish_execution(self, *, execution_id: object, outbox_event_id: object) -> None:
        self.calls.append((execution_id, outbox_event_id))
        if self.fail:
            raise ConnectionError("redis unavailable")


async def _create_one(session: AsyncSession) -> tuple[dict[str, object], object, str]:
    seeded = await _seed_ready(session)
    key = _idem_key()
    outcome = await _create(session, seeded, idempotency_key=key)
    return seeded, outcome.result.id, key


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_stage_publish_claim_restart_recovery(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        _seeded, execution_id, _key = await _create_one(session)

    async with integration_session_factory() as session:
        staged = await ExecutionQueueService(session).stage_created_batch(limit=10)
        await session.commit()
        assert staged == 1

    publisher = FakePublisher()
    async with integration_session_factory() as session:
        relay = await OutboxRelayService(session).publish_batch(
            publisher=publisher,
            limit=10,
        )
        await session.commit()
        assert relay.published == 1
        event = (
            await session.execute(
                select(OutboxEvent).where(OutboxEvent.aggregate_id == execution_id)
            )
        ).scalar_one()
        assert event.payload == {"execution_id": str(execution_id)}
        assert publisher.calls == [(execution_id, event.id)]

    async with integration_session_factory() as session:
        claimed = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id,
            worker_id="pg-worker-a",
        )
        await session.commit()
        assert claimed.claimed is True

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.RUNNING.value
        assert execution.worker_id == "pg-worker-a"
        assert execution.lease_token is not None
        assert execution.lease_expires_at is not None
        assert execution.heartbeat_at is not None
        assert execution.started_at is not None
        steps = await ExecutionRepository(session).list_steps(execution_id)
        assert len(steps) == 1
        assert steps[0].status == StepStatus.READY.value
        assert steps[0].ready_at is not None
        assert steps[0].started_at is None
        assert steps[0].attempt_count == 0
        assert steps[0].resolved_input is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_publish_failure_persists_unpublished_and_retries_same_row(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        _seeded, execution_id, _key = await _create_one(session)
        await ExecutionQueueService(session).stage_created_batch(limit=10)
        await session.commit()

    async with integration_session_factory() as session:
        result = await OutboxRelayService(session).publish_batch(
            publisher=FakePublisher(fail=True),
            limit=10,
        )
        await session.commit()
        assert result.failed == 1
        row = (
            await session.execute(
                select(OutboxEvent).where(OutboxEvent.aggregate_id == execution_id)
            )
        ).scalar_one()
        event_id = row.id
        assert row.published_at is None
        assert row.publish_attempt_count == 1
        assert row.last_error_code == "PUBLISH_FAILED"
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.QUEUED.value

    async with integration_session_factory() as session:
        result = await OutboxRelayService(session).publish_batch(
            publisher=FakePublisher(),
            limit=10,
        )
        await session.commit()
        assert result.published == 1
        row = await session.get(OutboxEvent, event_id)
        assert row is not None
        assert row.published_at is not None
        assert row.publish_attempt_count == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_double_stager_creates_single_outbox(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        _seeded, execution_id, _key = await _create_one(session)

    async def stage_once() -> int:
        async with integration_session_factory() as session:
            count = await ExecutionQueueService(session).stage_created_batch(limit=10)
            await session.commit()
            return count

    counts = await asyncio.gather(stage_once(), stage_once())
    assert sum(counts) == 1
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.QUEUED.value
        outbox_count = (
            await session.execute(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.aggregate_id == execution_id)
            )
        ).scalar_one()
        assert outbox_count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_double_claim_only_one_worker_wins(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        _seeded, execution_id, _key = await _create_one(session)
        await ExecutionQueueService(session).stage_created_batch(limit=10)
        await session.commit()

    async def claim_once(worker: str) -> bool:
        async with integration_session_factory() as session:
            outcome = await ExecutionClaimService(session, lease_seconds=60).claim(
                execution_id=execution_id,
                worker_id=worker,
            )
            await session.commit()
            return outcome.claimed

    results = await asyncio.gather(claim_once("worker-a"), claim_once("worker-b"))
    assert results.count(True) == 1
    assert results.count(False) == 1
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.RUNNING.value
        assert execution.worker_id in {"worker-a", "worker-b"}
        steps = await ExecutionRepository(session).list_steps(execution_id)
        assert [step.status for step in steps] == [StepStatus.READY.value]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_heartbeat_and_expired_lease_rejection(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        _seeded, execution_id, _key = await _create_one(session)
        await ExecutionQueueService(session).stage_created_batch(limit=10)
        await session.commit()
        now = datetime.now(UTC)
        service = ExecutionClaimService(session, lease_seconds=60)
        outcome = await service.claim(
            execution_id=execution_id,
            worker_id="worker-a",
            now=now,
        )
        await session.commit()
        assert outcome.lease_token is not None

    async with integration_session_factory() as session:
        service = ExecutionClaimService(session, lease_seconds=60)
        renewed_at = now + timedelta(seconds=10)
        expiry = await service.renew_lease(
            execution_id=execution_id,
            worker_id="worker-a",
            lease_token=outcome.lease_token,
            now=renewed_at,
        )
        await session.commit()
        assert expiry == renewed_at + timedelta(seconds=60)

    async with integration_session_factory() as session:
        await session.execute(
            update(Execution)
            .where(Execution.id == execution_id)
            .values(lease_expires_at=renewed_at - timedelta(seconds=1))
        )
        await session.commit()

    async with integration_session_factory() as session:
        with pytest.raises(AppError) as exc_info:
            await ExecutionClaimService(session, lease_seconds=60).renew_lease(
                execution_id=execution_id,
                worker_id="worker-a",
                lease_token=outcome.lease_token,
                now=renewed_at,
            )
        assert exc_info.value.code == "RESOURCE_CONFLICT"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_queued_corruption_fails_closed(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        _seeded, execution_id, _key = await _create_one(session)
        await ExecutionQueueService(session).stage_created_batch(limit=10)
        await session.commit()
        await session.execute(
            update(Execution).where(Execution.id == execution_id).values(queued_at=None)
        )
        await session.commit()

    async with integration_session_factory() as session:
        with pytest.raises(AppError):
            await ExecutionClaimService(session, lease_seconds=60).claim(
                execution_id=execution_id,
                worker_id="worker-a",
            )
        await session.rollback()
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.QUEUED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_creation_idempotency_replay_remains_created_after_running(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded, execution_id, key = await _create_one(session)
        await ExecutionQueueService(session).stage_created_batch(limit=10)
        await session.commit()
        await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id,
            worker_id="worker-a",
        )
        await session.commit()

    async with integration_session_factory() as session:
        replay = await ExecutionCreationService(session).create_from_agent_request(
            agent_request_id=seeded["request_id"],
            requester_id=seeded["requester_id"],
            idempotency_key=key,
        )
        assert replay.replayed is True
        assert replay.result.id == execution_id
        assert replay.result.status == ExecutionStatus.CREATED
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.RUNNING.value
