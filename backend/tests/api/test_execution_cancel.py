"""API tests for POST /executions/{id}/cancel."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.enums import (
    ExecutionStatus,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
    UserStatus,
)
from app.execution.claim import ExecutionClaimService
from app.execution.queue import ExecutionQueueService
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.role import PermissionRepository
from app.repositories.user import UserRepository
from app.schemas.auth import (
    RoleCreate,
    RolePermissionReplaceRequest,
    UserRoleReplaceRequest,
)
from app.services.role import RoleService
from app.services.user import UserService

from tests.unit.test_execution_creation import _create, _idem_key, _seed_ready

API = "/api/v1/executions"


async def _login_cancel_user(
    client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    requester_id: uuid.UUID,
) -> None:
    from app.auth.passwords import hash_password

    password = "correct-horse-battery-staple"
    async with db_session_factory() as session:
        user = await UserRepository(session).get(requester_id)
        assert user is not None
        await UserRepository(session).set_password_hash(
            user.id, hash_password(password)
        )
        cancel = await PermissionRepository(session).get_by_code("execution.cancel")
        assert cancel is not None
        role = await RoleService(session).create(
            RoleCreate(code=f"cx-{uuid.uuid4().hex[:8]}", name="Cancel")
        )
        await RoleService(session).replace_permissions(
            role.id,
            RolePermissionReplaceRequest(permission_ids=[cancel.id]),
            expected_lock_version=1,
        )
        user = await UserRepository(session).get(user.id)
        assert user is not None
        await UserService(session).replace_roles(
            user.id,
            UserRoleReplaceRequest(role_ids=[role.id]),
            expected_lock_version=int(user.lock_version),
        )
        await session.commit()
        username = user.username

    login = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert login.status_code == 200, login.text
    csrf = await client.get("/api/v1/auth/csrf")
    assert csrf.status_code == 200
    client.headers["X-CSRF-Token"] = csrf.json()["csrf_token"]


async def _seed_started_toolcall(
    session: AsyncSession, execution_id: uuid.UUID, worker_id: str = "w1"
) -> None:
    claim = await ExecutionClaimService(session, lease_seconds=60).claim(
        execution_id=execution_id, worker_id=worker_id
    )
    assert claim.claimed
    executions = ExecutionRepository(session)
    steps = await executions.list_steps(execution_id)
    step = steps[0]
    step.status = StepStatus.RUNNING.value
    step.started_at = datetime.now(UTC)
    step.attempt_count = 1
    attempt = await executions.create_attempt(
        step_execution_id=step.id,
        attempt_no=1,
        status=StepAttemptStatus.STARTED.value,
        worker_id=worker_id,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        idempotency_key=f"idem-{uuid.uuid4().hex}",
        request_snapshot={"ok": True},
        started_at=datetime.now(UTC),
    )
    version = await MCPToolRepository(session).get_version(step.mcp_tool_version_id)
    assert version is not None
    tool = await MCPToolRepository(session).get(version.mcp_tool_id)
    assert tool is not None
    await executions.create_tool_call(
        step_attempt_id=attempt.id,
        mcp_server_id=tool.mcp_server_id,
        mcp_tool_version_id=step.mcp_tool_version_id,
        protocol_era="CURRENT",
        protocol_version="2026-07-28",
        transport_type="STREAMABLE_HTTP",
        remote_request_id=str(uuid.uuid4()),
        request_meta={},
        normalized_status=ToolCallNormalizedStatus.STARTED.value,
        started_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_cancel_created_execution(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = unauthenticated_db_client
    async with db_session_factory() as session:
        seeded = await _seed_ready(session)
        outcome = await _create(session, seeded, idempotency_key=_idem_key())
        execution_id = outcome.result.id
        requester_id = seeded["requester_id"]
        await session.commit()

    await _login_cancel_user(client, db_session_factory, requester_id=requester_id)
    response = await client.post(
        f"{API}/{execution_id}/cancel",
        json={"reason": "사용자 요청"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "CANCELLED"
    assert body["cancel_requested_at"] is not None
    assert body["finished_at"] is not None


@pytest.mark.asyncio
async def test_cancel_reason_too_long_422(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = unauthenticated_db_client
    async with db_session_factory() as session:
        seeded = await _seed_ready(session)
        outcome = await _create(session, seeded, idempotency_key=_idem_key())
        execution_id = outcome.result.id
        requester_id = seeded["requester_id"]
        await session.commit()
    await _login_cancel_user(client, db_session_factory, requester_id=requester_id)
    response = await client.post(
        f"{API}/{execution_id}/cancel",
        json={"reason": "x" * 501},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_cancel_other_requester_404(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = unauthenticated_db_client
    async with db_session_factory() as session:
        seeded = await _seed_ready(session)
        outcome = await _create(session, seeded, idempotency_key=_idem_key())
        execution_id = outcome.result.id
        await session.commit()

        other = await UserRepository(session).create(
            username=f"cxo-{uuid.uuid4().hex[:8]}",
            display_name="Other",
            email=f"cxo-{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE,
        )
        await session.commit()
        other_id = other.id

    await _login_cancel_user(client, db_session_factory, requester_id=other_id)
    response = await client.post(f"{API}/{execution_id}/cancel", json={})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_cancel_missing_permission_403(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.auth.passwords import hash_password

    client = unauthenticated_db_client
    async with db_session_factory() as session:
        seeded = await _seed_ready(session)
        outcome = await _create(session, seeded, idempotency_key=_idem_key())
        execution_id = outcome.result.id
        user = await UserRepository(session).get(seeded["requester_id"])
        assert user is not None
        await UserRepository(session).set_password_hash(
            user.id, hash_password("correct-horse-battery-staple")
        )
        await session.commit()
        username = user.username

    login = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "correct-horse-battery-staple"},
    )
    assert login.status_code == 200
    csrf = await client.get("/api/v1/auth/csrf")
    client.headers["X-CSRF-Token"] = csrf.json()["csrf_token"]
    response = await client.post(f"{API}/{execution_id}/cancel", json={})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_cancel_inactive_user_403(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.auth.passwords import hash_password

    client = unauthenticated_db_client
    async with db_session_factory() as session:
        seeded = await _seed_ready(session)
        outcome = await _create(session, seeded, idempotency_key=_idem_key())
        execution_id = outcome.result.id
        user = await UserRepository(session).get(seeded["requester_id"])
        assert user is not None
        await UserRepository(session).set_password_hash(
            user.id, hash_password("correct-horse-battery-staple")
        )
        cancel = await PermissionRepository(session).get_by_code("execution.cancel")
        assert cancel is not None
        role = await RoleService(session).create(
            RoleCreate(code=f"cx-i-{uuid.uuid4().hex[:8]}", name="Cancel")
        )
        await RoleService(session).replace_permissions(
            role.id,
            RolePermissionReplaceRequest(permission_ids=[cancel.id]),
            expected_lock_version=1,
        )
        user = await UserRepository(session).get(user.id)
        assert user is not None
        await UserService(session).replace_roles(
            user.id,
            UserRoleReplaceRequest(role_ids=[role.id]),
            expected_lock_version=int(user.lock_version),
        )
        await session.commit()
        username = user.username
        user_id = user.id

    login = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "correct-horse-battery-staple"},
    )
    assert login.status_code == 200
    csrf = await client.get("/api/v1/auth/csrf")
    client.headers["X-CSRF-Token"] = csrf.json()["csrf_token"]

    async with db_session_factory() as session:
        user = await UserRepository(session).get(user_id)
        assert user is not None
        user.status = UserStatus.INACTIVE.value
        await session.commit()

    # Inactive users fail session resolution (AUTH_SESSION_INVALID) before
    # ExecutionCancellationService; service-layer ACTIVE check is unit-covered.
    response = await client.post(f"{API}/{execution_id}/cancel", json={})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_cancel_csrf_required(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = unauthenticated_db_client
    async with db_session_factory() as session:
        seeded = await _seed_ready(session)
        outcome = await _create(session, seeded, idempotency_key=_idem_key())
        execution_id = outcome.result.id
        requester_id = seeded["requester_id"]
        await session.commit()
    await _login_cancel_user(client, db_session_factory, requester_id=requester_id)
    saved = client.headers.pop("X-CSRF-Token", None)
    try:
        response = await client.post(f"{API}/{execution_id}/cancel", json={})
        assert response.status_code == 403
    finally:
        if saved is not None:
            client.headers["X-CSRF-Token"] = saved


@pytest.mark.asyncio
async def test_cancel_succeeded_409(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = unauthenticated_db_client
    async with db_session_factory() as session:
        seeded = await _seed_ready(session)
        outcome = await _create(session, seeded, idempotency_key=_idem_key())
        execution_id = outcome.result.id
        requester_id = seeded["requester_id"]
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        execution.status = ExecutionStatus.SUCCEEDED.value
        await session.commit()

    await _login_cancel_user(client, db_session_factory, requester_id=requester_id)
    response = await client.post(f"{API}/{execution_id}/cancel", json={})
    assert response.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal",
    [
        ExecutionStatus.FAILED.value,
        ExecutionStatus.TIMED_OUT.value,
        ExecutionStatus.PARTIALLY_SUCCEEDED.value,
    ],
)
async def test_cancel_other_terminals_409(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    terminal: str,
) -> None:
    client = unauthenticated_db_client
    async with db_session_factory() as session:
        seeded = await _seed_ready(session)
        outcome = await _create(session, seeded, idempotency_key=_idem_key())
        execution_id = outcome.result.id
        requester_id = seeded["requester_id"]
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        execution.status = terminal
        await session.commit()

    await _login_cancel_user(client, db_session_factory, requester_id=requester_id)
    response = await client.post(f"{API}/{execution_id}/cancel", json={})
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_cancel_idempotent_cancelled(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = unauthenticated_db_client
    async with db_session_factory() as session:
        seeded = await _seed_ready(session)
        outcome = await _create(session, seeded, idempotency_key=_idem_key())
        execution_id = outcome.result.id
        requester_id = seeded["requester_id"]
        await session.commit()
    await _login_cancel_user(client, db_session_factory, requester_id=requester_id)
    first = await client.post(
        f"{API}/{execution_id}/cancel", json={"reason": "once"}
    )
    assert first.status_code == 200
    second = await client.post(
        f"{API}/{execution_id}/cancel", json={"reason": "twice"}
    )
    assert second.status_code == 200
    assert second.json()["status"] == "CANCELLED"
    assert second.json()["cancel_requested_at"] == first.json()["cancel_requested_at"]


@pytest.mark.asyncio
async def test_cancel_queued(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = unauthenticated_db_client
    async with db_session_factory() as session:
        seeded = await _seed_ready(session)
        outcome = await _create(session, seeded, idempotency_key=_idem_key())
        execution_id = outcome.result.id
        requester_id = seeded["requester_id"]
        assert await ExecutionQueueService(session).stage_created_batch(limit=10) == 1
        await session.commit()
    await _login_cancel_user(client, db_session_factory, requester_id=requester_id)
    response = await client.post(f"{API}/{execution_id}/cancel", json={})
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_cancel_running_with_started_toolcall_cancel_requested(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = unauthenticated_db_client
    async with db_session_factory() as session:
        seeded = await _seed_ready(session)
        outcome = await _create(session, seeded, idempotency_key=_idem_key())
        execution_id = outcome.result.id
        requester_id = seeded["requester_id"]
        assert await ExecutionQueueService(session).stage_created_batch(limit=10) == 1
        await session.commit()
        await _seed_started_toolcall(session, execution_id)
        await session.commit()

    await _login_cancel_user(client, db_session_factory, requester_id=requester_id)
    response = await client.post(
        f"{API}/{execution_id}/cancel", json={"reason": "inflight"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "CANCEL_REQUESTED"
    assert response.json()["finished_at"] is None


@pytest.mark.asyncio
async def test_cancel_waiting_approval(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = unauthenticated_db_client
    async with db_session_factory() as session:
        seeded = await _seed_ready(session)
        outcome = await _create(session, seeded, idempotency_key=_idem_key())
        execution_id = outcome.result.id
        requester_id = seeded["requester_id"]
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        execution.status = ExecutionStatus.WAITING_APPROVAL.value
        steps = await ExecutionRepository(session).list_steps(execution_id)
        steps[0].status = StepStatus.WAITING_APPROVAL.value
        await session.commit()

    await _login_cancel_user(client, db_session_factory, requester_id=requester_id)
    response = await client.post(f"{API}/{execution_id}/cancel", json={})
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_cancel_waiting_input(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = unauthenticated_db_client
    async with db_session_factory() as session:
        seeded = await _seed_ready(session)
        outcome = await _create(session, seeded, idempotency_key=_idem_key())
        execution_id = outcome.result.id
        requester_id = seeded["requester_id"]
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        execution.status = ExecutionStatus.WAITING_INPUT.value
        steps = await ExecutionRepository(session).list_steps(execution_id)
        steps[0].status = StepStatus.WAITING_INPUT.value
        await session.commit()

    await _login_cancel_user(client, db_session_factory, requester_id=requester_id)
    response = await client.post(f"{API}/{execution_id}/cancel", json={})
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_cancel_idempotent_cancel_requested(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = unauthenticated_db_client
    async with db_session_factory() as session:
        seeded = await _seed_ready(session)
        outcome = await _create(session, seeded, idempotency_key=_idem_key())
        execution_id = outcome.result.id
        requester_id = seeded["requester_id"]
        assert await ExecutionQueueService(session).stage_created_batch(limit=10) == 1
        await session.commit()
        await _seed_started_toolcall(session, execution_id)
        await session.commit()

    await _login_cancel_user(client, db_session_factory, requester_id=requester_id)
    first = await client.post(
        f"{API}/{execution_id}/cancel", json={"reason": "first"}
    )
    assert first.status_code == 200
    assert first.json()["status"] == "CANCEL_REQUESTED"
    second = await client.post(
        f"{API}/{execution_id}/cancel", json={"reason": "second"}
    )
    assert second.status_code == 200
    assert second.json()["status"] == "CANCEL_REQUESTED"
    assert second.json()["cancel_requested_at"] == first.json()["cancel_requested_at"]


@pytest.mark.asyncio
async def test_cancel_requires_auth(unauthenticated_db_client: AsyncClient) -> None:
    response = await unauthenticated_db_client.post(
        f"{API}/{uuid.uuid4()}/cancel", json={}
    )
    assert response.status_code == 401
