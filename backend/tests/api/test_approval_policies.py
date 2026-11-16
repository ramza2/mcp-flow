"""SQLite-backed API tests for ApprovalPolicy admin API (docs/06 §13)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

API = "/api/v1/approval-policies"


@pytest.fixture
async def policy_client(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncClient:
    from app.auth.passwords import hash_password
    from app.domain.enums import UserStatus
    from app.repositories.role import PermissionRepository
    from app.repositories.user import UserRepository
    from app.schemas.auth import (
        RoleCreate,
        RolePermissionReplaceRequest,
        UserRoleReplaceRequest,
    )
    from app.services.role import RoleService
    from app.services.user import UserService

    client = unauthenticated_db_client
    username = f"apol-api-{uuid.uuid4().hex[:10]}"
    password = "correct-horse-battery-staple"
    async with db_session_factory() as session:
        user = await UserRepository(session).create(
            username=username,
            display_name="Approval Policy API",
            email=f"{username}@example.com",
            status=UserStatus.ACTIVE,
        )
        await UserRepository(session).set_password_hash(
            user.id, hash_password(password)
        )
        user = await UserRepository(session).get(user.id)
        assert user is not None

        role = await RoleService(session).create(
            RoleCreate(
                code=f"apol-api-r-{uuid.uuid4().hex[:8]}",
                name="Approval Policy API",
            )
        )
        manage = await PermissionRepository(session).get_by_code(
            "approval.policy.manage"
        )
        assert manage is not None
        await RoleService(session).replace_permissions(
            role.id,
            RolePermissionReplaceRequest(permission_ids=[manage.id]),
            expected_lock_version=1,
        )
        await UserService(session).replace_roles(
            user.id,
            UserRoleReplaceRequest(role_ids=[role.id]),
            expected_lock_version=int(user.lock_version),
        )
        await session.commit()
        client.policy_user_id = user.id  # type: ignore[attr-defined]
        client.policy_role_code = role.code  # type: ignore[attr-defined]

    login = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert login.status_code == 200, login.text
    csrf = await client.get("/api/v1/auth/csrf")
    assert csrf.status_code == 200
    client.headers["X-CSRF-Token"] = csrf.json()["csrf_token"]
    return client


def _create_body(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": f"apol-{uuid.uuid4().hex[:8]}",
        "name": "외부 전송 승인",
        "decision_mode": "ANY",
        "required_approvals": 1,
        "approver_scope": None,
        "default_expiry_seconds": 3600,
        "allow_self_approval": False,
        "reject_comment_required": True,
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_approval_policy_crud_activate_deactivate(
    policy_client: AsyncClient,
) -> None:
    created = await policy_client.post(API, json=_create_body())
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "ACTIVE"
    assert body["lock_version"] == 1
    assert body["decision_mode"] == "ANY"
    policy_id = body["id"]

    detail = await policy_client.get(f"{API}/{policy_id}")
    assert detail.status_code == 200
    assert detail.json()["name"] == "외부 전송 승인"

    listing = await policy_client.get(API, params={"q": "외부", "status": "ACTIVE"})
    assert listing.status_code == 200
    assert listing.json()["total"] >= 1

    patched = await policy_client.patch(
        f"{API}/{policy_id}",
        headers={"If-Match": "1"},
        json={"name": "Renamed Policy", "lock_version": 1},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "Renamed Policy"
    assert patched.json()["lock_version"] == 2

    stale = await policy_client.patch(
        f"{API}/{policy_id}",
        headers={"If-Match": "1"},
        json={"name": "Stale", "lock_version": 1},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "RESOURCE_VERSION_CONFLICT"

    deactivated = await policy_client.post(f"{API}/{policy_id}/deactivate")
    assert deactivated.status_code == 200
    assert deactivated.json()["status"] == "INACTIVE"
    assert deactivated.json()["lock_version"] == 3

    again = await policy_client.post(f"{API}/{policy_id}/deactivate")
    assert again.status_code == 200
    assert again.json()["status"] == "INACTIVE"
    assert again.json()["lock_version"] == 3

    activated = await policy_client.post(f"{API}/{policy_id}/activate")
    assert activated.status_code == 200
    assert activated.json()["status"] == "ACTIVE"


@pytest.mark.asyncio
async def test_approval_policy_validation_decision_mode_and_scope(
    policy_client: AsyncClient,
) -> None:
    role_code = policy_client.policy_role_code  # type: ignore[attr-defined]

    bad_any = await policy_client.post(
        API,
        json=_create_body(decision_mode="ANY", required_approvals=2),
    )
    assert bad_any.status_code == 422

    unknown_role = await policy_client.post(
        API,
        json=_create_body(
            approver_scope={"role_codes": ["DOES_NOT_EXIST"]},
        ),
    )
    assert unknown_role.status_code == 422
    assert "role_codes" in unknown_role.json()["error"]["message"]

    bad_scope = await policy_client.post(
        API,
        json=_create_body(approver_scope={"teams": ["x"]}),
    )
    assert bad_scope.status_code == 422

    ok = await policy_client.post(
        API,
        json=_create_body(
            decision_mode="QUORUM",
            required_approvals=2,
            approver_scope={"role_codes": [role_code]},
        ),
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["approver_scope"] == {"role_codes": [role_code]}
    assert ok.json()["decision_mode"] == "QUORUM"
    assert ok.json()["required_approvals"] == 2

    dup = await policy_client.post(
        API,
        json=_create_body(code=ok.json()["code"]),
    )
    assert dup.status_code == 409


@pytest.mark.asyncio
async def test_approval_policy_patch_rejects_code_status_injection(
    policy_client: AsyncClient,
) -> None:
    created = await policy_client.post(API, json=_create_body())
    policy_id = created.json()["id"]

    code_patch = await policy_client.patch(
        f"{API}/{policy_id}",
        headers={"If-Match": "1"},
        json={"code": "hijack", "lock_version": 1},
    )
    assert code_patch.status_code == 422

    status_patch = await policy_client.patch(
        f"{API}/{policy_id}",
        headers={"If-Match": "1"},
        json={"status": "INACTIVE", "lock_version": 1},
    )
    assert status_patch.status_code == 422

    no_lock = await policy_client.patch(
        f"{API}/{policy_id}",
        json={"name": "No Lock"},
    )
    assert no_lock.status_code == 422


@pytest.mark.asyncio
async def test_deactivate_blocked_by_tool_policy_reference(
    policy_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.domain.enums import RiskClass
    from app.repositories.mcp_server import MCPServerRepository
    from app.repositories.mcp_tool import MCPToolRepository
    from app.repositories.mcp_tool_policy import MCPToolPolicyRepository

    created = await policy_client.post(API, json=_create_body())
    assert created.status_code == 201
    policy_id = uuid.UUID(created.json()["id"])

    async with db_session_factory() as session:
        server = await MCPServerRepository(session).create(
            code=f"srv-{uuid.uuid4().hex[:8]}",
            name="Ref Server",
            transport_type="HTTP",
            endpoint_url="https://example.test/mcp",
        )
        tool = await MCPToolRepository(session).create_tool(
            mcp_server_id=server.id,
            remote_name=f"tool-{uuid.uuid4().hex[:8]}",
            display_name="Ref Tool",
        )
        await MCPToolPolicyRepository(session).create(
            mcp_tool_id=tool.id,
            risk_class=RiskClass.IDEMPOTENT_WRITE.value,
            requires_confirmation=False,
            requires_approval=True,
            approval_policy_id=policy_id,
            timeout_ms=5000,
            max_attempts=1,
            backoff_policy=None,
            max_result_bytes=1024,
            allow_auto_select=True,
            data_classification=None,
            policy_metadata=None,
        )
        await session.commit()

    blocked = await policy_client.post(f"{API}/{policy_id}/deactivate")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "RESOURCE_CONFLICT"
    assert "ToolPolicy" in blocked.json()["error"]["message"]


async def _seed_workflow_approval_version(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    policy_id: uuid.UUID,
    status: str,
) -> uuid.UUID:
    from app.domain.enums import AuthorableStepType
    from app.repositories.workflow import WorkflowRepository
    from app.repositories.workflow_version import WorkflowVersionRepository
    from app.schemas.execution_plan import EXECUTION_PLAN_SCHEMA_VERSION

    async with db_session_factory() as session:
        workflow = await WorkflowRepository(session).create(
            code=f"wf-{uuid.uuid4().hex[:8]}",
            name="Approval Ref WF",
        )
        plan = {
            "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
            "goal": "deactivate guard",
            "source": {"type": "WORKFLOW", "workflow_id": str(workflow.id)},
            "inputs": {},
            "limits": {"max_steps": 10, "max_parallelism": 1, "max_duration_seconds": 60},
            "steps": [
                {
                    "id": "apr1",
                    "name": "apr1",
                    "type": AuthorableStepType.APPROVAL.value,
                    "required": True,
                    "depends_on": [],
                    "when": None,
                    "timeout_seconds": 30,
                    "on_error": "FAIL_EXECUTION",
                    "config": {"approval_policy_id": str(policy_id)},
                }
            ],
            "completion": {
                "success_policy": "ALL_REQUIRED",
                "response_step_ids": ["apr1"],
            },
        }
        version = await WorkflowVersionRepository(session).create(
            workflow_id=workflow.id,
            version_no=1,
            plan_schema_version=EXECUTION_PLAN_SCHEMA_VERSION,
            plan_definition=plan,
            input_schema={},
            output_schema={},
            policy_defaults={},
            content_hash=uuid.uuid4().hex,
            change_summary="ref",
        )
        version.status = status
        await session.commit()
        return version.id


@pytest.mark.asyncio
async def test_deactivate_blocked_by_live_workflow_approval_reference(
    policy_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    created = await policy_client.post(API, json=_create_body())
    assert created.status_code == 201
    policy_id = uuid.UUID(created.json()["id"])

    await _seed_workflow_approval_version(
        db_session_factory, policy_id=policy_id, status="PUBLISHED"
    )

    blocked = await policy_client.post(f"{API}/{policy_id}/deactivate")
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "RESOURCE_CONFLICT"
    assert "WorkflowVersion" in blocked.json()["error"]["message"]


@pytest.mark.asyncio
async def test_deactivate_allows_deprecated_workflow_approval_history(
    policy_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    created = await policy_client.post(API, json=_create_body())
    assert created.status_code == 201
    policy_id = uuid.UUID(created.json()["id"])

    await _seed_workflow_approval_version(
        db_session_factory, policy_id=policy_id, status="DEPRECATED"
    )

    ok = await policy_client.post(f"{API}/{policy_id}/deactivate")
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "INACTIVE"


@pytest.mark.asyncio
async def test_deactivate_blocked_by_draft_workflow_approval_reference(
    policy_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    created = await policy_client.post(API, json=_create_body())
    assert created.status_code == 201
    policy_id = uuid.UUID(created.json()["id"])

    await _seed_workflow_approval_version(
        db_session_factory, policy_id=policy_id, status="DRAFT"
    )

    blocked = await policy_client.post(f"{API}/{policy_id}/deactivate")
    assert blocked.status_code == 409, blocked.text
    assert "WorkflowVersion" in blocked.json()["error"]["message"]


@pytest.mark.asyncio
async def test_permission_regression_decide_not_sufficient_or_required(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """approval.decide must not gate this API; manage is required."""
    from app.auth.passwords import hash_password
    from app.domain.enums import UserStatus
    from app.repositories.role import PermissionRepository
    from app.repositories.user import UserRepository
    from app.schemas.auth import (
        RoleCreate,
        RolePermissionReplaceRequest,
        UserRoleReplaceRequest,
    )
    from app.services.role import RoleService
    from app.services.user import UserService

    client = unauthenticated_db_client
    password = "correct-horse-battery-staple"

    async def _login(username: str) -> None:
        login = await client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": password},
        )
        assert login.status_code == 200, login.text
        csrf = await client.get("/api/v1/auth/csrf")
        client.headers["X-CSRF-Token"] = csrf.json()["csrf_token"]

    # Unauthenticated → 401
    unauth = await client.get(API)
    assert unauth.status_code == 401

    # ACTIVE user with only approval.decide → 403
    async with db_session_factory() as session:
        decide_user = await UserRepository(session).create(
            username=f"decide-only-{uuid.uuid4().hex[:8]}",
            display_name="Decide Only",
            email=f"decide-{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE,
        )
        await UserRepository(session).set_password_hash(
            decide_user.id, hash_password(password)
        )
        decide_user = await UserRepository(session).get(decide_user.id)
        assert decide_user is not None
        role = await RoleService(session).create(
            RoleCreate(code=f"dec-r-{uuid.uuid4().hex[:8]}", name="Decide Only")
        )
        decide = await PermissionRepository(session).get_by_code("approval.decide")
        assert decide is not None
        await RoleService(session).replace_permissions(
            role.id,
            RolePermissionReplaceRequest(permission_ids=[decide.id]),
            expected_lock_version=1,
        )
        await UserService(session).replace_roles(
            decide_user.id,
            UserRoleReplaceRequest(role_ids=[role.id]),
            expected_lock_version=int(decide_user.lock_version),
        )
        await session.commit()
        decide_username = decide_user.username

    await _login(decide_username)
    denied = await client.get(API)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "AUTH_FORBIDDEN"
    assert "approval.policy.manage" in denied.json()["error"]["message"]
    assert "approval.decide" not in denied.json()["error"]["message"]

    # ACTIVE user with manage but without decide → 200 (decide not required)
    async with db_session_factory() as session:
        manage_user = await UserRepository(session).create(
            username=f"manage-only-{uuid.uuid4().hex[:8]}",
            display_name="Manage Only",
            email=f"manage-{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE,
        )
        await UserRepository(session).set_password_hash(
            manage_user.id, hash_password(password)
        )
        manage_user = await UserRepository(session).get(manage_user.id)
        assert manage_user is not None
        role = await RoleService(session).create(
            RoleCreate(code=f"mgr-r-{uuid.uuid4().hex[:8]}", name="Manage Only")
        )
        manage = await PermissionRepository(session).get_by_code(
            "approval.policy.manage"
        )
        assert manage is not None
        await RoleService(session).replace_permissions(
            role.id,
            RolePermissionReplaceRequest(permission_ids=[manage.id]),
            expected_lock_version=1,
        )
        await UserService(session).replace_roles(
            manage_user.id,
            UserRoleReplaceRequest(role_ids=[role.id]),
            expected_lock_version=int(manage_user.lock_version),
        )
        await session.commit()
        manage_username = manage_user.username
        manage_user_id = manage_user.id

    await _login(manage_username)
    allowed = await client.get(API)
    assert allowed.status_code == 200, allowed.text

    # INACTIVE manage user → session invalid (401); not authorized via decide.
    async with db_session_factory() as session:
        user = await UserRepository(session).get(manage_user_id)
        assert user is not None
        user.status = UserStatus.INACTIVE.value
        await session.commit()

    inactive = await client.get(API)
    assert inactive.status_code == 401


@pytest.mark.asyncio
async def test_approval_query_still_uses_decide_not_policy_manage(
    db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: granting policy.manage alone must not open GET /approvals."""
    from app.auth.passwords import hash_password
    from app.repositories.role import PermissionRepository
    from app.repositories.user import UserRepository
    from app.schemas.auth import (
        RoleCreate,
        RolePermissionReplaceRequest,
        UserRoleReplaceRequest,
    )
    from app.services.role import RoleService
    from app.services.user import UserService

    from tests.api.test_approval_decisions import APPROVALS, _login_as
    from tests.api.test_auth_session import PASSWORD
    from tests.unit.test_approval_decision_resume import _enter_waiting

    _execution_id, _approval_id, requester_id = await _enter_waiting(
        db_session_factory,
        monkeypatch,
        allow_self_approval=True,
        approver_scope={},
    )
    async with db_session_factory() as session:
        await UserRepository(session).set_password_hash(
            requester_id, hash_password(PASSWORD)
        )
        user = await UserRepository(session).get(requester_id)
        assert user is not None
        role = await RoleService(session).create(
            RoleCreate(code=f"pm-r-{uuid.uuid4().hex[:8]}", name="Policy Manage Only")
        )
        manage = await PermissionRepository(session).get_by_code(
            "approval.policy.manage"
        )
        assert manage is not None
        await RoleService(session).replace_permissions(
            role.id,
            RolePermissionReplaceRequest(permission_ids=[manage.id]),
            expected_lock_version=1,
        )
        await UserService(session).replace_roles(
            user.id,
            UserRoleReplaceRequest(role_ids=[role.id]),
            expected_lock_version=int(user.lock_version),
        )
        await session.commit()

    await _login_as(db_client, db_session_factory, user_id=requester_id)
    resp = await db_client.get(APPROVALS)
    assert resp.status_code == 403
    assert "approval.decide" in resp.json()["error"]["message"]
