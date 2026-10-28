"""Durable outbox repository for at-least-once broker delivery."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outbox import OutboxEvent


class OutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_execution_dispatch(
        self,
        *,
        execution_id: uuid.UUID,
        created_at: datetime,
    ) -> OutboxEvent:
        row = OutboxEvent(
            id=uuid.uuid4(),
            event_type="EXECUTION_DISPATCH",
            aggregate_type="EXECUTION",
            aggregate_id=execution_id,
            dedupe_key=f"execution:{execution_id}:initial",
            payload={"execution_id": str(execution_id)},
            created_at=created_at,
            last_attempt_at=None,
            published_at=None,
            publish_attempt_count=0,
            last_error_code=None,
            lock_version=1,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def create_execution_approval_resume(
        self,
        *,
        execution_id: uuid.UUID,
        approval_request_id: uuid.UUID,
        created_at: datetime,
    ) -> OutboxEvent:
        row = OutboxEvent(
            id=uuid.uuid4(),
            event_type="EXECUTION_APPROVAL_RESUME",
            aggregate_type="EXECUTION",
            aggregate_id=execution_id,
            dedupe_key=(f"execution:{execution_id}:approval:{approval_request_id}:resume"),
            payload={
                "execution_id": str(execution_id),
                "approval_request_id": str(approval_request_id),
            },
            created_at=created_at,
            last_attempt_at=None,
            published_at=None,
            publish_attempt_count=0,
            last_error_code=None,
            lock_version=1,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def create_execution_mrtr_resume(
        self,
        *,
        execution_id: uuid.UUID,
        input_request_id: uuid.UUID,
        created_at: datetime,
    ) -> OutboxEvent:
        row = OutboxEvent(
            id=uuid.uuid4(),
            event_type="EXECUTION_MRTR_RESUME",
            aggregate_type="EXECUTION",
            aggregate_id=execution_id,
            dedupe_key=(f"execution:{execution_id}:mrtr:{input_request_id}:resume"),
            payload={
                "execution_id": str(execution_id),
                "input_request_id": str(input_request_id),
            },
            created_at=created_at,
            last_attempt_at=None,
            published_at=None,
            publish_attempt_count=0,
            last_error_code=None,
            lock_version=1,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def create_factory_job_dispatch(
        self,
        *,
        job_id: uuid.UUID,
        created_at: datetime,
    ) -> OutboxEvent:
        row = OutboxEvent(
            id=uuid.uuid4(),
            event_type="FACTORY_JOB_DISPATCH",
            aggregate_type="TOOL_FACTORY_JOB",
            aggregate_id=job_id,
            dedupe_key=f"factory_job:{job_id}:openapi_analyze",
            payload={"job_id": str(job_id)},
            created_at=created_at,
            last_attempt_at=None,
            published_at=None,
            publish_attempt_count=0,
            last_error_code=None,
            lock_version=1,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def create_factory_test_run_dispatch(
        self,
        *,
        test_run_id: uuid.UUID,
        created_at: datetime,
    ) -> OutboxEvent:
        row = OutboxEvent(
            id=uuid.uuid4(),
            event_type="FACTORY_TEST_RUN_DISPATCH",
            aggregate_type="TOOL_FACTORY_TEST_RUN",
            aggregate_id=test_run_id,
            dedupe_key=f"factory_test_run:{test_run_id}:sandbox_test",
            payload={"test_run_id": str(test_run_id)},
            created_at=created_at,
            last_attempt_at=None,
            published_at=None,
            publish_attempt_count=0,
            last_error_code=None,
            lock_version=1,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def create_tool_embedding_ensure(
        self,
        *,
        tool_version_id: uuid.UUID,
        embedding_profile_id: uuid.UUID,
        created_at: datetime,
    ) -> OutboxEvent:
        """Durable ensure request; occurrence UUID keeps dedupe_key unique for re-ensure."""
        occurrence_id = uuid.uuid4()
        row = OutboxEvent(
            id=occurrence_id,
            event_type="TOOL_EMBEDDING_ENSURE",
            aggregate_type="MCP_TOOL_VERSION",
            aggregate_id=tool_version_id,
            dedupe_key=(
                f"tool_embedding:{tool_version_id}:{embedding_profile_id}:ensure:{occurrence_id}"
            ),
            payload={
                "tool_version_id": str(tool_version_id),
                "embedding_profile_id": str(embedding_profile_id),
            },
            created_at=created_at,
            last_attempt_at=None,
            published_at=None,
            publish_attempt_count=0,
            last_error_code=None,
            lock_version=1,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def find_unpublished_tool_embedding_ensure(
        self,
        *,
        tool_version_id: uuid.UUID,
        embedding_profile_id: uuid.UUID,
    ) -> OutboxEvent | None:
        """Return an unpublished ensure for the same version+profile, if any."""
        expected_profile = str(embedding_profile_id)
        stmt = (
            select(OutboxEvent)
            .where(
                OutboxEvent.event_type == "TOOL_EMBEDDING_ENSURE",
                OutboxEvent.published_at.is_(None),
                OutboxEvent.aggregate_id == tool_version_id,
            )
            .order_by(OutboxEvent.created_at.asc(), OutboxEvent.id.asc())
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        for row in rows:
            payload = row.payload if isinstance(row.payload, dict) else {}
            if payload.get("embedding_profile_id") == expected_profile:
                return row
        return None

    async def find_latest_tool_embedding_ensure(
        self,
        *,
        tool_version_id: uuid.UUID,
        embedding_profile_id: uuid.UUID,
    ) -> OutboxEvent | None:
        """Latest ensure Outbox row for version+profile (published or not)."""
        expected_profile = str(embedding_profile_id)
        stmt = (
            select(OutboxEvent)
            .where(
                OutboxEvent.event_type == "TOOL_EMBEDDING_ENSURE",
                OutboxEvent.aggregate_id == tool_version_id,
            )
            .order_by(OutboxEvent.created_at.desc(), OutboxEvent.id.desc())
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        for row in rows:
            payload = row.payload if isinstance(row.payload, dict) else {}
            if payload.get("embedding_profile_id") == expected_profile:
                return row
        return None

    async def claim_unpublished_batch(self, *, limit: int) -> list[OutboxEvent]:
        # AGENT_SCHEDULE_PLAN is consumed in-process by AgentScheduleExecutionService.
        # Exclude it in SQL so plan backlog cannot starve EXECUTION_DISPATCH relay.
        stmt = (
            select(OutboxEvent)
            .where(
                OutboxEvent.published_at.is_(None),
                OutboxEvent.event_type != "AGENT_SCHEDULE_PLAN",
            )
            .order_by(OutboxEvent.created_at.asc(), OutboxEvent.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get(self, event_id: uuid.UUID) -> OutboxEvent | None:
        stmt = select(OutboxEvent).where(OutboxEvent.id == event_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def mark_published(self, row: OutboxEvent, *, now: datetime) -> None:
        row.publish_attempt_count += 1
        row.last_attempt_at = now
        row.published_at = now
        row.last_error_code = None
        row.lock_version += 1
        await self._session.flush()

    async def record_publish_failure(self, row: OutboxEvent, *, now: datetime) -> None:
        row.publish_attempt_count += 1
        row.last_attempt_at = now
        row.published_at = None
        row.last_error_code = "PUBLISH_FAILED"
        row.lock_version += 1
        await self._session.flush()
