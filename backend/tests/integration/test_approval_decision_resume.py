"""PostgreSQL integration: decision unique, concurrency, resume dedupe, migration 0018."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from app.approval.decision import ApprovalDecisionService
from app.core.secrets import UnimplementedSecretResolver
from app.domain.enums import ApprovalStatus, ExecutionStatus, StepStatus, UserStatus
from app.execution.approval_resume import ApprovalResumeClaimService
from app.execution.claim import ExecutionClaimService
from app.execution.queue import ExecutionQueueService
from app.execution.tool_runner import McpToolRunner
from app.models.outbox import OutboxEvent
from app.repositories.approval_decision import ApprovalDecisionRepository
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.approval_request import ApprovalRequestRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.role import (
    PermissionRepository,
    RolePermissionRepository,
    RoleRepository,
    UserRoleRepository,
)
from app.repositories.user import UserRepository
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.test_execution_creation import _create, _idem_key, _seed_ready


def _cfg(url: str) -> Config:
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    cfg = Config(os.path.join(backend_dir, "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    os.environ["MCPFLOW_DATABASE_URL"] = url
    from app.core.config import get_settings

    get_settings.cache_clear()
    return cfg


def _unimplemented_resolver_factory(_session: AsyncSession) -> UnimplementedSecretResolver:
    return UnimplementedSecretResolver()


class _NeverCalledMCPClient:
    async def call_tool(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("MCP call_tool must not be invoked for this scenario")


async def _grant_decide(session: AsyncSession, user_id: uuid.UUID) -> None:
    perm = await PermissionRepository(session).get_by_code("approval.decide")
    assert perm is not None
    role = await RoleRepository(session).create(
        code=f"DEC_{uuid.uuid4().hex[:6].upper()}",
        name="Decider",
        description=None,
    )
    await RolePermissionRepository(session).replace_all(role.id, [perm.id])
    # Append — do not wipe existing roles (requester still needs mcp.tool.execute).
    existing = await UserRoleRepository(session).list_role_ids(user_id)
    if role.id not in existing:
        await UserRoleRepository(session).replace_all(user_id, [*existing, role.id])
    await session.flush()


async def _enter_waiting_pg(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    decision_mode: str = "ALL",
    required_approvals: int = 2,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with session_factory() as session:
        approval = await ApprovalPolicyRepository(session).create(
            code=f"ap-{uuid.uuid4().hex[:8]}",
            name="PG Gate",
            decision_mode=decision_mode,
            required_approvals=required_approvals,
            allow_self_approval=True,
            # Use {} (SQL JSON object) — SQL NULL vs JSON null is ambiguous for CHECK.
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
            execution_id=execution_id, worker_id="pg-resume-worker"
        )
        assert claim.claimed and claim.lease_token is not None
        await session.commit()
        worker_id = "pg-resume-worker"
        lease_token = claim.lease_token

    runner = McpToolRunner(
        session_factory=session_factory,
        mcp_client=_NeverCalledMCPClient(),
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.terminal_status == StepStatus.WAITING_APPROVAL.value

    async with session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        pending = await ApprovalRequestRepository(session).find_pending_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        assert pending is not None
        return execution_id, pending.id, requester_id


@pytest.mark.integration
def test_alembic_0018_round_trip(integration_database_url: str) -> None:
    cfg = _cfg(integration_database_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "20260922_0017")
    command.upgrade(cfg, "head")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_decision_unique_and_schema(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT indexname FROM pg_indexes
                    WHERE tablename = 'approval_decisions'
                      AND indexdef ILIKE '%approval_request_id%'
                      AND indexdef ILIKE '%decided_by%'
                      AND indexdef ILIKE '%UNIQUE%'
                    """
                )
            )
        ).scalars().all()
        assert rows
        pending_idx = (
            await session.execute(
                text(
                    """
                    SELECT indexname FROM pg_indexes
                    WHERE tablename = 'approval_requests'
                      AND indexname = 'ix_approval_requests_pending_expires_at'
                    """
                )
            )
        ).scalars().all()
        assert pending_idx


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_threshold_one_outbox(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    execution_id, approval_id, _requester = await _enter_waiting_pg(
        integration_session_factory
    )
    async with integration_session_factory() as session:
        a1 = await UserRepository(session).create(
            username=f"c1_{uuid.uuid4().hex[:8]}",
            display_name="C1",
            email=f"c1_{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE.value,
        )
        a2 = await UserRepository(session).create(
            username=f"c2_{uuid.uuid4().hex[:8]}",
            display_name="C2",
            email=f"c2_{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE.value,
        )
        await _grant_decide(session, a1.id)
        await _grant_decide(session, a2.id)
        await session.commit()
        actor1, actor2 = a1.id, a2.id

    async def _vote(actor: uuid.UUID) -> str:
        async with integration_session_factory() as session:
            outcome = await ApprovalDecisionService(session).decide(
                approval_id=approval_id, actor_user_id=actor, decision="APPROVE"
            )
            await session.commit()
            return outcome.approval_status

    results = await asyncio.gather(_vote(actor1), _vote(actor2))
    assert ApprovalStatus.APPROVED.value in results
    async with integration_session_factory() as session:
        request = await ApprovalRequestRepository(session).get(approval_id)
        assert request is not None
        assert request.status == ApprovalStatus.APPROVED.value
        decisions = await ApprovalDecisionRepository(session).list_for_request(
            approval_id
        )
        assert len(decisions) == 2
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_duplicate_resume_claim_noop(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    execution_id, approval_id, requester_id = await _enter_waiting_pg(
        integration_session_factory,
        decision_mode="ANY",
        required_approvals=1,
    )
    async with integration_session_factory() as session:
        await _grant_decide(session, requester_id)
        await session.commit()
        await ApprovalDecisionService(session).decide(
            approval_id=approval_id,
            actor_user_id=requester_id,
            decision="APPROVE",
        )
        await session.commit()

        first = await ApprovalResumeClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id,
            approval_request_id=approval_id,
            worker_id="w1",
        )
        await session.commit()
        assert first.claimed is True
        token = first.lease_token

        second = await ApprovalResumeClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id,
            approval_request_id=approval_id,
            worker_id="w2",
        )
        await session.commit()
        assert second.claimed is False
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.lease_token == token
        assert execution.worker_id == "w1"
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.READY.value
        assert execution.status == ExecutionStatus.RUNNING.value
