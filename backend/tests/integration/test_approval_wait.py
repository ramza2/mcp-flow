"""PostgreSQL integration: approval_requests concurrency + migration + CHECKs."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from app.core.secrets import UnimplementedSecretResolver
from app.domain.enums import ApprovalStatus, ExecutionStatus, StepStatus
from app.execution.claim import ExecutionClaimService
from app.execution.queue import ExecutionQueueService
from app.execution.tool_runner import McpToolRunner
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.execution import ExecutionRepository
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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


async def _table_presence(url: str) -> dict[str, bool]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            present: dict[str, bool] = {}
            for name in (
                "approval_requests",
                "approval_decisions",
                "tool_calls",
                "secret_records",
                "executions",
                "approval_policies",
            ):
                result = await conn.execute(
                    text("SELECT to_regclass(:n)"), {"n": f"public.{name}"}
                )
                present[name] = result.scalar() is not None
            return present
    finally:
        await engine.dispose()


@pytest.mark.integration
def test_alembic_approval_requests_downgrade_upgrade(integration_database_url: str) -> None:
    cfg = _cfg(integration_database_url)
    command.upgrade(cfg, "head")
    tables = asyncio.run(_table_presence(integration_database_url))
    assert tables["approval_requests"]
    assert tables["approval_decisions"]
    assert tables["tool_calls"]
    assert tables["secret_records"]

    command.downgrade(cfg, "20260918_0016")
    tables = asyncio.run(_table_presence(integration_database_url))
    assert not tables["approval_requests"]
    assert not tables["approval_decisions"]
    assert tables["tool_calls"]
    assert tables["secret_records"]
    assert tables["executions"]
    assert tables["approval_policies"]

    command.upgrade(cfg, "head")
    tables = asyncio.run(_table_presence(integration_database_url))
    assert tables["approval_requests"]
    assert tables["approval_decisions"]
    assert tables["tool_calls"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_approval_requests_schema_and_pending_unique(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        columns = (
            (
                await session.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema='public' AND table_name='approval_requests'
                        ORDER BY column_name
                        """
                    )
                )
            )
            .scalars()
            .all()
        )
        for required in (
            "id",
            "execution_id",
            "step_execution_id",
            "approval_policy_id",
            "status",
            "decision_mode",
            "required_approvals",
            "approval_scope",
            "context_snapshot",
            "context_hash",
            "requested_at",
            "expires_at",
            "resolved_at",
            "requested_by",
            "lock_version",
        ):
            assert required in columns

        decision_columns = (
            (
                await session.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema='public' AND table_name='approval_decisions'
                        ORDER BY column_name
                        """
                    )
                )
            )
            .scalars()
            .all()
        )
        for required in (
            "id",
            "approval_request_id",
            "decided_by",
            "decision",
            "comment",
            "context_hash",
            "decided_at",
        ):
            assert required in decision_columns

        indexes = (
            (
                await session.execute(
                    text(
                        """
                        SELECT indexname FROM pg_indexes
                        WHERE tablename = 'approval_requests'
                        ORDER BY indexname
                        """
                    )
                )
            )
            .scalars()
            .all()
        )
        assert any("execution_id" in name for name in indexes)
        assert any("step_execution_id" in name for name in indexes)
        assert any("status" in name for name in indexes)
        assert any("expires_at" in name for name in indexes)
        assert any("pending_execution_step" in name for name in indexes)

        idx_def = (
            await session.execute(
                text(
                    """
                    SELECT indexdef FROM pg_indexes
                    WHERE indexname = 'uq_approval_requests_pending_execution_step'
                    """
                )
            )
        ).scalar_one()
        assert "PENDING" in idx_def
        assert "UNIQUE" in idx_def.upper()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_approval_requests_check_and_fk_constraints(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        approval = await ApprovalPolicyRepository(session).create(
            code=f"ap-{uuid.uuid4().hex[:8]}",
            name="PG Constraint Gate",
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
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        requester_id = seeded["requester_id"]
        approval_id = approval.id
        step_id = step.id

    now = datetime.now(UTC)
    base = {
        "id": uuid.uuid4(),
        "execution_id": execution_id,
        "step_execution_id": step_id,
        "approval_policy_id": approval_id,
        "status": "PENDING",
        "decision_mode": "ANY",
        "required_approvals": 1,
        "context_snapshot": "{}",
        "context_hash": "b" * 64,
        "requested_at": now,
        "expires_at": now + timedelta(hours=1),
        "resolved_at": None,
        "requested_by": requester_id,
        "lock_version": 1,
    }

    insert_sql = text(
        """
        INSERT INTO approval_requests (
            id, execution_id, step_execution_id, approval_policy_id,
            status, decision_mode, required_approvals, approval_scope,
            context_snapshot, context_hash, requested_at, expires_at,
            resolved_at, requested_by, lock_version
        ) VALUES (
            :id, :execution_id, :step_execution_id, :approval_policy_id,
            :status, :decision_mode, :required_approvals, NULL,
            CAST(:context_snapshot AS jsonb), :context_hash, :requested_at, :expires_at,
            :resolved_at, :requested_by, :lock_version
        )
        """
    )

    async def _expect_integrity(params: dict) -> None:
        async with integration_session_factory() as session:
            with pytest.raises(IntegrityError):
                await session.execute(insert_sql, params)
                await session.commit()
            await session.rollback()

    # invalid status
    await _expect_integrity({**base, "id": uuid.uuid4(), "status": "WAITING_APPROVAL"})
    # invalid decision_mode
    await _expect_integrity({**base, "id": uuid.uuid4(), "decision_mode": "MAJORITY"})
    # required_approvals < 1
    await _expect_integrity({**base, "id": uuid.uuid4(), "required_approvals": 0})
    # malformed context_hash
    await _expect_integrity({**base, "id": uuid.uuid4(), "context_hash": "not-a-hash"})
    # PENDING + resolved_at non-null
    await _expect_integrity(
        {
            **base,
            "id": uuid.uuid4(),
            "status": "PENDING",
            "resolved_at": now,
        }
    )
    # FK execution_id
    await _expect_integrity({**base, "id": uuid.uuid4(), "execution_id": uuid.uuid4()})
    # FK step_execution_id
    await _expect_integrity(
        {**base, "id": uuid.uuid4(), "step_execution_id": uuid.uuid4()}
    )
    # FK approval_policy_id
    await _expect_integrity(
        {**base, "id": uuid.uuid4(), "approval_policy_id": uuid.uuid4()}
    )
    # FK requested_by
    await _expect_integrity({**base, "id": uuid.uuid4(), "requested_by": uuid.uuid4()})

    # Valid PENDING then partial unique
    async with integration_session_factory() as session:
        await session.execute(insert_sql, {**base, "id": uuid.uuid4()})
        await session.commit()

    await _expect_integrity({**base, "id": uuid.uuid4()})

    # approval_decisions constraints
    async with integration_session_factory() as session:
        request_id = (
            await session.execute(
                text(
                    """
                    SELECT id FROM approval_requests
                    WHERE execution_id = :execution_id
                    LIMIT 1
                    """
                ),
                {"execution_id": execution_id},
            )
        ).scalar_one()

    decision_sql = text(
        """
        INSERT INTO approval_decisions (
            id, approval_request_id, decided_by, decision, comment,
            context_hash, decided_at
        ) VALUES (
            :id, :approval_request_id, :decided_by, :decision, :comment,
            :context_hash, :decided_at
        )
        """
    )
    decision_base = {
        "id": uuid.uuid4(),
        "approval_request_id": request_id,
        "decided_by": requester_id,
        "decision": "APPROVE",
        "comment": None,
        "context_hash": "c" * 64,
        "decided_at": now,
    }

    async def _expect_decision_integrity(params: dict) -> None:
        async with integration_session_factory() as session:
            with pytest.raises(IntegrityError):
                await session.execute(decision_sql, params)
                await session.commit()
            await session.rollback()

    await _expect_decision_integrity(
        {**decision_base, "id": uuid.uuid4(), "decision": "MAYBE"}
    )
    await _expect_decision_integrity(
        {**decision_base, "id": uuid.uuid4(), "context_hash": "ZZ"}
    )
    await _expect_decision_integrity(
        {**decision_base, "id": uuid.uuid4(), "approval_request_id": uuid.uuid4()}
    )
    await _expect_decision_integrity(
        {**decision_base, "id": uuid.uuid4(), "decided_by": uuid.uuid4()}
    )

    async with integration_session_factory() as session:
        await session.execute(decision_sql, {**decision_base, "id": uuid.uuid4()})
        await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_approval_wait_exactly_one_pending(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        approval = await ApprovalPolicyRepository(session).create(
            code=f"ap-{uuid.uuid4().hex[:8]}",
            name="PG Concurrent Gate",
            decision_mode="ANY",
            required_approvals=1,
            default_expiry_seconds=3600,
            approver_scope={"roles": ["approver"]},
        )
        await session.commit()
        approval_id = approval.id
        seeded = await _seed_ready(
            session,
            policy_requires_approval=True,
            approval_policy_id=approval_id,
        )
        created = await _create(session, seeded, idempotency_key=_idem_key())
        await session.commit()
        execution_id = created.result.id

    async with integration_session_factory() as session:
        staged = await ExecutionQueueService(session).stage_created_batch(limit=50)
        assert staged >= 1
        await session.commit()

    worker_id = "pg-approval-worker"
    async with integration_session_factory() as session:
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id=worker_id
        )
        assert claim.claimed is True
        assert claim.lease_token is not None
        await session.commit()
        lease_token = claim.lease_token

    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=_NeverCalledMCPClient(),  # type: ignore[arg-type]
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    outcomes = await asyncio.gather(
        runner.run_claimed_execution(
            execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
        ),
        runner.run_claimed_execution(
            execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
        ),
    )
    assert all(o.mcp_called is False for o in outcomes)
    assert any(
        o.terminal_status == StepStatus.WAITING_APPROVAL.value
        and o.reason == "WAITING_APPROVAL"
        for o in outcomes
    )

    async with integration_session_factory() as session:
        from app.models.approval import ApprovalRequest

        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        count = (
            await session.execute(
                select(func.count())
                .select_from(ApprovalRequest)
                .where(
                    ApprovalRequest.execution_id == execution_id,
                    ApprovalRequest.step_execution_id == step.id,
                    ApprovalRequest.status == ApprovalStatus.PENDING.value,
                )
            )
        ).scalar_one()
        assert count == 1
        assert (await ExecutionRepository(session).list_attempts(step.id)) == []
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.WAITING_APPROVAL.value
        assert execution.worker_id is None
        assert execution.lease_token is None
