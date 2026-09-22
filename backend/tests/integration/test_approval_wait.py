"""PostgreSQL integration: approval_requests concurrency + migration round-trip."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from app.domain.enums import ApprovalStatus, ExecutionStatus, StepStatus
from app.execution.tool_runner import McpToolRunner
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.execution import ExecutionRepository
from app.core.secrets import UnimplementedSecretResolver
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.test_execution_creation import _seed_ready
from tests.integration.test_mcp_tool_runner import _claim_ready_execution


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


@pytest.mark.integration
def test_alembic_approval_requests_downgrade_upgrade(integration_database_url: str) -> None:
    cfg = _cfg(integration_database_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "20260918_0016")
    command.upgrade(cfg, "head")


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

    execution_id, worker_id, lease_token = await _claim_ready_execution(
        integration_session_factory, seeded=seeded, worker_id="pg-approval-worker"
    )

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
