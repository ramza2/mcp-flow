"""Unit tests for External MCP Discovery service foundation."""

from __future__ import annotations

import uuid
from dataclasses import fields

import pytest
from app.core.errors import AppError
from app.discovery.contracts import ExternalMCPProviderCandidate
from app.discovery.provider import (
    EXTERNAL_DISCOVERY_PROVIDER_UNAVAILABLE,
    ExternalDiscoveryProviderError,
    UnavailableExternalMCPProvider,
)
from app.domain.enums import (
    ExternalMCPCandidateReviewState,
    ExternalMCPReviewDecision,
    ExternalMCPSearchStatus,
    ExternalMCPSourceType,
    MCPServerStatus,
    MCPTransportType,
    UserStatus,
)
from app.models.mcp import MCPServer, MCPServerCheck, MCPServerDiscovery
from app.repositories.external_discovery import ExternalDiscoveryRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.role import PermissionRepository
from app.schemas.auth import (
    RoleCreate,
    RolePermissionReplaceRequest,
    UserCreate,
    UserRoleReplaceRequest,
)
from app.schemas.external_discovery import (
    ExternalMCPReviewCreate,
    ExternalMCPSearchCreate,
)
from app.services.external_discovery import ExternalDiscoveryService
from app.services.role import RoleService
from app.services.user import UserService
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


class _FakeProvider:
    def __init__(
        self,
        candidates: list[ExternalMCPProviderCandidate] | None = None,
        *,
        error: ExternalDiscoveryProviderError | None = None,
    ) -> None:
        self._candidates = candidates or []
        self._error = error
        self.calls = 0

    async def search(self, source, query: str, limit: int):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._candidates[:limit]


async def _seed_user_with_perms(
    session: AsyncSession,
    *,
    codes: list[str],
) -> uuid.UUID:
    user = await UserService(session).create(
        UserCreate(
            username=f"disc-{uuid.uuid4().hex[:10]}",
            display_name="Discovery Tester",
            email=f"disc-{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE,
        )
    )
    role = await RoleService(session).create(
        RoleCreate(code=f"disc-r-{uuid.uuid4().hex[:8]}", name="Discovery Role")
    )
    perm_ids: list[uuid.UUID] = []
    for code in codes:
        perm = await PermissionRepository(session).get_by_code(code)
        assert perm is not None
        perm_ids.append(perm.id)
    await RoleService(session).replace_permissions(
        role.id,
        RolePermissionReplaceRequest(permission_ids=perm_ids),
        expected_lock_version=1,
    )
    await UserService(session).replace_roles(
        user.id,
        UserRoleReplaceRequest(role_ids=[role.id]),
        expected_lock_version=int(user.lock_version),
    )
    await session.commit()
    return user.id


async def _seed_source(
    session: AsyncSession,
    *,
    enabled: bool = True,
    code: str | None = None,
) -> uuid.UUID:
    source = await ExternalDiscoveryRepository(session).create_source(
        code=code or f"src-{uuid.uuid4().hex[:8]}",
        name="Test Registry",
        source_type=ExternalMCPSourceType.REGISTRY.value,
        provider_key="test.fake",
        enabled=enabled,
    )
    await session.commit()
    return source.id


def _http_candidate(**overrides) -> ExternalMCPProviderCandidate:
    base = dict(
        external_key=f"ext-{uuid.uuid4().hex[:8]}",
        name="Example MCP",
        description="A candidate",
        version="1.0.0",
        license="MIT",
        repository_url="https://example.com/repo",
        homepage_url="https://example.com",
        transport_type=MCPTransportType.STREAMABLE_HTTP.value,
        endpoint_url="https://example.com/mcp",
    )
    base.update(overrides)
    return ExternalMCPProviderCandidate(**base)


@pytest.mark.asyncio
async def test_provider_contract_has_no_install_or_credential_fields() -> None:
    names = {f.name for f in fields(ExternalMCPProviderCandidate)}
    forbidden = {
        "install_command",
        "command",
        "args",
        "credentials",
        "auth_headers",
        "token",
        "secret",
        "raw",
        "raw_blob",
    }
    assert names.isdisjoint(forbidden)
    with pytest.raises(TypeError):
        ExternalMCPProviderCandidate(  # type: ignore[call-arg]
            external_key="k",
            name="n",
            install_command="curl | sh",
        )


