"""Unit regressions for bounded Celery publish and Outbox timestamps."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.execution.queue as queue_module
from app.core.config import get_settings
from app.domain.enums import ExecutionStatus
from app.execution.queue import ExecutionQueueService, OutboxRelayService
from app.infrastructure.celery_app import celery_app
from app.infrastructure.queue import CeleryExecutionQueuePublisher
from app.models.outbox import OutboxEvent

from tests.unit.test_execution_creation import _create, _idem_key, _seed_ready


async def _created_execution(session: AsyncSession) -> uuid.UUID:
    seeded = await _seed_ready(session)
    outcome = await _create(session, seeded, idempotency_key=_idem_key())
    assert outcome.result.status == ExecutionStatus.CREATED
    return outcome.result.id


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def test_celery_publish_retry_policy_is_bounded() -> None:
    settings = get_settings()
    retry_policy = celery_app.conf.task_publish_retry_policy

    assert celery_app.conf.task_publish_retry is True
    assert retry_policy["max_retries"] == settings.celery_publish_max_retries
    assert retry_policy["interval_start"] == 0
    assert retry_policy["interval_step"] == 0.2
    assert retry_policy["interval_max"] == 1.0


def test_execution_publisher_uses_producer_scoped_timeouts_and_retry_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    captured: dict[str, Any] = {}

    class FakeConnection:
        def __enter__(self) -> "FakeConnection":
            return self

        def __exit__(self, *args: object) -> None:
            del args

    connection = FakeConnection()

    def fake_connection_for_write(**kwargs: Any) -> FakeConnection:
        captured["connection_kwargs"] = kwargs
        return connection

    def fake_send_task(name: str, **kwargs: Any) -> None:
        captured["name"] = name
        captured.update(kwargs)

    monkeypatch.setattr(celery_app, "connection_for_write", fake_connection_for_write)
    monkeypatch.setattr(celery_app, "send_task", fake_send_task)
    execution_id = uuid.uuid4()
    outbox_event_id = uuid.uuid4()

    CeleryExecutionQueuePublisher().publish_execution(
        execution_id=execution_id,
        outbox_event_id=outbox_event_id,
    )

    assert captured["connection_kwargs"] == {
        "connect_timeout": settings.celery_publish_connect_timeout,
        "transport_options": {
            "socket_connect_timeout": settings.celery_publish_connect_timeout,
            "socket_timeout": settings.celery_publish_socket_timeout,
            "max_retries": settings.celery_publish_max_retries,
        },
    }
    assert captured["name"] == "mcpflow.execution.claim"
    assert captured["queue"] == "execution"
    assert captured["task_id"] == str(outbox_event_id)
    assert captured["connection"] is connection
    assert captured["kwargs"] == {
        "execution_id": str(execution_id),
        "outbox_event_id": str(outbox_event_id),
    }
    assert captured["retry"] is True
    assert captured["retry_policy"] == celery_app.conf.task_publish_retry_policy


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
    assert row.published_at is not None
    assert row.last_attempt_at is not None
    assert _as_utc(row.published_at) == after
    assert _as_utc(row.last_attempt_at) == after
    assert row.publish_attempt_count == 1
    assert row.last_error_code is None
