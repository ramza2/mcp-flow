"""SQLite-backed API tests for External MCP Discovery."""

from __future__ import annotations

import uuid

import pytest
from app.api.v1.mcp_discovery import get_external_discovery_provider
from app.discovery.contracts import ExternalMCPProviderCandidate
from app.discovery.provider import EXTERNAL_DISCOVERY_PROVIDER_UNAVAILABLE
from app.domain.enums import (
    ExternalMCPCandidateReviewState,
    ExternalMCPSearchStatus,
    ExternalMCPSourceType,
    MCPTransportType,
    UserStatus,
)
from app.repositories.external_discovery import ExternalDiscoveryRepository
from app.repositories.role import PermissionRepository
from app.repositories.user import UserRepository
from app.schemas.auth import (
    RoleCreate,
    RolePermissionReplaceRequest,
    UserRoleReplaceRequest,
)
from app.services.role import RoleService
from app.services.user import UserService
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

API = "/api/v1/mcp-discovery"


class _FakeProvider:
    def __init__(self, candidates: list[ExternalMCPProviderCandidate]) -> None:
        self._candidates = candidates

    async def search(self, source, query: str, limit: int):
        return self._candidates[:limit]


@pytest.fixture
async def discovery_client(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    db_app,
) -> AsyncClient:
    from app.auth.passwords import hash_password

    client = unauthenticated_db_client
    username = f"disc-api-{uuid.uuid4().hex[:10]}"
    password = "correct-horse-battery-staple"
    async with db_session_factory() as session:
        user = await UserRepository(session).create(
            username=username,
            display_name="Discovery API",
            email=f"{username}@example.com",
            status=UserStatus.ACTIVE,
        )
        await UserRepository(session).set_password_hash(
            user.id, hash_password(password)
        )
        user = await UserRepository(session).get(user.id)
        assert user is not None
        role = await RoleService(session).create(
            RoleCreate(code=f"disc-api-r-{uuid.uuid4().hex[:8]}", name="Disc API")
        )
        read = await PermissionRepository(session).get_by_code("mcp.server.read")
        manage = await PermissionRepository(session).get_by_code("mcp.server.manage")
        assert read is not None and manage is not None
        await RoleService(session).replace_permissions(
            role.id,
            RolePermissionReplaceRequest(permission_ids=[read.id, manage.id]),
            expected_lock_version=1,
        )
        await UserService(session).replace_roles(
            user.id,
            UserRoleReplaceRequest(role_ids=[role.id]),
            expected_lock_version=int(user.lock_version),
        )
        await session.commit()
        client.discovery_user_id = user.id  # type: ignore[attr-defined]

    login = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert login.status_code == 200, login.text
    csrf = await client.get("/api/v1/auth/csrf")
    assert csrf.status_code == 200
    client.headers["X-CSRF-Token"] = csrf.json()["csrf_token"]

    # Default provider remains unavailable unless a test overrides.
    db_app.dependency_overrides.pop(get_external_discovery_provider, None)
    return client


