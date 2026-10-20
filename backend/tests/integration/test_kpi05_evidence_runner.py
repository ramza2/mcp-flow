"""PostgreSQL checks for KPI-05 evidence runner ops hardening."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from app.domain.enums import ExecutionStatus
from app.repositories.execution import ExecutionRepository
from app.schemas.workflow import WorkflowCreate
from app.services.workflow import WorkflowService
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.evaluation.kpi05_evidence.constants import SCENARIO_CONDITION
from tests.evaluation.kpi05_evidence.runner import (
    check_db_duplicate_guards,
    poll_execution_until_terminal,
    workflow_name_for,
)
from tests.helpers.execution_ops import seed_execution, seed_user


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_poll_sees_worker_commit_via_fresh_sessions(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Same-session identity map can stay stale; runner poll must open fresh sessions."""

    async with integration_session_factory() as session:
        user_id = await seed_user(session)
        row = await seed_execution(
            session,
            requester_id=user_id,
            status=ExecutionStatus.CREATED.value,
        )
        execution_id = row.id
        await session.commit()

    async with integration_session_factory() as stale:
        cached = await ExecutionRepository(stale).get(execution_id)
        assert cached is not None
        assert cached.status == ExecutionStatus.CREATED.value

        async with integration_session_factory() as worker:
            live = await ExecutionRepository(worker).get(execution_id)
            assert live is not None
            now = datetime.now(UTC)
            live.status = ExecutionStatus.SUCCEEDED.value
            live.queued_at = now
            live.started_at = now
            live.finished_at = now
            await worker.commit()

        # Identity map on the open stale session still shows CREATED.
        again = await ExecutionRepository(stale).get(execution_id)
        assert again is not None
        assert again.status == ExecutionStatus.CREATED.value

        result = await poll_execution_until_terminal(
            integration_session_factory,
            execution_id,
            wait_seconds=5,
            poll_interval=0.05,
        )
        assert result["timed_out"] is False
        assert result["status"] == ExecutionStatus.SUCCEEDED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_duplicate_run_stamp_blocks_before_new_resources(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    stamp = f"pgdup-{uuid.uuid4().hex[:10]}"
    name = workflow_name_for(SCENARIO_CONDITION, stamp)
    async with integration_session_factory() as session:
        await WorkflowService(session).create(
            WorkflowCreate(name=name, description="kpi05 dup probe")
        )
        await session.commit()

    async with integration_session_factory() as session:
        before_rows, before_total = await WorkflowService(session).list(
            page=1, page_size=100, q=name
        )
        before_ids = {str(w.id) for w in before_rows}
        assert before_total >= 1
        guard = await check_db_duplicate_guards(
            session, run_stamp=stamp, requester_id=uuid.uuid4()
        )
        assert guard["ok"] is False
        assert guard["reason"] == "DUPLICATE_RUN_STAMP"
        after_rows, after_total = await WorkflowService(session).list(
            page=1, page_size=100, q=name
        )
        after_ids = {str(w.id) for w in after_rows}
        assert after_ids == before_ids
        assert after_total == before_total
