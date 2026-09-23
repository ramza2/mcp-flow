"""PostgreSQL integration: approval query JSONB scope + pagination totals."""

from __future__ import annotations

import json
import uuid

import pytest
from app.approval.decision import ApprovalDecisionService
from app.approval.query import ApprovalQueryService
from app.core.errors import AppError
from app.core.secrets import UnimplementedSecretResolver
from app.domain.enums import ApprovalStatus, StepStatus, UserStatus
from app.execution.claim import ExecutionClaimService
from app.execution.queue import ExecutionQueueService
from app.execution.tool_runner import McpToolRunner
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.approval_request import ApprovalRequestRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.user import UserRepository
from sqlalchemy import text
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


async def _set_requested_by(
    session: AsyncSession, *, approval_ids: list[uuid.UUID], owner_id: uuid.UUID
) -> None:
    """Re-home only test-owned ApprovalRequest rows under one requested_by filter."""
    for aid in approval_ids:
        await session.execute(
            text(
                "UPDATE approval_requests SET requested_by = :owner "
                "WHERE id = :id"
            ),
            {"owner": owner_id, "id": aid},
        )
    await session.flush()


async def _set_approval_scope(
    session: AsyncSession, *, approval_id: uuid.UUID, scope: object
) -> None:
    if scope is None:
        await session.execute(
            text(
                "UPDATE approval_requests SET approval_scope = NULL WHERE id = :id"
            ),
            {"id": approval_id},
        )
    else:
        await session.execute(
            text(
                "UPDATE approval_requests "
                "SET approval_scope = CAST(:scope AS jsonb) "
                "WHERE id = :id"
            ),
            {"scope": json.dumps(scope), "id": approval_id},
        )
    await session.flush()


@pytest.mark.asyncio
async def test_pg_jsonb_role_codes_visibility_and_pagination(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """JSONB role_codes + auth-before-page totals via test-owned requested_by filter."""
    suffix = uuid.uuid4().hex[:8].upper()
    role_match = f"ROLE_PG_Q_{suffix}"
    role_other = f"ROLE_PG_X_{suffix}"

    async with integration_session_factory() as session:
        owner = await UserRepository(session).create(
            username=f"owner_{uuid.uuid4().hex[:8]}",
            display_name="Query Owner",
            email=f"owner_{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE.value,
        )
        await session.commit()
        owner_id = owner.id

    eligible_ids: list[uuid.UUID] = []
    for _ in range(3):
        _e, aid, _r = await _enter_waiting_pg_scoped(
            integration_session_factory,
            role_codes=[role_match],
            allow_self_approval=False,
        )
        eligible_ids.append(aid)

    _e_wrong, wrong_id, _r_wrong = await _enter_waiting_pg_scoped(
        integration_session_factory,
        role_codes=[role_other],
        allow_self_approval=False,
    )

    _e_dec, decided_id, _r_dec = await _enter_waiting_pg_scoped(
        integration_session_factory,
        role_codes=[role_match],
        allow_self_approval=True,
        decision_mode="ALL",
        required_approvals=2,
    )

    owned_ids = [*eligible_ids, wrong_id, decided_id]

    async with integration_session_factory() as session:
        await _set_requested_by(session, approval_ids=owned_ids, owner_id=owner_id)
        actor = await _create_approver(session, role_code=role_match)
        await session.commit()
        await ApprovalDecisionService(session).decide(
            approval_id=decided_id,
            actor_user_id=actor,
            decision="APPROVE",
        )

    async with integration_session_factory() as session:
        decided = await ApprovalRequestRepository(session).get(decided_id)
        assert decided is not None
        assert decided.status == ApprovalStatus.PENDING.value
        match_row = await ApprovalRequestRepository(session).get(eligible_ids[0])
        assert match_row is not None
        assert match_row.approval_scope == {"role_codes": [role_match]}

        page1 = await ApprovalQueryService(session).list_for_actor(
            actor_user_id=actor,
            requested_by=owner_id,
            page=1,
            page_size=2,
            sort="expires_at",
        )
        assert page1.total == 3
        assert len(page1.items) == 2
        assert page1.has_next is True

        page2 = await ApprovalQueryService(session).list_for_actor(
            actor_user_id=actor,
            requested_by=owner_id,
            page=2,
            page_size=2,
            sort="expires_at",
        )
        assert page2.total == 3
        assert len(page2.items) == 1
        assert page2.has_next is False

        visible = {item.id for item in page1.items} | {item.id for item in page2.items}
        assert set(eligible_ids) == visible
        assert wrong_id not in visible
        assert decided_id not in visible

        with pytest.raises(AppError) as exc:
            await ApprovalQueryService(session).get_for_actor(
                approval_id=wrong_id, actor_user_id=actor
            )
        assert exc.value.status_code == 404

    # Open scope {} visibility (membership via execution_id — ignores DB pollution).
    open_exec, open_id, _open_req = await _enter_waiting_pg_scoped(
        integration_session_factory,
        role_codes=None,
        allow_self_approval=True,
    )
    async with integration_session_factory() as session:
        open_list = await ApprovalQueryService(session).list_for_actor(
            actor_user_id=actor,
            execution_id=open_exec,
        )
        assert open_list.total == 1
        assert open_list.items[0].id == open_id
        assert open_list.items[0].can_decide is True


@pytest.mark.asyncio
async def test_pg_scope_semantics_match_validate_approver_scope(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A–E: list/detail visibility matches validate_approver_scope fail-closed rules."""
    suffix = uuid.uuid4().hex[:8].upper()
    role_a = f"ROLE_A_{suffix}"

    cases: list[tuple[str, object, bool]] = [
        ("mixed_non_string", {"role_codes": [role_a, 123]}, False),
        ("empty_string", {"role_codes": [""]}, False),
        ("blank_whitespace", {"role_codes": ["   "]}, False),
        ("trimmed_match", {"role_codes": [f" {role_a} "]}, True),
        ("extra_key", {"role_codes": [role_a], "legacy": True}, False),
        ("open_null", None, True),
        ("open_empty", {}, True),
    ]

    created: list[tuple[str, uuid.UUID, bool]] = []
    for label, scope, expect_visible in cases:
        _e, aid, _r = await _enter_waiting_pg_scoped(
            integration_session_factory,
            role_codes=[role_a],
            allow_self_approval=True,
        )
        async with integration_session_factory() as session:
            await _set_approval_scope(session, approval_id=aid, scope=scope)
            await session.commit()
        created.append((label, aid, expect_visible))

    async with integration_session_factory() as session:
        actor = await _create_approver(session, role_code=role_a)
        await session.commit()

        inbox = await ApprovalQueryService(session).list_for_actor(actor_user_id=actor)
        inbox_ids = {item.id for item in inbox.items}

        for label, aid, expect_visible in created:
            if expect_visible:
                assert aid in inbox_ids, label
                detail = await ApprovalQueryService(session).get_for_actor(
                    approval_id=aid, actor_user_id=actor
                )
                assert detail.item.id == aid, label
            else:
                assert aid not in inbox_ids, label
                with pytest.raises(AppError) as exc:
                    await ApprovalQueryService(session).get_for_actor(
                        approval_id=aid, actor_user_id=actor
                    )
                assert exc.value.status_code == 404, label
                assert exc.value.code == "NOT_FOUND", label
