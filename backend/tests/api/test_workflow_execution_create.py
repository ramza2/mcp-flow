"""API tests for POST /workflows/{id}/versions/{vid}/executions."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

API = "/api/v1/workflows"


@pytest.fixture
async def workflow_execute_client(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncClient:
    from app.auth.passwords import hash_password
    from app.domain.enums import UserStatus
    from app.repositories.user import UserRepository

    client = unauthenticated_db_client
    username = f"wf-api-exec-{uuid.uuid4().hex[:10]}"
    password = "correct-horse-battery-staple"
    async with db_session_factory() as session:
        user = await UserRepository(session).create(
            username=username,
            display_name="WF Exec API",
            email=f"{username}@example.com",
            status=UserStatus.ACTIVE,
        )
        await UserRepository(session).set_password_hash(
            user.id, hash_password(password)
        )
        await session.commit()
        client.workflow_user_id = user.id  # type: ignore[attr-defined]

    login = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert login.status_code == 200, login.text
    csrf = await client.get("/api/v1/auth/csrf")
    assert csrf.status_code == 200
    client.headers["X-CSRF-Token"] = csrf.json()["csrf_token"]
    return client


async def _seed_published_workflow(
    db_session_factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID]:
    from app.domain.enums import MCPToolStatus, RiskClass, WorkflowStatus
    from app.repositories.mcp_server import MCPServerRepository
    from app.repositories.mcp_tool import MCPToolRepository
    from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
    from app.schemas.auth import ResourceGrantCreate
    from app.schemas.workflow import WorkflowCreate, WorkflowUpdate, WorkflowVersionCreate
    from app.services.authorization import ResourceGrantService
    from app.services.workflow import WorkflowService
    from app.services.workflow_version import WorkflowVersionService
    from tests.unit.test_workflow_registry import _tool_plan

    async with db_session_factory() as session:
        server = await MCPServerRepository(session).create(
            code=f"wf-api-ex-{uuid.uuid4().hex[:8]}",
            name="API Exec Server",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/mcp",
            status="ACTIVE",
        )
        tools = MCPToolRepository(session)
        tool = await tools.create_tool(
            mcp_server_id=server.id,
            remote_name=f"tool_{uuid.uuid4().hex[:6]}",
            display_name="api_exec_tool",
            tags=[],
            status="ACTIVE",
        )
        version = await tools.create_version(
            mcp_tool_id=tool.id,
            version_no=1,
            content_hash=uuid.uuid4().hex,
            validation_status="VALID",
            remote_description="api",
            input_schema={
                "type": "object",
                "properties": {"location": {"type": "string"}},
                "required": ["location"],
            },
        )
        tool.current_version_id = version.id
        tool.status = MCPToolStatus.ACTIVE.value
        await MCPToolPolicyRepository(session).create(
            mcp_tool_id=tool.id,
            risk_class=RiskClass.READ_ONLY.value,
            requires_confirmation=False,
            requires_approval=False,
            approval_policy_id=None,
            timeout_ms=30_000,
            max_attempts=1,
            backoff_policy=None,
            max_result_bytes=65536,
            allow_auto_select=True,
            data_classification=None,
            policy_metadata=None,
        )
        workflow = await WorkflowService(session).create(
            WorkflowCreate(name="API Exec WF", visibility="PRIVATE")
        )
        plan = _tool_plan(workflow.id, version.id)
        plan["inputs"] = {
            "region": {"type": "string", "required": False, "secret": False},
        }
        wf_version = await WorkflowVersionService(session).create_version(
            workflow.id,
            WorkflowVersionCreate(plan_definition=plan, change_summary="api"),
        )
        await WorkflowVersionService(session).validate(workflow.id, wf_version.id)
        await WorkflowVersionService(session).publish(workflow.id, wf_version.id)
        wf = await WorkflowService(session).get(workflow.id)
        assert wf is not None
        await WorkflowService(session).update(
            workflow.id,
            WorkflowUpdate(status=WorkflowStatus.ACTIVE, lock_version=wf.lock_version),
            expected_lock_version=int(wf.lock_version),
        )
        grants = ResourceGrantService(session)
        from app.domain.enums import ResourceGrantResourceType

        await grants.create_for_user(
            user_id,
            ResourceGrantCreate(
                resource_type=ResourceGrantResourceType.WORKFLOW,
                resource_id=workflow.id,
            ),
        )
        await grants.create_for_user(
            user_id,
            ResourceGrantCreate(
                resource_type=ResourceGrantResourceType.MCP_TOOL,
                resource_id=tool.id,
            ),
        )
        from app.repositories.role import PermissionRepository
        from app.schemas.auth import RoleCreate, RolePermissionReplaceRequest, UserRoleReplaceRequest
        from app.services.role import RoleService
        from app.repositories.user import UserRepository
        from app.services.user import UserService

        role = await RoleService(session).create(
            RoleCreate(code=f"wf-api-r-{uuid.uuid4().hex[:8]}", name="API WF Exec")
        )
        perms = PermissionRepository(session)
        execute_tool = await perms.get_by_code("mcp.tool.execute")
        execute_wf = await perms.get_by_code("workflow.execute")
        assert execute_tool and execute_wf
        await RoleService(session).replace_permissions(
            role.id,
            RolePermissionReplaceRequest(permission_ids=[execute_tool.id, execute_wf.id]),
            expected_lock_version=1,
        )
        user = await UserRepository(session).get(user_id)
        assert user is not None
        await UserService(session).replace_roles(
            user_id,
            UserRoleReplaceRequest(role_ids=[role.id]),
            expected_lock_version=int(user.lock_version),
        )
        await session.commit()
        return workflow.id, wf_version.id


def _exec_url(workflow_id: uuid.UUID, version_id: uuid.UUID) -> str:
    return f"{API}/{workflow_id}/versions/{version_id}/executions"


@pytest.mark.asyncio
async def test_idempotency_key_required(workflow_execute_client: AsyncClient) -> None:
    wf_id, ver_id = uuid.uuid4(), uuid.uuid4()
    response = await workflow_execute_client.post(
        _exec_url(wf_id, ver_id),
        json={"inputs": {}},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_create_201_and_replay(
    workflow_execute_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = workflow_execute_client.workflow_user_id  # type: ignore[attr-defined]
    wf_id, ver_id = await _seed_published_workflow(db_session_factory, user_id)
    key = f"idem-{uuid.uuid4().hex}"
    headers = {"Idempotency-Key": key}
    first = await workflow_execute_client.post(
        _exec_url(wf_id, ver_id),
        json={"inputs": {}},
        headers=headers,
    )
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["status"] == "CREATED"
    assert body["source_type"] == "WORKFLOW_VERSION"
    assert body["workflow_version_id"] == str(ver_id)

    replay = await workflow_execute_client.post(
        _exec_url(wf_id, ver_id),
        json={"inputs": {}},
        headers=headers,
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == body["id"]


@pytest.mark.asyncio
async def test_idempotency_key_reuse_different_body_409(
    workflow_execute_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = workflow_execute_client.workflow_user_id  # type: ignore[attr-defined]
    wf_id, ver_id = await _seed_published_workflow(db_session_factory, user_id)
    key = f"idem-{uuid.uuid4().hex}"
    ok = await workflow_execute_client.post(
        _exec_url(wf_id, ver_id),
        json={"inputs": {"region": "A"}},
        headers={"Idempotency-Key": key},
    )
    assert ok.status_code == 201
    conflict = await workflow_execute_client.post(
        _exec_url(wf_id, ver_id),
        json={"inputs": {"region": "B"}},
        headers={"Idempotency-Key": key},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.asyncio
async def test_not_found_workflow_404(
    workflow_execute_client: AsyncClient,
) -> None:
    response = await workflow_execute_client.post(
        _exec_url(uuid.uuid4(), uuid.uuid4()),
        json={"inputs": {}},
        headers={"Idempotency-Key": f"idem-{uuid.uuid4().hex}"},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unauthenticated_401(unauthenticated_db_client: AsyncClient) -> None:
    response = await unauthenticated_db_client.post(
        _exec_url(uuid.uuid4(), uuid.uuid4()),
        json={"inputs": {}},
        headers={"Idempotency-Key": "k1"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_forbidden_without_grants(
    authenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.domain.enums import UserStatus
    from app.repositories.user import UserRepository

    async with db_session_factory() as session:
        owner = await UserRepository(session).create(
            username=f"wf-owner-{uuid.uuid4().hex[:8]}",
            display_name="Owner",
            email=f"owner-{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE,
        )
        await session.commit()
        owner_id = owner.id
    wf_id, ver_id = await _seed_published_workflow(db_session_factory, owner_id)
    response = await authenticated_db_client.post(
        _exec_url(wf_id, ver_id),
        json={"inputs": {}},
        headers={"Idempotency-Key": f"idem-{uuid.uuid4().hex}"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_csrf_required_without_token(
    workflow_execute_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = workflow_execute_client.workflow_user_id  # type: ignore[attr-defined]
    wf_id, ver_id = await _seed_published_workflow(db_session_factory, user_id)
    client = workflow_execute_client
    saved = client.headers.pop("X-CSRF-Token", None)
    try:
        response = await client.post(
            _exec_url(wf_id, ver_id),
            json={"inputs": {}},
            headers={"Idempotency-Key": f"idem-{uuid.uuid4().hex}"},
        )
        assert response.status_code == 403
    finally:
        if saved:
            client.headers["X-CSRF-Token"] = saved
