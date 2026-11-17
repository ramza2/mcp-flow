"""PostgreSQL: ToolPolicy requires_approval wait → decide → resume → Mock MCP.

Closes the PG gap where wait/Outbox/resume-claim were covered but the final
approved MCP call was only exercised on SQLite unit tests.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from app.approval.decision import ApprovalDecisionService
from app.core.secrets import UnimplementedSecretResolver
from app.domain.enums import ApprovalStatus, ExecutionStatus, StepStatus
from app.execution.approval_resume import ApprovalResumeClaimService
from app.execution.claim import ExecutionClaimService
from app.execution.queue import ExecutionQueueService
from app.execution.tool_runner import McpToolRunner
from app.mcp.contracts import NormalizedToolResult
from app.models.outbox import OutboxEvent
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.approval_request import ApprovalRequestRepository
from app.repositories.execution import ExecutionRepository
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.test_approval_decision_resume import _grant_decide
from tests.integration.test_execution_creation import _create, _idem_key, _seed_ready


def _resolver_factory(_session: AsyncSession) -> UnimplementedSecretResolver:
    return UnimplementedSecretResolver()


class _CountingMCPClient:
    def __init__(self) -> None:
        self.calls = 0

    async def call_tool(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        return (
            NormalizedToolResult(
                protocol_success=True,
                tool_error=False,
                structured_content={"ok": True, "n": self.calls},
                content=[{"type": "text", "text": "ok"}],
                raw_size_bytes=8,
                duration_ms=1,
            ),
            {"http_status": 200},
            datetime.now(UTC),
        )


async def _enter_toolpolicy_waiting(
    session_factory: async_sessionmaker[AsyncSession],
    client: _CountingMCPClient,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with session_factory() as session:
        approval = await ApprovalPolicyRepository(session).create(
            code=f"ap-tp-e2e-{uuid.uuid4().hex[:8]}",
            name="PG ToolPolicy Gate",
            decision_mode="ANY",
            required_approvals=1,
            allow_self_approval=True,
            approver_scope={},
        )
        await session.commit()
        seeded = await _seed_ready(
            session,
            policy_requires_approval=True,
            approval_policy_id=approval.id,
        )
        created = await _create(session, seeded, idempotency_key=_idem_key())
        await session.commit()
        execution_id = created.result.id
        requester_id = seeded["requester_id"]

        await ExecutionQueueService(session).stage_created_batch(limit=10)
        await session.commit()
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id="pg-tp-wait"
        )
        assert claim.claimed and claim.lease_token is not None
        await session.commit()
        worker_id = "pg-tp-wait"
        lease_token = claim.lease_token

    runner = McpToolRunner(
        session_factory=session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.terminal_status == StepStatus.WAITING_APPROVAL.value
    assert outcome.mcp_called is False
    assert client.calls == 0

    async with session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert await ExecutionRepository(session).list_attempts(step.id) == []
        pending = await ApprovalRequestRepository(session).find_pending_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        assert pending is not None
        assert pending.status == ApprovalStatus.PENDING.value
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.WAITING_APPROVAL.value
        assert execution.lease_token is None
        return execution_id, pending.id, requester_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_toolpolicy_approve_resume_mcp_once(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = _CountingMCPClient()
    execution_id, approval_id, requester_id = await _enter_toolpolicy_waiting(
        integration_session_factory, client
    )

    async with integration_session_factory() as session:
        await _grant_decide(session, requester_id)
        await session.commit()
        decided = await ApprovalDecisionService(session).decide(
            approval_id=approval_id,
            actor_user_id=requester_id,
            decision="APPROVE",
        )
        await session.commit()
        assert decided.approval_status == ApprovalStatus.APPROVED.value
        events = list(
            (
                await session.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == "EXECUTION_APPROVAL_RESUME",
                        OutboxEvent.aggregate_id == execution_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1

        resume = await ApprovalResumeClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id,
            approval_request_id=approval_id,
            worker_id="pg-tp-resume",
        )
        await session.commit()
        assert resume.claimed is True
        assert resume.lease_token is not None
        worker_id = resume.worker_id
        lease_token = resume.lease_token

        dup = await ApprovalResumeClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id,
            approval_request_id=approval_id,
            worker_id="pg-tp-resume-dup",
        )
        await session.commit()
        assert dup.claimed is False

    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value
    assert client.calls == 1

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.SUCCEEDED.value
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_toolpolicy_reject_blocks_mcp(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = _CountingMCPClient()
    execution_id, approval_id, requester_id = await _enter_toolpolicy_waiting(
        integration_session_factory, client
    )

    async with integration_session_factory() as session:
        await _grant_decide(session, requester_id)
        await session.commit()
        decided = await ApprovalDecisionService(session).decide(
            approval_id=approval_id,
            actor_user_id=requester_id,
            decision="REJECT",
            comment="blocked in PG e2e",
        )
        await session.commit()
        assert decided.approval_status == ApprovalStatus.REJECTED.value
        events = list(
            (
                await session.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == "EXECUTION_APPROVAL_RESUME",
                        OutboxEvent.aggregate_id == execution_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert events == []
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.FAILED.value
        assert await ExecutionRepository(session).list_attempts(step.id) == []
        assert client.calls == 0
