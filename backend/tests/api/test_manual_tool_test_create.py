"""API tests for POST /mcp/tools/{tool_id}/test-calls (FNC-TOOL-005)."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

API = "/api/v1/mcp/tools"


@pytest.fixture
async def manual_test_client(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncClient:
    from app.auth.passwords import hash_password
    from app.domain.enums import UserStatus
    from app.repositories.user import UserRepository

    client = unauthenticated_db_client
    username = f"mtt-api-{uuid.uuid4().hex[:10]}"
    password = "correct-horse-battery-staple"
    async with db_session_factory() as session:
        user = await UserRepository(session).create(
            username=username,
            display_name="MTT API",
            email=f"{username}@example.com",
            status=UserStatus.ACTIVE,
        )
        await UserRepository(session).set_password_hash(
            user.id, hash_password(password)
        )
        await session.commit()
        client.mtt_user_id = user.id  # type: ignore[attr-defined]

    login = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert login.status_code == 200, login.text
    csrf = await client.get("/api/v1/auth/csrf")
    assert csrf.status_code == 200
    client.headers["X-CSRF-Token"] = csrf.json()["csrf_token"]
    return client


async def _seed_tool(
    db_session_factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    *,
    grant: bool = True,
) -> tuple[uuid.UUID, uuid.UUID]:
    from app.domain.enums import MCPToolStatus, RiskClass
    from app.repositories.mcp_server import MCPServerRepository
    from app.repositories.mcp_tool import MCPToolRepository
    from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
    from app.repositories.user import UserRepository

    from tests.unit.test_plan_validator import _authorize_requester

    async with db_session_factory() as session:
        server = await MCPServerRepository(session).create(
            code=f"mtt-api-{uuid.uuid4().hex[:8]}",
            name="API MTT Server",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/mtt",
            status="ACTIVE",
        )
        tools = MCPToolRepository(session)
        tool = await tools.create_tool(
            mcp_server_id=server.id,
            remote_name=f"tool_{uuid.uuid4().hex[:6]}",
            display_name="mtt",
            tags=[],
            status=MCPToolStatus.ACTIVE.value,
        )
        version = await tools.create_version(
            mcp_tool_id=tool.id,
            version_no=1,
            content_hash=uuid.uuid4().hex,
            validation_status="VALID",
            remote_description="mtt",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
            },
        )
        tool.current_version_id = version.id
        await MCPToolPolicyRepository(session).create(
            mcp_tool_id=tool.id,
            risk_class=RiskClass.READ_ONLY.value,
            requires_confirmation=False,
            requires_approval=False,
            approval_policy_id=None,
            timeout_ms=30_000,
            max_attempts=1,
            backoff_policy=None,
            max_result_bytes=1_048_576,
            allow_auto_select=True,
            data_classification=None,
            policy_metadata=None,
        )
        if grant:
            await _authorize_requester(session, user_id=user_id, tool_id=tool.id)
        else:
            # Permission without ResourceGrant — exercise FORBIDDEN path.
            from app.repositories.role import PermissionRepository
            from app.schemas.auth import (
                RoleCreate,
                RolePermissionReplaceRequest,
                UserRoleReplaceRequest,
            )
            from app.services.role import RoleService
            from app.services.user import UserService

            role = await RoleService(session).create(
                RoleCreate(code=f"mtt-nog-{uuid.uuid4().hex[:6]}", name="nog")
            )
            perm = await PermissionRepository(session).get_by_code("mcp.tool.execute")
            assert perm is not None
            await RoleService(session).replace_permissions(
                role.id,
                RolePermissionReplaceRequest(permission_ids=[perm.id]),
                expected_lock_version=1,
            )
            user = await UserRepository(session).get(user_id)
            assert user is not None
            await UserService(session).replace_roles(
                user.id,
                UserRoleReplaceRequest(role_ids=[role.id]),
                expected_lock_version=int(user.lock_version),
            )
        await session.commit()
        return tool.id, version.id


@pytest.mark.asyncio
async def test_manual_tool_test_requires_csrf(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.auth.passwords import hash_password
    from app.domain.enums import UserStatus
    from app.repositories.user import UserRepository

    client = unauthenticated_db_client
    username = f"mtt-csrf-{uuid.uuid4().hex[:8]}"
    async with db_session_factory() as session:
        user = await UserRepository(session).create(
            username=username,
            display_name="csrf",
            email=f"{username}@example.com",
            status=UserStatus.ACTIVE,
        )
        await UserRepository(session).set_password_hash(
            user.id, hash_password("correct-horse-battery-staple")
        )
        await session.commit()
        user_id = user.id
    login = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "correct-horse-battery-staple"},
    )
    assert login.status_code == 200
    tool_id, version_id = await _seed_tool(db_session_factory, user_id)
    resp = await client.post(
        f"{API}/{tool_id}/test-calls",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"mcp_tool_version_id": str(version_id), "arguments": {}},
    )
    assert resp.status_code in {401, 403}


@pytest.mark.asyncio
async def test_manual_tool_test_create_success(
    manual_test_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = manual_test_client.mtt_user_id  # type: ignore[attr-defined]
    tool_id, version_id = await _seed_tool(db_session_factory, user_id)
    key = str(uuid.uuid4())
    resp = await manual_test_client.post(
        f"{API}/{tool_id}/test-calls",
        headers={"Idempotency-Key": key},
        json={
            "mcp_tool_version_id": str(version_id),
            "arguments": {"text": "ping"},
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source_type"] == "MANUAL_TOOL_TEST"
    assert body["trigger_type"] == "TEST"
    assert body["mcp_tool_id"] == str(tool_id)
    assert body["mcp_tool_version_id"] == str(version_id)
    assert body["step_count"] == 1
    execution_id = body["id"]

    # Replay
    replay = await manual_test_client.post(
        f"{API}/{tool_id}/test-calls",
        headers={"Idempotency-Key": key},
        json={
            "mcp_tool_version_id": str(version_id),
            "arguments": {"text": "ping"},
        },
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == execution_id

    # Existing Execution read API
    detail = await manual_test_client.get(f"/api/v1/executions/{execution_id}")
    assert detail.status_code in {200, 403}


@pytest.mark.asyncio
async def test_manual_tool_test_forbidden_without_grant(
    manual_test_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = manual_test_client.mtt_user_id  # type: ignore[attr-defined]
    tool_id, version_id = await _seed_tool(db_session_factory, user_id, grant=False)
    resp = await manual_test_client.post(
        f"{API}/{tool_id}/test-calls",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"mcp_tool_version_id": str(version_id), "arguments": {}},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_manual_tool_test_requires_idempotency_key(
    manual_test_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = manual_test_client.mtt_user_id  # type: ignore[attr-defined]
    tool_id, version_id = await _seed_tool(db_session_factory, user_id)
    resp = await manual_test_client.post(
        f"{API}/{tool_id}/test-calls",
        json={"mcp_tool_version_id": str(version_id), "arguments": {}},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_manual_tool_test_invalid_version(
    manual_test_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = manual_test_client.mtt_user_id  # type: ignore[attr-defined]
    tool_id, _version_id = await _seed_tool(db_session_factory, user_id)
    resp = await manual_test_client.post(
        f"{API}/{tool_id}/test-calls",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"mcp_tool_version_id": str(uuid.uuid4()), "arguments": {}},
    )
    assert resp.status_code == 404