@pytest.mark.asyncio
async def test_disabled_source_rejected(db_session: AsyncSession) -> None:
    user_id = await _seed_user_with_perms(
        db_session, codes=["mcp.server.manage", "mcp.server.read"]
    )
    source_id = await _seed_source(db_session, enabled=False)
    service = ExternalDiscoveryService(db_session, provider=_FakeProvider())
    with pytest.raises(AppError) as exc:
        await service.create_search(
            user_id,
            ExternalMCPSearchCreate(source_id=source_id, q="weather", limit=5),
        )
    assert exc.value.code == "EXTERNAL_DISCOVERY_SOURCE_DISABLED"


@pytest.mark.asyncio
async def test_fake_provider_successful_search(db_session: AsyncSession) -> None:
    user_id = await _seed_user_with_perms(
        db_session, codes=["mcp.server.manage", "mcp.server.read"]
    )
    source_id = await _seed_source(db_session)
    cand = _http_candidate(external_key="weather-1", name="Weather")
    service = ExternalDiscoveryService(
        db_session, provider=_FakeProvider([cand])
    )
    result = await service.create_search(
        user_id,
        ExternalMCPSearchCreate(source_id=source_id, q="weather", limit=10),
    )
    assert result.status == ExternalMCPSearchStatus.SUCCEEDED
    assert result.candidate_count == 1
    assert len(result.candidates) == 1
    assert result.candidates[0].external_key == "weather-1"
    assert result.candidates[0].review_state == ExternalMCPCandidateReviewState.UNREVIEWED


@pytest.mark.asyncio
async def test_provider_failure_durable_failed_search(db_session: AsyncSession) -> None:
    user_id = await _seed_user_with_perms(
        db_session, codes=["mcp.server.manage", "mcp.server.read"]
    )
    source_id = await _seed_source(db_session)
    provider = UnavailableExternalMCPProvider()
    service = ExternalDiscoveryService(db_session, provider=provider)
    result = await service.create_search(
        user_id,
        ExternalMCPSearchCreate(source_id=source_id, q="anything", limit=5),
    )
    assert result.status == ExternalMCPSearchStatus.FAILED
    assert result.error_code == EXTERNAL_DISCOVERY_PROVIDER_UNAVAILABLE
    assert result.candidate_count == 0
    assert result.candidates == []
    # Durable evidence reloaded
    again = await service.get_search(user_id, result.id)
    assert again.status == ExternalMCPSearchStatus.FAILED
    assert again.error_code == EXTERNAL_DISCOVERY_PROVIDER_UNAVAILABLE


@pytest.mark.asyncio
async def test_candidate_field_bounds(db_session: AsyncSession) -> None:
    user_id = await _seed_user_with_perms(
        db_session, codes=["mcp.server.manage", "mcp.server.read"]
    )
    source_id = await _seed_source(db_session)
    long_desc = "x" * 5000
    cand = _http_candidate(description=long_desc, name="Bounded")
    service = ExternalDiscoveryService(
        db_session, provider=_FakeProvider([cand])
    )
    result = await service.create_search(
        user_id,
        ExternalMCPSearchCreate(source_id=source_id, q="b", limit=5),
    )
    assert result.status == ExternalMCPSearchStatus.SUCCEEDED
    assert result.candidates[0].description is not None
    assert len(result.candidates[0].description) == 4000


@pytest.mark.asyncio
async def test_latest_review_determines_state(db_session: AsyncSession) -> None:
    import asyncio

    user_id = await _seed_user_with_perms(
        db_session, codes=["mcp.server.manage", "mcp.server.read"]
    )
    source_id = await _seed_source(db_session)
    service = ExternalDiscoveryService(
        db_session, provider=_FakeProvider([_http_candidate()])
    )
    search = await service.create_search(
        user_id,
        ExternalMCPSearchCreate(source_id=source_id, q="x", limit=5),
    )
    cid = search.candidates[0].id

    await service.create_review(
        user_id,
        cid,
        ExternalMCPReviewCreate(decision=ExternalMCPReviewDecision.APPROVE),
    )
    await asyncio.sleep(0.01)
    await service.create_review(
        user_id,
        cid,
        ExternalMCPReviewCreate(
            decision=ExternalMCPReviewDecision.REJECT, comment="unsafe"
        ),
    )
    detail = await service.get_candidate(user_id, cid)
    assert detail.review_state == ExternalMCPCandidateReviewState.REJECTED

    await asyncio.sleep(0.01)
    await service.create_review(
        user_id,
        cid,
        ExternalMCPReviewCreate(decision=ExternalMCPReviewDecision.APPROVE),
    )
    detail2 = await service.get_candidate(user_id, cid)
    assert detail2.review_state == ExternalMCPCandidateReviewState.APPROVED

    reviews = await ExternalDiscoveryRepository(db_session).list_reviews(cid)
    assert len(reviews) == 3


