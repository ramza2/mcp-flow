"""SQLite-backed API tests for Schedule registry."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

API = "/api/v1/schedules"


@pytest.fixture
async def schedule_client(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncClient:
    from app.auth.passwords import hash_password
    from app.domain.enums import UserStatus
    from app.repositories.user import UserRepository

    client = unauthenticated_db_client
    username = f"sch-api-{uuid.uuid4().hex[:10]}"
    password = "correct-horse-battery-staple"
    async with db_session_factory() as session:
        user = await UserRepository(session).create(
            username=username,
            display_name="Schedule API",
            email=f"{username}@example.com",
            status=UserStatus.ACTIVE,
        )
        await UserRepository(session).set_password_hash(
            user.id, hash_password(password)
        )
        user = await UserRepository(session).get(user.id)
        assert user is not None
        from app.repositories.role import PermissionRepository
        from app.schemas.auth import (
            RoleCreate,
            RolePermissionReplaceRequest,
            UserRoleReplaceRequest,
        )
        from app.services.role import RoleService
        from app.services.user import UserService

        role = await RoleService(session).create(
            RoleCreate(code=f"sch-api-r-{uuid.uuid4().hex[:8]}", name="Schedule API")
        )
        manage = await PermissionRepository(session).get_by_code("schedule.manage")
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
        client.schedule_user_id = user.id  # type: ignore[attr-defined]

    login = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert login.status_code == 200, login.text
    csrf = await client.get("/api/v1/auth/csrf")
    assert csrf.status_code == 200
    client.headers["X-CSRF-Token"] = csrf.json()["csrf_token"]
    return client


async def _seed_agent_target(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> uuid.UUID:
    from app.domain.enums import (
        AgentStatus,
        AgentVersionStatus,
        AgentVersionValidationStatus,
    )
    from app.repositories.agent import AgentRepository
    from app.repositories.agent_version import AgentVersionRepository
    from app.repositories.user import UserRepository

    async with db_session_factory() as session:
        user = await UserRepository(session).create(
            username=f"sch-tgt-{uuid.uuid4().hex[:8]}",
            display_name="Target Owner",
            email=f"tgt-{uuid.uuid4().hex[:8]}@example.com",
            status="ACTIVE",
        )
        agent = await AgentRepository(session).create(
            code=f"agt-api-{uuid.uuid4().hex[:8]}",
            name="API Agent",
            owner_id=user.id,
            status=AgentStatus.ACTIVE.value,
        )
        version = await AgentVersionRepository(session).create(
            agent_id=agent.id,
            version_no=1,
            system_instruction="run",
            llm_profile_id=uuid.uuid4(),
            request_schema_version="1.0",
            plan_schema_version="1.0",
            selection_settings={},
            planning_settings={},
            response_settings={},
            content_hash=uuid.uuid4().hex,
        )
        version.status = AgentVersionStatus.PUBLISHED.value
        version.validation_status = AgentVersionValidationStatus.VALID.value
        await session.commit()
        return version.id


def _create_body(target_id: uuid.UUID, **overrides: Any) -> dict[str, Any]:
    body = {
        "name": "Daily job",
        "target_type": "AGENT_VERSION",
        "target_id": str(target_id),
        "schedule_type": "CRON",
        "schedule_expression": "0 9 * * *",
        "timezone": "UTC",
        "inputs": {},
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_unauthenticated_401(db_client: AsyncClient) -> None:
    response = await db_client.get(API)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_missing_schedule_manage_403(
    authenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    target_id = await _seed_agent_target(db_session_factory)
    response = await authenticated_db_client.post(
        API, json=_create_body(target_id)
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_FORBIDDEN"


@pytest.mark.asyncio
async def test_create_extra_forbid(schedule_client: AsyncClient) -> None:
    response = await schedule_client.post(
        API,
        json={
            "name": "Bad",
            "target_type": "AGENT_VERSION",
            "target_id": str(uuid.uuid4()),
            "schedule_type": "CRON",
            "schedule_expression": "0 9 * * *",
            "timezone": "UTC",
            "status": "ACTIVE",
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_list_get_patch_lifecycle(
    schedule_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    target_id = await _seed_agent_target(db_session_factory)
    created = await schedule_client.post(API, json=_create_body(target_id))
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "PAUSED"
    assert body["target_id"] == str(target_id)
    assert body["owner_id"] == str(schedule_client.schedule_user_id)  # type: ignore[attr-defined]
    schedule_id = body["id"]

    listing = await schedule_client.get(API, params={"q": "Daily", "sort": "+name"})
    assert listing.status_code == 200
    assert listing.json()["total"] >= 1

    detail = await schedule_client.get(f"{API}/{schedule_id}")
    assert detail.status_code == 200

    patched = await schedule_client.patch(
        f"{API}/{schedule_id}",
        headers={"If-Match": "1"},
        json={"name": "Renamed", "lock_version": 1},
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Renamed"
    assert patched.json()["lock_version"] == 2


@pytest.mark.asyncio
async def test_patch_requires_if_match(schedule_client: AsyncClient) -> None:
    missing = await schedule_client.patch(
        f"{API}/{uuid.uuid4()}",
        json={"name": "nope"},
    )
    assert missing.status_code == 422


@pytest.mark.asyncio
async def test_status_not_patchable_via_body(
    schedule_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    target_id = await _seed_agent_target(db_session_factory)
    created = await schedule_client.post(API, json=_create_body(target_id))
    schedule_id = created.json()["id"]
    forbidden = await schedule_client.patch(
        f"{API}/{schedule_id}",
        headers={"If-Match": "1"},
        json={"status": "ACTIVE", "lock_version": 1},
    )
    assert forbidden.status_code == 422


@pytest.mark.asyncio
async def test_owner_isolation_404(
    schedule_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    db_app,
) -> None:
    from httpx import ASGITransport, AsyncClient

    target_id = await _seed_agent_target(db_session_factory)
    created = await schedule_client.post(API, json=_create_body(target_id))
    schedule_id = created.json()["id"]

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

    username = f"sch-other-{uuid.uuid4().hex[:10]}"
    password = "correct-horse-battery-staple"
    async with db_session_factory() as session:
        user = await UserRepository(session).create(
            username=username,
            display_name="Other",
            email=f"{username}@example.com",
            status=UserStatus.ACTIVE,
        )
        await UserRepository(session).set_password_hash(
            user.id, hash_password(password)
        )
        user = await UserRepository(session).get(user.id)
        assert user is not None
        role = await RoleService(session).create(
            RoleCreate(code=f"sch-o-{uuid.uuid4().hex[:8]}", name="Other Sch")
        )
        manage = await PermissionRepository(session).get_by_code("schedule.manage")
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

    async with AsyncClient(
        transport=ASGITransport(app=db_app), base_url="http://test"
    ) as other:
        login = await other.post(
            "/api/v1/auth/login",
            json={"username": username, "password": password},
        )
        assert login.status_code == 200
        csrf = await other.get("/api/v1/auth/csrf")
        other.headers["X-CSRF-Token"] = csrf.json()["csrf_token"]
        missing = await other.get(f"{API}/{schedule_id}")
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_activate_pause_resume(
    schedule_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    target_id = await _seed_agent_target(db_session_factory)
    created = await schedule_client.post(API, json=_create_body(target_id))
    schedule_id = created.json()["id"]

    activated = await schedule_client.post(f"{API}/{schedule_id}/activate")
    assert activated.status_code == 200, activated.text
    assert activated.json()["status"] == "ACTIVE"
    assert activated.json()["next_run_at"] is not None
    next_run = activated.json()["next_run_at"]

    paused = await schedule_client.post(f"{API}/{schedule_id}/pause")
    assert paused.status_code == 200
    assert paused.json()["status"] == "PAUSED"
    assert paused.json()["next_run_at"] == next_run

    resumed = await schedule_client.post(f"{API}/{schedule_id}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "ACTIVE"


@pytest.mark.asyncio
async def test_occurrences_list_empty(
    schedule_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    target_id = await _seed_agent_target(db_session_factory)
    created = await schedule_client.post(API, json=_create_body(target_id))
    schedule_id = created.json()["id"]
    occ = await schedule_client.get(f"{API}/{schedule_id}/occurrences")
    assert occ.status_code == 200
    assert occ.json()["total"] == 0


@pytest.mark.asyncio
async def test_csrf_required_on_mutations(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
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
    username = f"sch-csrf-{uuid.uuid4().hex[:10]}"
    password = "correct-horse-battery-staple"
    async with db_session_factory() as session:
        user = await UserRepository(session).create(
            username=username,
            display_name="CSRF",
            email=f"{username}@example.com",
            status=UserStatus.ACTIVE,
        )
        await UserRepository(session).set_password_hash(
            user.id, hash_password(password)
        )
        user = await UserRepository(session).get(user.id)
        assert user is not None
        role = await RoleService(session).create(
            RoleCreate(code=f"sch-c-{uuid.uuid4().hex[:8]}", name="CSRF Sch")
        )
        manage = await PermissionRepository(session).get_by_code("schedule.manage")
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

    login = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert login.status_code == 200
    target_id = await _seed_agent_target(db_session_factory)
    no_csrf = await client.post(API, json=_create_body(target_id))
    assert no_csrf.status_code == 403
