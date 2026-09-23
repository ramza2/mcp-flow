"""PostgreSQL integration: approval query JSONB scope + pagination totals."""

from __future__ import annotations

import uuid

import pytest
from app.approval.decision import ApprovalDecisionService
from app.approval.query import ApprovalQueryService
from app.core.errors import AppError
from app.core.secrets import UnimplementedSecretResolver
from app.domain.enums import ApprovalStatus, StepStatus
from app.execution.claim import ExecutionClaimService
from app.execution.queue import ExecutionQueueService
from app.execution.tool_runner import McpToolRunner
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.approval_request import ApprovalRequestRepository
from app.repositories.execution import ExecutionRepository
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.test_execution_creation import _create, _idem_key, _seed_ready
from tests.unit.test_approval_decision_resume import _create_approver

pytestmark = pytest.mark.integration


def _unimplemented_resolver_factory(_session: AsyncSession) -> UnimplementedSecretResolver:
    return UnimplementedSecretResolver()


class _NeverCalledMCPClient:
    async def call_tool(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("MCP call_tool must not be invoked for this scenario")


async def _enter_waiting_pg_scoped(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    role_codes: list[str] | None = None,
    allow_self_approval: bool = True,
    decision_mode: str = "ANY",
    required_approvals: int = 1,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    worker_id = f"pg-query-{uuid.uuid4().hex[:8]}"
    async with session_factory() as session:
        scope: dict | None = (
            {} if role_codes is None else {"role_codes": role_codes}
        )
        approval = await ApprovalPolicyRepository(session).create(
            code=f"ap-q-{uuid.uuid4().hex[:8]}",
            name="Query Gate",
            decision_mode=decision_mode,
            required_approvals=required_approvals,
            allow_self_approval=allow_self_approval,
            approver_scope=scope,
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
            execution_id=execution_id, worker_id=worker_id
        )
        assert claim.claimed and claim.lease_token is not None
        await session.commit()
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


@pytest.mark.asyncio
async def test_pg_jsonb_role_codes_visibility_and_pagination(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _e1, match_id, _r1 = await _enter_waiting_pg_scoped(
        integration_session_factory,
        role_codes=["ROLE_PG_APPROVER"],
        allow_self_approval=False,
    )
    _e2, wrong_id, _r2 = await _enter_waiting_pg_scoped(
        integration_session_factory,
        role_codes=["ROLE_PG_OTHER"],
        allow_self_approval=False,
    )
    _e3, open_id, _r3 = await _enter_waiting_pg_scoped(
        integration_session_factory,
        role_codes=None,
        allow_self_approval=True,
        decision_mode="ALL",
        required_approvals=2,
    )
    _e4, open_id2, _r4 = await _enter_waiting_pg_scoped(
        integration_session_factory,
        role_codes=None,
        allow_self_approval=True,
    )

    async with integration_session_factory() as session:
        actor = await _create_approver(session, role_code="ROLE_PG_APPROVER")
        await session.commit()
        await ApprovalDecisionService(session).decide(
            approval_id=open_id,
            actor_user_id=actor,
            decision="APPROVE",
        )

    async with integration_session_factory() as session:
        request = await ApprovalRequestRepository(session).get(open_id)
        assert request is not None
        assert request.status == ApprovalStatus.PENDING.value
        match_row = await ApprovalRequestRepository(session).get(match_id)
        assert match_row is not None
        assert match_row.approval_scope == {"role_codes": ["ROLE_PG_APPROVER"]}

        result = await ApprovalQueryService(session).list_for_actor(
            actor_user_id=actor, page=1, page_size=2, sort="expires_at"
        )
        assert result.total == 2
        assert len(result.items) == 2
        assert result.has_next is False
        visible_ids = {item.id for item in result.items}
        assert match_id in visible_ids
        assert open_id2 in visible_ids
        assert wrong_id not in visible_ids
        assert open_id not in visible_ids

        with pytest.raises(AppError) as exc:
            await ApprovalQueryService(session).get_for_actor(
                approval_id=wrong_id, actor_user_id=actor
            )
        assert exc.value.status_code == 404