@pytest.fixture
async def read_only_client(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncClient:
    from app.auth.passwords import hash_password

    client = unauthenticated_db_client
    username = f"disc-ro-{uuid.uuid4().hex[:10]}"
    password = "correct-horse-battery-staple"
    async with db_session_factory() as session:
        user = await UserRepository(session).create(
            username=username,
            display_name="Discovery RO",
            email=f"{username}@example.com",
            status=UserStatus.ACTIVE,
        )
        await UserRepository(session).set_password_hash(
            user.id, hash_password(password)
        )
        user = await UserRepository(session).get(user.id)
        assert user is not None
        role = await RoleService(session).create(
            RoleCreate(code=f"disc-ro-r-{uuid.uuid4().hex[:8]}", name="Disc RO")
        )
        read = await PermissionRepository(session).get_by_code("mcp.server.read")
        assert read is not None
        await RoleService(session).replace_permissions(
            role.id,
            RolePermissionReplaceRequest(permission_ids=[read.id]),
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
    assert login.status_code == 200, login.text
    csrf = await client.get("/api/v1/auth/csrf")
    assert csrf.status_code == 200
    client.headers["X-CSRF-Token"] = csrf.json()["csrf_token"]
    return client


async def _seed_source(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    enabled: bool = True,
) -> uuid.UUID:
    async with db_session_factory() as session:
        source = await ExternalDiscoveryRepository(session).create_source(
            code=f"api-src-{uuid.uuid4().hex[:8]}",
            name="API Source",
            source_type=ExternalMCPSourceType.REGISTRY.value,
            provider_key="test.fake",
            enabled=enabled,
        )
        await session.commit()
        return source.id


def _cand(**overrides) -> ExternalMCPProviderCandidate:
    base = dict(
        external_key=f"k-{uuid.uuid4().hex[:8]}",
        name="Cand",
        description="desc",
        version="1.0",
        license="MIT",
        repository_url="https://example.com/r",
        homepage_url="https://example.com",
        transport_type=MCPTransportType.STREAMABLE_HTTP.value,
        endpoint_url="https://example.com/mcp",
    )
    base.update(overrides)
    return ExternalMCPProviderCandidate(**base)


@pytest.mark.asyncio
async def test_unauthenticated_rejected(unauthenticated_db_client: AsyncClient) -> None:
    response = await unauthenticated_db_client.get(f"{API}/sources")
    assert response.status_code in {401, 403}


@pytest.mark.asyncio
async def test_list_sources_empty_and_seeded(
    discovery_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    empty = await discovery_client.get(f"{API}/sources")
    assert empty.status_code == 200
    assert empty.json()["items"] == []

    source_id = await _seed_source(db_session_factory)
    listing = await discovery_client.get(f"{API}/sources")
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == str(source_id)
    assert "credentials" not in items[0]
    assert set(items[0].keys()) >= {
        "id",
        "code",
        "name",
        "source_type",
        "provider_key",
        "base_url",
        "enabled",
        "created_at",
        "updated_at",
    }


@pytest.mark.asyncio
async def test_search_extra_forbid(
    discovery_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _seed_source(db_session_factory)
    response = await discovery_client.post(
        f"{API}/searches",
        json={
            "source_id": str(source_id),
            "q": "x",
            "limit": 5,
            "evil": True,
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_search_failed_provider_returns_resource(
    discovery_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _seed_source(db_session_factory)
    response = await discovery_client.post(
        f"{API}/searches",
        json={"source_id": str(source_id), "q": "weather", "limit": 5},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == ExternalMCPSearchStatus.FAILED.value
    assert body["error_code"] == EXTERNAL_DISCOVERY_PROVIDER_UNAVAILABLE
    assert body["candidates"] == []

    detail = await discovery_client.get(f"{API}/searches/{body['id']}")
    assert detail.status_code == 200
    assert detail.json()["error_code"] == EXTERNAL_DISCOVERY_PROVIDER_UNAVAILABLE


@pytest.mark.asyncio
async def test_search_review_import_happy_path(
    discovery_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    db_app,
) -> None:
    source_id = await _seed_source(db_session_factory)
    db_app.dependency_overrides[get_external_discovery_provider] = lambda: _FakeProvider(
        [_cand(external_key="happy-1", name="Happy Server")]
    )
    try:
        search = await discovery_client.post(
            f"{API}/searches",
            json={"source_id": str(source_id), "q": "happy", "limit": 10},
        )
        assert search.status_code == 200, search.text
        body = search.json()
        assert body["status"] == ExternalMCPSearchStatus.SUCCEEDED.value
        assert body["candidate_count"] == 1
        candidate_id = body["candidates"][0]["id"]

        cand = await discovery_client.get(f"{API}/candidates/{candidate_id}")
        assert cand.status_code == 200
        assert cand.json()["review_state"] == ExternalMCPCandidateReviewState.UNREVIEWED.value
        assert "install_command" not in cand.json()
        assert "raw" not in cand.json()

        review = await discovery_client.post(
            f"{API}/candidates/{candidate_id}/reviews",
            json={"decision": "APPROVE", "comment": "ok"},
        )
        assert review.status_code == 200, review.text
        assert review.json()["review_state"] == ExternalMCPCandidateReviewState.APPROVED.value

        imported = await discovery_client.post(
            f"{API}/candidates/{candidate_id}/import"
        )
        assert imported.status_code == 200, imported.text
        payload = imported.json()
        assert payload["created"] is True
        assert payload["server_status"] == "DRAFT"
        server_id = payload["mcp_server_id"]

        again = await discovery_client.post(
            f"{API}/candidates/{candidate_id}/import"
        )
        assert again.status_code == 200
        assert again.json()["created"] is False
        assert again.json()["mcp_server_id"] == server_id

        server = await discovery_client.get(f"/api/v1/mcp/servers/{server_id}")
        assert server.status_code == 200
        assert server.json()["status"] == "DRAFT"
        assert server.json()["auth_type"] == "NONE"
    finally:
        db_app.dependency_overrides.pop(get_external_discovery_provider, None)


@pytest.mark.asyncio
async def test_read_ok_manage_forbidden_for_read_only(
    read_only_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _seed_source(db_session_factory)
    listing = await read_only_client.get(f"{API}/sources")
    assert listing.status_code == 200

    banned = await read_only_client.post(
        f"{API}/searches",
        json={"source_id": str(source_id), "q": "x", "limit": 5},
    )
    assert banned.status_code == 403
    assert banned.json()["error"]["code"] == "AUTH_FORBIDDEN"


@pytest.mark.asyncio
async def test_csrf_required_on_search(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.auth.passwords import hash_password

    source_id = await _seed_source(db_session_factory)
    client = unauthenticated_db_client
    username = f"disc-csrf-{uuid.uuid4().hex[:10]}"
    password = "correct-horse-battery-staple"
    async with db_session_factory() as session:
        user = await UserRepository(session).create(
            username=username,
            display_name="CSRF Disc",
            email=f"{username}@example.com",
            status=UserStatus.ACTIVE,
        )
        await UserRepository(session).set_password_hash(
            user.id, hash_password(password)
        )
        user = await UserRepository(session).get(user.id)
        assert user is not None
        role = await RoleService(session).create(
            RoleCreate(code=f"disc-c-{uuid.uuid4().hex[:8]}", name="CSRF Disc")
        )
        manage = await PermissionRepository(session).get_by_code("mcp.server.manage")
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
    assert login.status_code == 200, login.text
    # Intentionally omit X-CSRF-Token
    response = await client.post(
        f"{API}/searches",
        json={"source_id": str(source_id), "q": "x", "limit": 5},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_unknown_resources_404(discovery_client: AsyncClient) -> None:
    missing = uuid.uuid4()
    search = await discovery_client.get(f"{API}/searches/{missing}")
    assert search.status_code == 404
    cand = await discovery_client.get(f"{API}/candidates/{missing}")
    assert cand.status_code == 404
