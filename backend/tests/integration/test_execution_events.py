"""PostgreSQL integration tests for durable execution_events + SSE cursor."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from app.domain.enums import (
    ExecutionEventVisibility,
    ExecutionStatus,
)
from app.execution.events import ExecutionEventWriter
from app.execution.queue import ExecutionQueueService
from app.models.execution_event import ExecutionEvent
from app.repositories.execution_event import ExecutionEventRepository
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.helpers.execution_ops import seed_execution, seed_user

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_execution_events_schema_constraints_and_index(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        cols = (
            await session.execute(
                text(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema='public' AND table_name='execution_events'
                    ORDER BY column_name
                    """
                )
            )
        ).scalars().all()
        assert set(cols) >= {
            "id",
            "event_id",
            "execution_id",
            "step_execution_id",
            "event_type",
            "visibility",
            "payload",
            "payload_version",
            "occurred_at",
        }

        indexes = (
            await session.execute(
                text(
                    """
                    SELECT indexname FROM pg_indexes
                    WHERE schemaname='public' AND tablename='execution_events'
                    """
                )
            )
        ).scalars().all()
        assert "ix_execution_events_execution_id_id" in indexes

        checks = (
            await session.execute(
                text(
                    """
                    SELECT conname, pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conrelid = 'public.execution_events'::regclass
                      AND contype = 'c'
                    """
                )
            )
        ).all()
        check_map = {name: definition for name, definition in checks}
        joined = " ".join(check_map.values()).lower()
        assert "user" in joined and "operator" in joined and "internal" in joined
        assert "payload_version" in joined
        assert "jsonb_typeof" in joined
        assert "event_type" in joined


@pytest.mark.asyncio
async def test_same_tx_append_and_rollback(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        owner = await seed_user(session)
        execution = await seed_execution(
            session,
            requester_id=owner,
            status=ExecutionStatus.CREATED.value,
        )
        await session.commit()
        execution_id = execution.id

    async with integration_session_factory() as session:
        execution = (
            await session.execute(
                text("SELECT id FROM executions WHERE id = :id"),
                {"id": execution_id},
            )
        ).one()
        del execution
        from app.repositories.execution import ExecutionRepository

        row = await ExecutionRepository(session).get(execution_id)
        assert row is not None
        writer = ExecutionEventWriter(session)
        previous = row.status
        row.status = ExecutionStatus.QUEUED.value
        event = await writer.emit_execution_status_changed(
            row, previous_status=previous
        )
        assert event is not None
        event_id = event.event_id
        await session.flush()
        await session.rollback()

    async with integration_session_factory() as session:
        gone = await ExecutionEventRepository(session).list_after(
            execution_id=execution_id,
            after_id=0,
            visibilities=[ExecutionEventVisibility.USER.value],
            limit=10,
        )
        assert gone == []
        # Confirm UUID not present
        found = (
            await session.execute(
                select(ExecutionEvent).where(ExecutionEvent.event_id == event_id)
            )
        ).scalar_one_or_none()
        assert found is None


@pytest.mark.asyncio
async def test_created_queued_started_ordering_and_cursor(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        owner = await seed_user(session)
        execution = await seed_execution(
            session,
            requester_id=owner,
            status=ExecutionStatus.CREATED.value,
        )
        writer = ExecutionEventWriter(session)
        e1 = await writer.emit_execution_status_changed(
            execution, previous_status=None
        )
        previous = execution.status
        execution.status = ExecutionStatus.QUEUED.value
        e2 = await writer.emit_execution_status_changed(
            execution, previous_status=previous
        )
        previous = execution.status
        execution.status = ExecutionStatus.RUNNING.value
        execution.worker_id = "w1"
        execution.lease_token = uuid.uuid4()
        execution.lease_expires_at = datetime.now(UTC)
        execution.heartbeat_at = datetime.now(UTC)
        execution.started_at = datetime.now(UTC)
        e3 = await writer.emit_execution_status_changed(
            execution, previous_status=previous
        )
        await session.commit()
        assert e1 is not None and e2 is not None and e3 is not None
        assert e1.id < e2.id < e3.id
        assert [e1.event_type, e2.event_type, e3.event_type] == [
            "execution.created",
            "execution.queued",
            "execution.started",
        ]
        execution_id = execution.id
        cursor = e1.id

    async with integration_session_factory() as session:
        rows = await ExecutionEventRepository(session).list_after(
            execution_id=execution_id,
            after_id=cursor,
            visibilities=[ExecutionEventVisibility.USER.value],
            limit=100,
        )
        assert [r.event_type for r in rows] == [
            "execution.queued",
            "execution.started",
        ]
        assert all(r.id > cursor for r in rows)


@pytest.mark.asyncio
async def test_queue_service_emits_queued_event(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """CREATED → QUEUED staging appends execution.queued in the same TX."""
    from app.domain.enums import ExecutionSourceType

    async with integration_session_factory() as session:
        owner = await seed_user(session)
        execution = await seed_execution(
            session,
            requester_id=owner,
            status=ExecutionStatus.CREATED.value,
            source_type=ExecutionSourceType.WORKFLOW_VERSION.value,
        )
        # Materialize-style created event
        await ExecutionEventWriter(session).emit_execution_status_changed(
            execution, previous_status=None
        )
        await session.commit()
        execution_id = execution.id

    async with integration_session_factory() as session:
        staged = await ExecutionQueueService(session).stage_created_batch(limit=10)
        assert staged >= 1
        await session.commit()

    async with integration_session_factory() as session:
        rows = await ExecutionEventRepository(session).list_after(
            execution_id=execution_id,
            after_id=0,
            visibilities=[ExecutionEventVisibility.USER.value],
            limit=20,
        )
        types = [r.event_type for r in rows]
        assert "execution.created" in types
        assert "execution.queued" in types
        assert types.index("execution.created") < types.index("execution.queued")
