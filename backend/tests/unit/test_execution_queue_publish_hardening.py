"""Unit regressions for bounded Celery publish and Outbox timestamps."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.execution.queue as queue_module
from app.core.config import get_settings
from app.domain.enums import ExecutionStatus
from app.execution.queue import ExecutionQueueService, OutboxRelayService
from app.infrastructure.celery_app import celery_app
from app.models.outbox import OutboxEvent

from tests.unit.test_execution_creation import _create, _idem_key, _seed_ready


async def _created_execution(session: AsyncSession) -> uuid.UUID:
    seeded = await _seed_ready(session)
    outcome = await _create(session, seeded, idempotency_key=_idem_key())
    assert outcome.result.status == ExecutionStatus.CREATED
    return outcome.result.id


def test_celery_publish_is_bounded_by_transport_and_retry_settings() -> None:
    settings = get_settings()
    transport = celery_app.conf.broker_transport_options
    retry_policy = celery_app.conf.task_publish_retry_policy

    assert transport["socket_connect_timeout"] == settings.celery_broker_connection_timeout
    assert transport["socket_timeout"] == settings.celery_broker_socket_timeout
    assert transport["max_retries"] == settings.celery_publish_max_retries
    assert celery_app.conf.task_publish_retry is True
    assert retry_policy["max_retries"] == settings.celery_publish_max_retries
    assert retry_policy["interval_start"] == 0
    assert retry_policy["interval_step"] == 0.2
    assert retry_policy["interval_max"] == 1.0


@pytest.mark.asyncio
async def test_outbox_published_at_is_captured_after_publish_returns(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id = await _created_execution(db_session)
    await ExecutionQueueService(db_session).stage_created_batch(limit=10)
    await db_session.commit()

    before = datetime(2026, 9, 17, 6, 41, 34, tzinfo=UTC)
    after = before + timedelta(minutes=8)
    clock = {"now": before}

    class FakeDateTime:
        @classmethod
        def now(cls, tz: object) -> datetime:
            assert tz is UTC
            return clock["now"]

    class AdvancingPublisher:
        def publish_execution(
            self,
            *,
            execution_id: uuid.UUID,
            outbox_event_id: uuid.UUID,
        ) -> None:
            del execution_id, outbox_event_id
            clock["now"] = after

    monkeypatch.setattr(queue_module, "datetime", FakeDateTime)

    result = await OutboxRelayService(db_session).publish_batch(
        publisher=AdvancingPublisher(),
        limit=10,
    )
    await db_session.commit()

    assert result.published == 1
    row = (
        await db_session.execute(
            select(OutboxEvent).where(OutboxEvent.aggregate_id == execution_id)
        )
    ).scalar_one()
    assert row.published_at == after
    assert row.last_attempt_at == after
    assert row.publish_attempt_count == 1
    assert row.last_error_code is None
