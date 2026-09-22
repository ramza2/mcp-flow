"""API tests for POST /approvals/{id}/decisions."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.approval.wait import ApprovalWaitService
from app.domain.enums import ApprovalStatus, UserStatus
from app.execution.tool_runner import McpToolRunner
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
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.api.test_auth_session import PASSWORD, _provision_user
from tests.unit.test_execution_creation import _install_no_side_effects
from tests.unit.test_tool_runner import _NeverCalledMCPClient
from tests.unit.test_tool_runner_security import (
    _claim_ready_execution,
    _unimplemented_resolver_factory,
)

APPROVALS = "/api/v1/approvals"


async def _login_as(
    client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: uuid.UUID,
    password: str = PASSWORD,
) -> None:
    async with db_session_factory() as session:
        user = await UserRepository(session).get(user_id)
        assert user is not None
        username = user.username
    login = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert login.status_code == 200, login.text
    csrf = await client.get("/api/v1/auth/csrf")
    assert csrf.status_code == 200
    client.headers["X-CSRF-Token"] = csrf.json()["csrf_token"]


async def _grant_decide(
    session: AsyncSession, user_id: uuid.UUID
) -> None:
    from app.auth.passwords import hash_password

    perm = await PermissionRepository(session).get_by_code("approval.decide")
    assert perm is not None
    role = await RoleRepository(session).create(
        code=f"DEC_{uuid.uuid4().hex[:6].upper()}",
        name="Decider",
        description=None,
    )
    await RolePermissionRepository(session).replace_all(role.id, [perm.id])
    existing = await UserRoleRepository(session).list_role_ids(user_id)
    if role.id not in existing:
        await UserRoleRepository(session).replace_all(user_id, [*existing, role.id])
    await UserRepository(session).set_password_hash(user_id, hash_password(PASSWORD))
    await session.flush()


@pytest.mark.asyncio
async def test_post_decision_approve_201(
    db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        approval = await ApprovalPolicyRepository(session).create(
            code=f"ap-{uuid.uuid4().hex[:8]}",
            name="API Gate",
            decision_mode="ANY",
            required_approvals=1,
            allow_self_approval=True,
        )
        await session.commit()
        approval_policy_id = approval.id

    execution_id, worker_id, lease_token = await _claim_ready_execution(
        db_session_factory,
        seeded_kwargs={
            "policy_requires_approval": True,
            "approval_policy_id": approval_policy_id,
        },
    )
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=_NeverCalledMCPClient(),
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        pending = await ApprovalRequestRepository(session).find_pending_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        assert pending is not None
        approval_id = pending.id
        await _grant_decide(session, execution.requester_id)
        await session.commit()
        requester_id = execution.requester_id

    await _login_as(db_client, db_session_factory, user_id=requester_id)
    resp = await db_client.post(
        f"{APPROVALS}/{approval_id}/decisions",
        json={"decision": "APPROVE"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["approval_status"] == ApprovalStatus.APPROVED.value
    assert body["resume_enqueued"] is True
    assert body["execution_status"] == "WAITING_APPROVAL"
    assert "context_hash" not in body


@pytest.mark.asyncio
async def test_post_decision_forbidden_without_permission(
    db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        approval = await ApprovalPolicyRepository(session).create(
            code=f"ap-{uuid.uuid4().hex[:8]}",
            name="API Gate",
            decision_mode="ANY",
            required_approvals=1,
        )
        await session.commit()
        approval_policy_id = approval.id

    execution_id, worker_id, lease_token = await _claim_ready_execution(
        db_session_factory,
        seeded_kwargs={
            "policy_requires_approval": True,
            "approval_policy_id": approval_policy_id,
        },
    )
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=_NeverCalledMCPClient(),
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )

    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        pending = await ApprovalRequestRepository(session).find_pending_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        assert pending is not None
        approval_id = pending.id
        stranger = await _provision_user(db_session_factory)
        stranger_id = uuid.UUID(stranger["id"])

    await _login_as(
        db_client, db_session_factory, user_id=stranger_id
    )
    resp = await db_client.post(
        f"{APPROVALS}/{approval_id}/decisions",
        json={"decision": "APPROVE"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "AUTH_FORBIDDEN"
