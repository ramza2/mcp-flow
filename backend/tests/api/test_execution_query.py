"""API tests for GET /executions history (own vs execution.read)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.passwords import hash_password
from app.domain.enums import ExecutionStatus, ExecutionTriggerType
from app.repositories.user import UserRepository
from tests.helpers.execution_ops import seed_execution, seed_user

API = "/api/v1/executions"
PASSWORD = "correct-horse-battery-staple"


async def _client_for(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    with_execution_read: bool = False,
) -> tuple[AsyncClient, uuid.UUID]:
    client = unauthenticated_db_client
    async with db_session_factory() as session:
        user_id = await seed_user(session, with_execution_read=with_execution_read)
        user = await UserRepository(session).get(user_id)
        assert user is not None
        await UserRepository(session).set_password_hash(user.id, hash_password(PASSWORD))
        await session.commit()
        username = user.username

    login = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    csrf = await client.get("/api/v1/auth/csrf")
    assert csrf.status_code == 200
    client.headers["X-CSRF-Token"] = csrf.json()["csrf_token"]
    return client, user_id


@pytest.mark.asyncio
async def test_own_history_vs_global_execution_read(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Two users without execution.read
    client_a, user_a = await _client_for(
        unauthenticated_db_client, db_session_factory, with_execution_read=False
    )
    async with db_session_factory() as session:
        user_b = await seed_user(session, with_execution_read=False)
        exec_a = await seed_execution(session, requester_id=user_a)
        exec_b = await seed_execution(session, requester_id=user_b)
        await session.commit()
        exec_a_id, exec_b_id = exec_a.id, exec_b.id

    listed = await client_a.get(API)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    ids = {i["id"] for i in body["items"]}
    assert str(exec_a_id) in ids
    assert str(exec_b_id) not in ids
    assert body["total"] == len(body["items"]) or body["total"] >= 1

    detail_b = await client_a.get(f"{API}/{exec_b_id}")
    assert detail_b.status_code == 404
    steps_b = await client_a.get(f"{API}/{exec_b_id}/steps")
    assert steps_b.status_code == 404

    # Foreign requester_id filter → 403
    forbidden = await client_a.get(API, params={"requester_id": str(user_b)})
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "AUTH_FORBIDDEN"

    # Self requester_id ok
    self_ok = await client_a.get(API, params={"requester_id": str(user_a)})
    assert self_ok.status_code == 200

    # Grant execution.read to A
    from app.repositories.role import PermissionRepository
    from app.schemas.auth import (
        RoleCreate,
        RolePermissionReplaceRequest,
        UserRoleReplaceRequest,
    )
    from app.services.role import RoleService
    from app.services.user import UserService

    async with db_session_factory() as session:
        perm = await PermissionRepository(session).get_by_code("execution.read")
        assert perm is not None
        role = await RoleService(session).create(
            RoleCreate(code=f"er-{uuid.uuid4().hex[:6]}", name="ER")
        )
        await RoleService(session).replace_permissions(
            role.id,
            RolePermissionReplaceRequest(permission_ids=[perm.id]),
            expected_lock_version=1,
        )
        user = await UserRepository(session).get(user_a)
        assert user is not None
        roles = await UserService(session).list_roles(user_a)
        await UserService(session).replace_roles(
            user_a,
            UserRoleReplaceRequest(role_ids=[r.id for r in roles] + [role.id]),
            expected_lock_version=int(user.lock_version),
        )
        await session.commit()

    # Re-login to refresh? Permissions are DB-checked per request — no need.
    listed2 = await client_a.get(API)
    assert listed2.status_code == 200
    ids2 = {i["id"] for i in listed2.json()["items"]}
    assert str(exec_a_id) in ids2
    assert str(exec_b_id) in ids2

    detail_b2 = await client_a.get(f"{API}/{exec_b_id}")
    assert detail_b2.status_code == 200
    d = detail_b2.json()
    for forbidden_key in (
        "plan_snapshot",
        "input_snapshot",
        "policy_snapshot",
        "error_message",
        "cancel_reason",
        "worker_id",
        "lease_token",
        "lease_expires_at",
    ):
        assert forbidden_key not in d


@pytest.mark.asyncio
async def test_list_filters_sort_and_validation(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, user_id = await _client_for(
        unauthenticated_db_client, db_session_factory, with_execution_read=True
    )
    base = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)
    async with db_session_factory() as session:
        e1 = await seed_execution(
            session,
            requester_id=user_id,
            status=ExecutionStatus.FAILED.value,
            error_code="TOOL_FAILED",
            requested_at=base,
            started_at=base,
            finished_at=base + timedelta(seconds=10),
            trace_id="trace-abc",
        )
        e2 = await seed_execution(
            session,
            requester_id=user_id,
            status=ExecutionStatus.TIMED_OUT.value,
            error_code="STEP_TIMEOUT_EXCEEDED",
            requested_at=base + timedelta(seconds=1),
            started_at=base + timedelta(seconds=1),
            finished_at=base + timedelta(seconds=20),
        )
        e3 = await seed_execution(
            session,
            requester_id=user_id,
            status=ExecutionStatus.SUCCEEDED.value,
            requested_at=base + timedelta(seconds=2),
            started_at=base + timedelta(seconds=2),
            finished_at=base + timedelta(seconds=5),
        )
        await session.commit()

    multi = await client.get(
        API, params={"status": "FAILED,TIMED_OUT", "page_size": 50}
    )
    assert multi.status_code == 200
    statuses = {i["status"] for i in multi.json()["items"] if i["id"] in {str(e1.id), str(e2.id), str(e3.id)}}
    assert statuses == {"FAILED", "TIMED_OUT"}

    bad_status = await client.get(API, params={"status": "FAILED,NOPE"})
    assert bad_status.status_code == 422

    bad_sort = await client.get(API, params={"sort": "plan_snapshot"})
    assert bad_sort.status_code == 422

    naive = await client.get(API, params={"from": "2026-10-07T12:00:00"})
    assert naive.status_code == 422

    window = await client.get(
        API,
        params={
            "from": base.isoformat().replace("+00:00", "Z"),
            "to": (base + timedelta(seconds=2)).isoformat().replace("+00:00", "Z"),
        },
    )
    assert window.status_code == 200
    # inclusive from, exclusive to → e1,e2
    win_ids = {i["id"] for i in window.json()["items"]}
    assert str(e1.id) in win_ids
    assert str(e2.id) in win_ids
    assert str(e3.id) not in win_ids

    q = await client.get(API, params={"q": "trace-abc"})
    assert q.status_code == 200
    assert any(i["id"] == str(e1.id) for i in q.json()["items"])

    err = await client.get(API, params={"error_code": "TOOL_FAILED"})
    assert err.status_code == 200
    assert all(
        i["error_code"] == "TOOL_FAILED"
        for i in err.json()["items"]
        if i["id"] == str(e1.id)
    )

    # List omits secret fields
    item = next(i for i in multi.json()["items"] if i["id"] == str(e1.id))
    assert item["error_category"] == "tool"
    assert "plan_snapshot" not in item
    assert "result_summary" not in item


@pytest.mark.asyncio
async def test_steps_and_attempts_safe_projection(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.domain.enums import StepAttemptStatus, StepStatus, ToolCallNormalizedStatus
    from app.repositories.execution import ExecutionRepository
    from app.repositories.mcp_server import MCPServerRepository
    from app.repositories.mcp_tool import MCPToolRepository

    client, user_id = await _client_for(
        unauthenticated_db_client, db_session_factory, with_execution_read=False
    )
    async with db_session_factory() as session:
        execution = await seed_execution(session, requester_id=user_id)
        repo = ExecutionRepository(session)
        parent = await repo.create_step(
            execution_id=execution.id,
            step_key="loop",
            step_type="LOOP",
            mcp_tool_version_id=None,
            parent_step_id=None,
            sequence_hint=1,
            status=StepStatus.SUCCEEDED.value,
            step_snapshot={"type": "LOOP"},
            iteration_no=None,
        )
        child = await repo.create_step(
            execution_id=execution.id,
            step_key="tool-1",
            step_type="TOOL",
            mcp_tool_version_id=None,
            parent_step_id=parent.id,
            sequence_hint=2,
            status=StepStatus.SUCCEEDED.value,
            step_snapshot={"type": "TOOL"},
            iteration_no=0,
        )
        # Need server/tool version for ToolCall FK
        server = await MCPServerRepository(session).create(
            code=f"s-{uuid.uuid4().hex[:8]}",
            name="S",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/mcp",
            status="ACTIVE",
        )
        tools = MCPToolRepository(session)
        tool = await tools.create_tool(
            mcp_server_id=server.id,
            remote_name="t",
            display_name="t",
            tags=[],
            status="ACTIVE",
        )
        tv = await tools.create_version(
            mcp_tool_id=tool.id,
            version_no=1,
            content_hash=uuid.uuid4().hex,
            validation_status="VALID",
            remote_description="d",
            input_schema={"type": "object"},
        )
        child.mcp_tool_version_id = tv.id
        base = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)
        attempt = await repo.create_attempt(
            step_execution_id=child.id,
            attempt_no=1,
            status=StepAttemptStatus.SUCCEEDED.value,
            worker_id="worker-1",
            lease_expires_at=base + timedelta(minutes=1),
            idempotency_key=f"idem-{uuid.uuid4().hex}",
            request_snapshot={"secret": "x"},
            started_at=base,
        )
        attempt.finished_at = base + timedelta(seconds=3)
        attempt.error_layer = None
        tc = await repo.create_tool_call(
            step_attempt_id=attempt.id,
            mcp_server_id=server.id,
            mcp_tool_version_id=tv.id,
            protocol_era="CURRENT",
            protocol_version="2026-07-28",
            transport_type="STREAMABLE_HTTP",
            remote_request_id=f"rr-{uuid.uuid4().hex[:8]}",
            request_meta={"Authorization": "Bearer x"},
            normalized_status=ToolCallNormalizedStatus.SUCCEEDED.value,
            started_at=base,
        )
        tc.response_meta = {"body": "y"}
        tc.request_bytes = 10
        tc.response_bytes = 20
        tc.first_byte_at = base + timedelta(milliseconds=100)
        tc.finished_at = base + timedelta(seconds=2)
        await session.commit()
        exec_id, step_id = execution.id, child.id

    steps = await client.get(f"{API}/{exec_id}/steps")
    assert steps.status_code == 200
    items = steps.json()["items"]
    assert [i["step_key"] for i in items] == ["loop", "tool-1"]
    child_item = items[1]
    assert child_item["parent_step_id"] == items[0]["id"]
    assert child_item["iteration_no"] == 0
    assert "step_snapshot" not in child_item
    assert "resolved_input" not in child_item

    detail = await client.get(f"{API}/{exec_id}/steps/{step_id}")
    assert detail.status_code == 200
    d = detail.json()
    assert len(d["attempts"]) == 1
    att = d["attempts"][0]
    assert att["attempt_no"] == 1
    assert "worker_id" not in att
    assert "idempotency_key" not in att
    assert "request_snapshot" not in att
    assert len(att["tool_calls"]) == 1
    tc = att["tool_calls"][0]
    assert tc["request_bytes"] == 10
    assert tc["duration_ms"] == 2000
    assert tc["time_to_first_byte_ms"] == 100
    assert "request_meta" not in tc
    assert "response_meta" not in tc
    assert "remote_request_id" not in tc

    wrong = await client.get(f"{API}/{uuid.uuid4()}/steps/{step_id}")
    assert wrong.status_code == 404
