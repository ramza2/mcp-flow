"""PostgreSQL integration: MRTR resume claim concurrency and duplicate response Outbox."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from app.core.errors import AppError
from app.domain.enums import (
    ExecutionStatus,
    McpInputRequestStatus,
    StepStatus,
    ToolCallNormalizedStatus,
)
from app.execution.claim import ExecutionClaimService
from app.execution.mrtr_response import MrtrResponseService
from app.execution.mrtr_resume import MrtrResumeClaimService
from app.execution.queue import ExecutionQueueService
from app.execution.tool_runner import McpToolRunner
from app.models.outbox import OutboxEvent
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_input_request import MCPInputRequestRepository
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.test_execution_creation import _create, _idem_key, _seed_ready
from tests.unit.test_execution_creation import _install_no_side_effects
from tests.unit.test_mrtr_waiting_input import _MrtrClient, _runner
from tests.unit.test_tool_runner import _resolver_factory


async def _enter_waiting_answered(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create WAITING_INPUT → ANSWERED + exactly one EXECUTION_MRTR_RESUME Outbox.

    Returns (execution_id, input_request_id, requester_id).
    """
    _install_no_side_effects(monkeypatch)
    async with session_factory() as session:
        seeded = await _seed_ready(session)
        created = await _create(session, seeded, idempotency_key=_idem_key())
        await session.commit()
        execution_id = created.result.id
        requester_id = seeded["requester_id"]

        await ExecutionQueueService(session).stage_created_batch(limit=10)
        await session.commit()
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id="pg-mrtr-setup"
        )
        assert claim.claimed and claim.lease_token is not None
        await session.commit()
        worker_id = "pg-mrtr-setup"
        lease_token = claim.lease_token

    outcome = await _runner(session_factory, _MrtrClient()).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.terminal_status == StepStatus.WAITING_INPUT.value

    async with session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        open_rows = await MCPInputRequestRepository(session).list_open_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        assert len(open_rows) == 1
        mir_id = open_rows[0].id

        answered = await MrtrResponseService(session).submit_response(
            execution_id=execution_id,
            input_request_id=mir_id,
            actor_user_id=requester_id,
            responses={"city": "Seoul", "units": "c"},
        )
        # submit_response commits; keep an explicit commit for fixture clarity.
        await session.commit()
        assert answered.resume_enqueued is True
        assert answered.status == McpInputRequestStatus.ANSWERED.value

        events = list(
            (
                await session.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == "EXECUTION_MRTR_RESUME",
                        OutboxEvent.aggregate_id == execution_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        return execution_id, mir_id, requester_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_concurrent_mrtr_resume_claim_one_winner(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, mir_id, _actor = await _enter_waiting_answered(
        integration_session_factory, monkeypatch
    )

    async def claim_once(worker: str) -> dict[str, Any]:
        async with integration_session_factory() as session:
            outcome = await MrtrResumeClaimService(session, lease_seconds=60).claim(
                execution_id=execution_id,
                input_request_id=mir_id,
                worker_id=worker,
            )
            await session.commit()
            return {
                "worker": worker,
                "claimed": outcome.claimed,
                "reason": outcome.reason,
                "lease_token": outcome.lease_token,
                "status": outcome.status,
            }

    results = await asyncio.gather(claim_once("worker-a"), claim_once("worker-b"))
    winners = [r for r in results if r["claimed"]]
    losers = [r for r in results if not r["claimed"]]
    assert len(winners) == 1
    assert len(losers) == 1
    assert losers[0]["reason"] == "STALE_DELIVERY"
    assert losers[0]["lease_token"] is None

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.RUNNING.value
        assert execution.worker_id == winners[0]["worker"]
        assert execution.lease_token == winners[0]["lease_token"]
        assert execution.lease_expires_at is not None
        assert execution.heartbeat_at is not None

        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.RUNNING.value

        # Losing delivery must not create a second ToolCall / MCP round.
        attempt = (await ExecutionRepository(session).list_attempts(step.id))[0]
        tool_calls = await ExecutionRepository(session).list_tool_calls(attempt.id)
        assert len(tool_calls) == 1
        assert (
            tool_calls[0].normalized_status
            == ToolCallNormalizedStatus.SUCCEEDED.value
        )
        assert not any(
            tc.normalized_status == ToolCallNormalizedStatus.STARTED.value
            for tc in tool_calls
        )

    # Loser cannot drive Runner with a missing lease — zero additional MCP.
    class _NeverCalled:
        async def call_tool(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("loser must not invoke MCP")

    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=_NeverCalled(),
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    # Fake lease from loser path — claim did not grant one.
    fake_token = uuid.uuid4()
    loser_outcome = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id=losers[0]["worker"],
        lease_token=fake_token,
    )
    assert loser_outcome.mcp_called is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_concurrent_duplicate_response_one_outbox(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identical concurrent responses: one ANSWERED + one resume Outbox."""
    _install_no_side_effects(monkeypatch)
    async with integration_session_factory() as session:
        seeded = await _seed_ready(session)
        created = await _create(session, seeded, idempotency_key=_idem_key())
        await session.commit()
        execution_id = created.result.id
        requester_id = seeded["requester_id"]
        await ExecutionQueueService(session).stage_created_batch(limit=10)
        await session.commit()
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id="pg-mrtr-dup"
        )
        assert claim.claimed and claim.lease_token is not None
        await session.commit()
        worker_id = "pg-mrtr-dup"
        lease_token = claim.lease_token

    outcome = await _runner(integration_session_factory, _MrtrClient()).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.terminal_status == StepStatus.WAITING_INPUT.value

    async with integration_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        mir = (
            await MCPInputRequestRepository(session).list_open_for_step(
                execution_id=execution_id, step_execution_id=step.id
            )
        )[0]
        mir_id = mir.id

    responses = {"city": "Seoul", "units": "c"}

    async def respond_once() -> dict[str, Any]:
        async with integration_session_factory() as session:
            try:
                result = await MrtrResponseService(session).submit_response(
                    execution_id=execution_id,
                    input_request_id=mir_id,
                    actor_user_id=requester_id,
                    responses=responses,
                )
                await session.commit()
                return {
                    "ok": True,
                    "resume_enqueued": result.resume_enqueued,
                    "status": result.status,
                }
            except AppError as exc:
                await session.rollback()
                return {"ok": False, "code": exc.code}

    results = await asyncio.gather(respond_once(), respond_once())
    assert all(r["ok"] for r in results)
    # Exactly one path enqueues resume; the identical duplicate is a safe no-op.
    assert [r["resume_enqueued"] for r in results].count(True) == 1
    assert [r["resume_enqueued"] for r in results].count(False) == 1
    assert all(r["status"] == McpInputRequestStatus.ANSWERED.value for r in results)

    async with integration_session_factory() as session:
        mir = await MCPInputRequestRepository(session).get(mir_id)
        assert mir is not None
        assert mir.status == McpInputRequestStatus.ANSWERED.value
        assert mir.response_payload == responses
        events = list(
            (
                await session.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == "EXECUTION_MRTR_RESUME",
                        OutboxEvent.aggregate_id == execution_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        open_rows = await MCPInputRequestRepository(session).list_open_for_step(
            execution_id=execution_id,
            step_execution_id=(
                await ExecutionRepository(session).list_steps(execution_id)
            )[0].id,
        )
        assert open_rows == []
