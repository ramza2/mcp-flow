"""API tests for Audit read endpoints and auth instrumentation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.writer import (
    ACTION_AUTH_LOGIN,
    ACTION_AUTH_LOGOUT,
    ACTION_EXECUTION_CREATE,
    AuditWriter,
)
from app.auth.passwords import hash_password
from app.domain.enums import AuditActorType, AuditResult, UserStatus
from app.models.audit import AuditEvent
from app.repositories.role import PermissionRepository
from app.repositories.session import SessionRepository
from app.repositories.user import UserRepository
from app.schemas.auth import (
    RoleCreate,
    RolePermissionReplaceRequest,
    UserRoleReplaceRequest,
)
from app.services.role import RoleService
from app.services.user import UserService

AUTH = "/api/v1/auth"
AUDIT = "/api/v1/audit"
PASSWORD = "correct-horse-battery-staple"


async def _provision_user(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    username: str | None = None,
    password: str = PASSWORD,
    with_audit_read: bool = False,
) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    username = username or f"aud_{suffix}"
    async with db_session_factory() as session:
        user = await UserRepository(session).create(
            username=username,
            display_name=f"Aud {suffix}",
            email=f"{username}@example.com",
            status=UserStatus.ACTIVE,
        )
        await UserRepository(session).set_password_hash(
            user.id, hash_password(password)
        )
        if with_audit_read:
            perm = await PermissionRepository(session).get_by_code("audit.read")
            assert perm is not None
            role = await RoleService(session).create(
                RoleCreate(code=f"ar-{suffix}", name="Audit Reader")
            )
            await RoleService(session).replace_permissions(
                role.id,
                RolePermissionReplaceRequest(permission_ids=[perm.id]),
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
        return {"id": user.id, "username": username}


async def _login(
    client: AsyncClient, username: str, password: str = PASSWORD
) -> Any:
    return await client.post(
        f"{AUTH}/login",
        json={"username": username, "password": password},
    )


async def _count_action(
    db_session_factory: async_sessionmaker[AsyncSession], action: str
) -> int:
    async with db_session_factory() as session:
        rows = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == action)
            )
        ).scalars().all()
        return len(rows)


@pytest.mark.asyncio
async def test_login_success_emits_audit(
    db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await _provision_user(db_session_factory)
    response = await _login(db_client, user["username"])
    assert response.status_code == 200, response.text
    request_id = response.headers.get("x-request-id")
    assert request_id

    async with db_session_factory() as session:
        rows = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == ACTION_AUTH_LOGIN)
            )
        ).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.result == AuditResult.SUCCESS.value
        assert row.actor_type == AuditActorType.USER.value
        assert row.actor_id == str(user["id"])
        assert row.resource_type == "USER"
        assert row.resource_id == str(user["id"])
        assert row.request_id == request_id
        assert row.trace_id is None
        assert row.source_ip_hash is None
        blob = str(row.before_data) + str(row.after_data) + str(row.change_set)
        assert "password" not in blob.lower() or "[REDACTED]" in blob
        assert PASSWORD not in blob
        assert "mcpflow_session" not in blob


@pytest.mark.asyncio
async def test_login_failure_emits_audit_and_no_session(
    db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await _provision_user(db_session_factory)
    response = await _login(db_client, user["username"], password="wrong-password")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_INVALID_CREDENTIALS"

    async with db_session_factory() as session:
        rows = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == ACTION_AUTH_LOGIN)
            )
        ).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.result == AuditResult.FAILURE.value
        assert row.actor_id is None
        assert row.reason == "AUTH_INVALID_CREDENTIALS"
        assert "password" not in str(row.change_set).lower() or "[REDACTED]" in str(
            row.change_set
        )
        assert PASSWORD not in str(row.change_set)
        assert "wrong-password" not in str(row.change_set)
        sessions = await SessionRepository(session).count_active_for_user(user["id"])
        assert sessions == 0


@pytest.mark.asyncio
async def test_logout_emits_session_audit(
    db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await _provision_user(db_session_factory)
    login = await _login(db_client, user["username"])
    assert login.status_code == 200
    csrf = await db_client.get(f"{AUTH}/csrf")
    assert csrf.status_code == 200
    logout = await db_client.post(
        f"{AUTH}/logout",
        headers={"X-CSRF-Token": csrf.json()["csrf_token"]},
    )
    assert logout.status_code == 204

    async with db_session_factory() as session:
        rows = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == ACTION_AUTH_LOGOUT)
            )
        ).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.result == AuditResult.SUCCESS.value
        assert row.resource_type == "SESSION"
        assert row.actor_id == str(user["id"])
        assert row.resource_id is not None


@pytest.mark.asyncio
async def test_audit_api_requires_auth_and_permission(
    db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Unauthenticated
    bare = await db_client.get(f"{AUDIT}/events")
    assert bare.status_code == 401

    # Authenticated without audit.read
    user = await _provision_user(db_session_factory, with_audit_read=False)
    login = await _login(db_client, user["username"])
    assert login.status_code == 200
    forbidden = await db_client.get(f"{AUDIT}/events")
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "AUTH_FORBIDDEN"


@pytest.mark.asyncio
async def test_audit_list_filters_cursor_and_detail(
    db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    reader = await _provision_user(db_session_factory, with_audit_read=True)
    login = await _login(db_client, reader["username"])
    assert login.status_code == 200

    base = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
    async with db_session_factory() as session:
        writer = AuditWriter(session)
        for i in range(5):
            await writer.append(
                actor_type=AuditActorType.USER,
                actor_id=str(reader["id"]),
                action=ACTION_EXECUTION_CREATE if i % 2 == 0 else ACTION_AUTH_LOGIN,
                result=AuditResult.SUCCESS if i < 4 else AuditResult.FAILURE,
                resource_type="EXECUTION" if i % 2 == 0 else "USER",
                resource_id=str(uuid.uuid4()),
                request_id=f"req-{i}",
                occurred_at=base + timedelta(seconds=i),
            )
        await session.commit()

    page1 = await db_client.get(f"{AUDIT}/events", params={"limit": 2})
    assert page1.status_code == 200, page1.text
    body1 = page1.json()
    assert len(body1["items"]) == 2
    assert body1["next_cursor"]
    # List omits snapshots
    assert "before_data" not in body1["items"][0]
    assert "change_set" not in body1["items"][0]

    page2 = await db_client.get(
        f"{AUDIT}/events",
        params={"limit": 2, "cursor": body1["next_cursor"]},
    )
    assert page2.status_code == 200
    body2 = page2.json()
    ids1 = {i["event_id"] for i in body1["items"]}
    ids2 = {i["event_id"] for i in body2["items"]}
    assert ids1.isdisjoint(ids2)

    # Ordering: occurred_at DESC
    assert body1["items"][0]["occurred_at"] >= body1["items"][1]["occurred_at"]

    filtered = await db_client.get(
        f"{AUDIT}/events",
        params={"action": ACTION_AUTH_LOGIN, "result": "FAILURE"},
    )
    assert filtered.status_code == 200
    assert all(i["action"] == ACTION_AUTH_LOGIN for i in filtered.json()["items"])
    assert all(i["result"] == "FAILURE" for i in filtered.json()["items"])

    # from inclusive / to exclusive
    window = await db_client.get(
        f"{AUDIT}/events",
        params={
            "from": (base + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
            "to": (base + timedelta(seconds=3)).isoformat().replace("+00:00", "Z"),
        },
    )
    assert window.status_code == 200
    assert len(window.json()["items"]) == 2  # seconds 1 and 2

    naive = await db_client.get(
        f"{AUDIT}/events",
        params={"from": "2026-10-06T12:00:00"},
    )
    assert naive.status_code == 422

    bad_cursor = await db_client.get(
        f"{AUDIT}/events", params={"cursor": "%%%bad%%%"}
    )
    assert bad_cursor.status_code == 422

    event_id = body1["items"][0]["event_id"]
    detail = await db_client.get(f"{AUDIT}/events/{event_id}")
    assert detail.status_code == 200
    d = detail.json()
    assert "before_data" in d
    assert "after_data" in d
    assert "change_set" in d
    assert "source_ip_hash" in d
    assert "id" not in d  # no internal DB id

    missing = await db_client.get(f"{AUDIT}/events/{uuid.uuid4()}")
    assert missing.status_code == 404

    # Audit query itself must not create AuditEvents
    before = await _count_action(db_session_factory, "audit.read")
    await db_client.get(f"{AUDIT}/events")
    after = await _count_action(db_session_factory, "audit.read")
    assert before == after == 0


@pytest.mark.asyncio
async def test_audit_openapi_has_no_mutation_routes(db_app) -> None:
    paths = db_app.openapi()["paths"]
    audit_paths = {p: methods for p, methods in paths.items() if "/audit" in p}
    assert "/api/v1/audit/events" in audit_paths
    assert "/api/v1/audit/events/{event_id}" in audit_paths
    for methods in audit_paths.values():
        for method in methods:
            assert method.lower() in {"get", "parameters", "head", "options"}