@pytest.mark.asyncio
async def test_import_requires_approve(db_session: AsyncSession) -> None:
    user_id = await _seed_user_with_perms(
        db_session, codes=["mcp.server.manage", "mcp.server.read"]
    )
    source_id = await _seed_source(db_session)
    service = ExternalDiscoveryService(
        db_session, provider=_FakeProvider([_http_candidate()])
    )
    search = await service.create_search(
        user_id,
        ExternalMCPSearchCreate(source_id=source_id, q="x", limit=5),
    )
    cid = search.candidates[0].id
    with pytest.raises(AppError) as exc:
        await service.import_candidate(user_id, cid)
    assert exc.value.code == "EXTERNAL_DISCOVERY_IMPORT_NOT_APPROVED"


@pytest.mark.asyncio
async def test_import_creates_draft_only_no_discovery(
    db_session: AsyncSession,
) -> None:
    user_id = await _seed_user_with_perms(
        db_session, codes=["mcp.server.manage", "mcp.server.read"]
    )
    source_id = await _seed_source(db_session)
    service = ExternalDiscoveryService(
        db_session, provider=_FakeProvider([_http_candidate()])
    )
    search = await service.create_search(
        user_id,
        ExternalMCPSearchCreate(source_id=source_id, q="x", limit=5),
    )
    cid = search.candidates[0].id
    await service.create_review(
        user_id,
        cid,
        ExternalMCPReviewCreate(decision=ExternalMCPReviewDecision.APPROVE),
    )
    imported = await service.import_candidate(user_id, cid)
    assert imported.created is True
    assert imported.server_status == MCPServerStatus.DRAFT

    server = await MCPServerRepository(db_session).get(imported.mcp_server_id)
    assert server is not None
    assert server.status == MCPServerStatus.DRAFT.value
    assert server.auth_type == "NONE"
    assert server.discovery_mode is None

    discovery_count = (
        await db_session.execute(select(func.count()).select_from(MCPServerDiscovery))
    ).scalar_one()
    check_count = (
        await db_session.execute(select(func.count()).select_from(MCPServerCheck))
    ).scalar_one()
    assert discovery_count == 0
    assert check_count == 0

    again = await service.import_candidate(user_id, cid)
    assert again.created is False
    assert again.mcp_server_id == imported.mcp_server_id

    server_count = (
        await db_session.execute(select(func.count()).select_from(MCPServer))
    ).scalar_one()
    assert server_count == 1


@pytest.mark.asyncio
async def test_stdio_import_fail_closed(db_session: AsyncSession) -> None:
    user_id = await _seed_user_with_perms(
        db_session, codes=["mcp.server.manage", "mcp.server.read"]
    )
    source_id = await _seed_source(db_session)
    cand = _http_candidate(
        transport_type=MCPTransportType.STDIO.value,
        endpoint_url=None,
    )
    service = ExternalDiscoveryService(
        db_session, provider=_FakeProvider([cand])
    )
    search = await service.create_search(
        user_id,
        ExternalMCPSearchCreate(source_id=source_id, q="stdio", limit=5),
    )
    cid = search.candidates[0].id
    await service.create_review(
        user_id,
        cid,
        ExternalMCPReviewCreate(decision=ExternalMCPReviewDecision.APPROVE),
    )
    with pytest.raises(AppError) as exc:
        await service.import_candidate(user_id, cid)
    assert exc.value.code == "EXTERNAL_DISCOVERY_STDIO_IMPORT_UNSUPPORTED"


@pytest.mark.asyncio
async def test_read_requires_permission(db_session: AsyncSession) -> None:
    user_id = await _seed_user_with_perms(db_session, codes=[])  # no perms
    service = ExternalDiscoveryService(db_session)
    with pytest.raises(AppError) as exc:
        await service.list_sources(user_id)
    assert exc.value.code == "AUTH_FORBIDDEN"
