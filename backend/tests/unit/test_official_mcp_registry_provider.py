"""Unit tests for Official MCP Registry provider (allowlisted HTTPS)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from app.discovery import official_registry as official_registry_mod
from app.discovery.errors import (
    EXTERNAL_DISCOVERY_PROVIDER_UNAVAILABLE,
    ExternalDiscoveryProviderError,
)
from app.discovery.official_registry import (
    EXTERNAL_DISCOVERY_REGISTRY_HTTP_ERROR,
    EXTERNAL_DISCOVERY_REGISTRY_INVALID_RESPONSE,
    EXTERNAL_DISCOVERY_REGISTRY_REDIRECT_REJECTED,
    EXTERNAL_DISCOVERY_REGISTRY_RESPONSE_TOO_LARGE,
    EXTERNAL_DISCOVERY_REGISTRY_TIMEOUT,
    OFFICIAL_MCP_REGISTRY_HOST,
    OFFICIAL_MCP_REGISTRY_PROVIDER_KEY,
    OfficialMCPRegistryProvider,
)
from app.discovery.provider import (
    RoutedExternalMCPDiscoveryProvider,
    UnavailableExternalMCPProvider,
)
from app.domain.enums import (
    ExternalMCPReviewDecision,
    ExternalMCPSearchStatus,
    ExternalMCPSourceType,
    MCPServerStatus,
    MCPTransportType,
    UserStatus,
)
from app.repositories.external_discovery import ExternalDiscoveryRepository
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
from sqlalchemy.ext.asyncio import AsyncSession


def _source(
    *,
    provider_key: str = OFFICIAL_MCP_REGISTRY_PROVIDER_KEY,
    base_url: str = "https://evil.example/override",
) -> Any:
    """Minimal source stand-in — provider must ignore hostile base_url."""

    class _Src:
        pass

    src = _Src()
    src.id = uuid.uuid4()
    src.provider_key = provider_key
    src.base_url = base_url
    src.enabled = True
    src.source_type = ExternalMCPSourceType.REGISTRY.value
    return src


def _server_entry(
    *,
    name: str,
    remotes: list[dict[str, Any]] | None = None,
    packages: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    server: dict[str, Any] = {
        "name": name,
        "description": extra.pop("description", f"desc for {name}"),
        "version": extra.pop("version", "1.0.0"),
    }
    server.update(extra)
    if remotes is not None:
        server["remotes"] = remotes
    if packages is not None:
        server["packages"] = packages
    return {"server": server, "_meta": {}}


@pytest.mark.asyncio
async def test_search_query_and_latest_version_params() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.host == OFFICIAL_MCP_REGISTRY_HOST
        assert request.url.scheme == "https"
        assert request.url.path == "/v0.1/servers"
        assert request.url.params.get("search") == "weather"
        assert request.url.params.get("version") == "latest"
        assert request.url.params.get("limit") == "2"
        body = {
            "servers": [
                _server_entry(
                    name="io.example/weather",
                    title="Weather",
                    remotes=[
                        {
                            "type": "streamable-http",
                            "url": "https://weather.example/mcp",
                            "headers": [
                                {
                                    "name": "Authorization",
                                    "value": "Bearer SECRET",
                                    "isSecret": True,
                                }
                            ],
                        }
                    ],
                    repository={"url": "https://github.com/example/weather"},
                    websiteUrl="https://weather.example",
                )
            ],
            "metadata": {"count": 1},
        }
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=False
    ) as http:
        provider = OfficialMCPRegistryProvider(http=http)
        results = await provider.search(_source(), "weather", 2)

    assert len(seen) == 1
    assert len(results) == 1
    cand = results[0]
    assert cand.external_key == "io.example/weather"
    assert cand.name == "Weather"
    assert cand.transport_type == MCPTransportType.STREAMABLE_HTTP.value
    assert cand.endpoint_url == "https://weather.example/mcp"
    assert cand.repository_url == "https://github.com/example/weather"
    assert cand.homepage_url == "https://weather.example"
    # Credentials/headers from registry must never enter the candidate contract.
    assert not hasattr(cand, "headers")
    assert not hasattr(cand, "install_command")


@pytest.mark.asyncio
async def test_cursor_pagination_respects_limit() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        cursor = request.url.params.get("cursor")
        if cursor is None:
            return httpx.Response(
                200,
                json={
                    "servers": [
                        _server_entry(
                            name="a/one",
                            remotes=[
                                {
                                    "type": "streamable-http",
                                    "url": "https://a.example/mcp",
                                }
                            ],
                        ),
                        _server_entry(
                            name="a/two",
                            remotes=[
                                {
                                    "type": "streamable-http",
                                    "url": "https://b.example/mcp",
                                }
                            ],
                        ),
                    ],
                    "metadata": {"nextCursor": "page2", "count": 2},
                },
            )
        return httpx.Response(
            200,
            json={
                "servers": [
                    _server_entry(
                        name="a/three",
                        remotes=[
                            {
                                "type": "streamable-http",
                                "url": "https://c.example/mcp",
                            }
                        ],
                    )
                ],
                "metadata": {"count": 1},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=False
    ) as http:
        provider = OfficialMCPRegistryProvider(http=http)
        results = await provider.search(_source(), "a", 3)

    assert calls == 2
    assert [c.external_key for c in results] == ["a/one", "a/two", "a/three"]


@pytest.mark.asyncio
async def test_duplicate_external_keys_deduped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "servers": [
                    _server_entry(
                        name="dup/key",
                        remotes=[
                            {
                                "type": "streamable-http",
                                "url": "https://one.example/mcp",
                            }
                        ],
                    ),
                    _server_entry(
                        name="dup/key",
                        remotes=[
                            {
                                "type": "streamable-http",
                                "url": "https://two.example/mcp",
                            }
                        ],
                    ),
                ],
                "metadata": {"count": 2},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=False
    ) as http:
        provider = OfficialMCPRegistryProvider(http=http)
        results = await provider.search(_source(), "dup", 10)
    assert len(results) == 1
    assert results[0].endpoint_url == "https://one.example/mcp"


@pytest.mark.asyncio
async def test_package_only_candidate_has_no_fabricated_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "servers": [
                    _server_entry(
                        name="io.example/local-fs",
                        packages=[
                            {
                                "registryType": "npm",
                                "identifier": "@ex/fs",
                                "transport": {"type": "stdio"},
                                "runtimeArguments": [{"value": "-y"}],
                                "environmentVariables": [
                                    {"name": "TOKEN", "isSecret": True}
                                ],
                            }
                        ],
                        repository={"url": "https://github.com/example/fs"},
                    )
                ],
                "metadata": {"count": 1},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=False
    ) as http:
        provider = OfficialMCPRegistryProvider(http=http)
        results = await provider.search(_source(), "fs", 5)

    assert len(results) == 1
    assert results[0].external_key == "io.example/local-fs"
    assert results[0].repository_url == "https://github.com/example/fs"
    assert results[0].transport_type is None
    assert results[0].endpoint_url is None


@pytest.mark.asyncio
async def test_malformed_json_rejected_no_retry() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=b"not-json{")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=False
    ) as http:
        provider = OfficialMCPRegistryProvider(http=http)
        with pytest.raises(ExternalDiscoveryProviderError) as exc:
            await provider.search(_source(), "x", 5)
    assert exc.value.code == EXTERNAL_DISCOVERY_REGISTRY_INVALID_RESPONSE
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_oversized_response_rejected_no_retry() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            content=b"{" + b"a" * (official_registry_mod._MAX_RESPONSE_BYTES + 10),
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=False
    ) as http:
        provider = OfficialMCPRegistryProvider(http=http)
        with pytest.raises(ExternalDiscoveryProviderError) as exc:
            await provider.search(_source(), "x", 5)
    assert exc.value.code == EXTERNAL_DISCOVERY_REGISTRY_RESPONSE_TOO_LARGE
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_timeout_then_success_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    sleeps = {"n": 0}

    async def _no_sleep() -> None:
        sleeps["n"] += 1

    monkeypatch.setattr(official_registry_mod, "_sleep_before_retry", _no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("slow")
        return httpx.Response(
            200,
            json={
                "servers": [
                    _server_entry(
                        name="io.example/retry-ok",
                        title="Retry OK",
                        remotes=[
                            {
                                "type": "streamable-http",
                                "url": "https://ok.example/mcp",
                            }
                        ],
                    )
                ],
                "metadata": {"count": 1},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=False
    ) as http:
        provider = OfficialMCPRegistryProvider(http=http)
        results = await provider.search(_source(), "retry", 5)

    assert calls["n"] == 2
    assert sleeps["n"] == 1
    assert len(results) == 1
    assert results[0].external_key == "io.example/retry-ok"
    assert results[0].name == "Retry OK"


@pytest.mark.asyncio
async def test_timeout_twice_maps_to_stable_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    async def _no_sleep() -> None:
        return None

    monkeypatch.setattr(official_registry_mod, "_sleep_before_retry", _no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("slow")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=False
    ) as http:
        provider = OfficialMCPRegistryProvider(http=http)
        with pytest.raises(ExternalDiscoveryProviderError) as exc:
            await provider.search(_source(), "x", 5)
    assert exc.value.code == EXTERNAL_DISCOVERY_REGISTRY_TIMEOUT
    assert exc.value.message == "Official MCP Registry request timed out."
    assert calls["n"] == official_registry_mod._MAX_GET_ATTEMPTS == 2


@pytest.mark.asyncio
async def test_redirect_rejected_no_retry() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            302, headers={"Location": "https://evil.example/steal"}
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=False
    ) as http:
        provider = OfficialMCPRegistryProvider(http=http)
        with pytest.raises(ExternalDiscoveryProviderError) as exc:
            await provider.search(_source(), "x", 5)
    assert exc.value.code == EXTERNAL_DISCOVERY_REGISTRY_REDIRECT_REJECTED
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_http_4xx_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, json={"error": "missing"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=False
    ) as http:
        provider = OfficialMCPRegistryProvider(http=http)
        with pytest.raises(ExternalDiscoveryProviderError) as exc:
            await provider.search(_source(), "x", 5)
    assert exc.value.code == EXTERNAL_DISCOVERY_REGISTRY_HTTP_ERROR
    assert "404" in exc.value.message
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_hostile_base_url_cannot_redirect_host() -> None:
    seen_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host or "")
        return httpx.Response(
            200, json={"servers": [], "metadata": {"count": 0}}
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=False
    ) as http:
        provider = OfficialMCPRegistryProvider(http=http)
        await provider.search(
            _source(base_url="https://attacker.example/v0.1/servers"),
            "x",
            5,
        )
    assert seen_hosts == [OFFICIAL_MCP_REGISTRY_HOST]


@pytest.mark.asyncio
async def test_unknown_provider_key_unavailable() -> None:
    routed = RoutedExternalMCPDiscoveryProvider(
        official=OfficialMCPRegistryProvider(
            http=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(500, json={"error": "nope"})
                )
            )
        ),
        unavailable=UnavailableExternalMCPProvider(),
    )
    with pytest.raises(ExternalDiscoveryProviderError) as exc:
        await routed.search(_source(provider_key="glama.fake"), "x", 5)
    assert exc.value.code == EXTERNAL_DISCOVERY_PROVIDER_UNAVAILABLE


@pytest.mark.asyncio
async def test_no_secondary_fetch_of_repository_or_homepage() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "servers": [
                    _server_entry(
                        name="io.example/x",
                        remotes=[
                            {
                                "type": "streamable-http",
                                "url": "https://x.example/mcp",
                            }
                        ],
                        repository={"url": "https://github.com/example/x"},
                        websiteUrl="https://example.com/home",
                    )
                ],
                "metadata": {"count": 1},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=False
    ) as http:
        provider = OfficialMCPRegistryProvider(http=http)
        await provider.search(_source(), "x", 5)
    assert paths == ["/v0.1/servers"]


async def _seed_manager(session: AsyncSession) -> uuid.UUID:
    user = await UserService(session).create(
        UserCreate(
            username=f"reg-{uuid.uuid4().hex[:10]}",
            display_name="Registry Tester",
            email=f"reg-{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE,
        )
    )
    role = await RoleService(session).create(
        RoleCreate(code=f"reg-r-{uuid.uuid4().hex[:8]}", name="Reg Role")
    )
    perm_ids = []
    for code in ("mcp.server.manage", "mcp.server.read"):
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


@pytest.mark.asyncio
async def test_remote_http_candidate_imports_as_draft(
    db_session: AsyncSession,
) -> None:
    user_id = await _seed_manager(db_session)
    source = await ExternalDiscoveryRepository(db_session).create_source(
        code=f"official-{uuid.uuid4().hex[:8]}",
        name="Official",
        source_type=ExternalMCPSourceType.REGISTRY.value,
        provider_key=OFFICIAL_MCP_REGISTRY_PROVIDER_KEY,
        base_url="https://registry.modelcontextprotocol.io",
        enabled=True,
    )
    await db_session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "servers": [
                    _server_entry(
                        name="io.example/importable",
                        remotes=[
                            {
                                "type": "streamable-http",
                                "url": "https://importable.example/mcp",
                            }
                        ],
                    )
                ],
                "metadata": {"count": 1},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=False
    ) as http:
        provider = RoutedExternalMCPDiscoveryProvider(
            official=OfficialMCPRegistryProvider(http=http)
        )
        service = ExternalDiscoveryService(db_session, provider=provider)
        search = await service.create_search(
            user_id,
            ExternalMCPSearchCreate(source_id=source.id, q="importable", limit=5),
        )
    assert search.status == ExternalMCPSearchStatus.SUCCEEDED
    cid = search.candidates[0].id
    await service.create_review(
        user_id,
        cid,
        ExternalMCPReviewCreate(decision=ExternalMCPReviewDecision.APPROVE),
    )
    imported = await service.import_candidate(user_id, cid)
    assert imported.created is True
    assert imported.server_status == MCPServerStatus.DRAFT


@pytest.mark.asyncio
async def test_package_only_candidate_discoverable_but_not_importable(
    db_session: AsyncSession,
) -> None:
    from app.core.errors import AppError

    user_id = await _seed_manager(db_session)
    source = await ExternalDiscoveryRepository(db_session).create_source(
        code=f"official-{uuid.uuid4().hex[:8]}",
        name="Official",
        source_type=ExternalMCPSourceType.REGISTRY.value,
        provider_key=OFFICIAL_MCP_REGISTRY_PROVIDER_KEY,
        enabled=True,
    )
    await db_session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "servers": [
                    _server_entry(
                        name="io.example/stdio-only",
                        packages=[
                            {
                                "identifier": "@ex/stdio",
                                "transport": {"type": "stdio"},
                            }
                        ],
                    )
                ],
                "metadata": {"count": 1},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=False
    ) as http:
        provider = RoutedExternalMCPDiscoveryProvider(
            official=OfficialMCPRegistryProvider(http=http)
        )
        service = ExternalDiscoveryService(db_session, provider=provider)
        search = await service.create_search(
            user_id,
            ExternalMCPSearchCreate(source_id=source.id, q="stdio", limit=5),
        )
    assert search.status == ExternalMCPSearchStatus.SUCCEEDED
    assert search.candidate_count == 1
    assert search.candidates[0].endpoint_url is None
    cid = search.candidates[0].id
    await service.create_review(
        user_id,
        cid,
        ExternalMCPReviewCreate(decision=ExternalMCPReviewDecision.APPROVE),
    )
    with pytest.raises(AppError) as exc:
        await service.import_candidate(user_id, cid)
    assert exc.value.code == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_http_error_code_bounded_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream boom " + ("x" * 2000))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=False
    ) as http:
        provider = OfficialMCPRegistryProvider(http=http)
        with pytest.raises(ExternalDiscoveryProviderError) as exc:
            await provider.search(_source(), "x", 5)
    assert exc.value.code == EXTERNAL_DISCOVERY_REGISTRY_HTTP_ERROR
    assert "boom" not in exc.value.message
    assert "503" in exc.value.message


@pytest.mark.asyncio
async def test_response_must_not_leak_secret_headers_into_json_dump() -> None:
    """Candidate mapping drops remote headers; JSON dump stays credential-free."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "servers": [
                    _server_entry(
                        name="io.example/secret-remote",
                        remotes=[
                            {
                                "type": "streamable-http",
                                "url": "https://sec.example/mcp",
                                "headers": [
                                    {
                                        "name": "Authorization",
                                        "value": "Bearer super-secret-token",
                                    }
                                ],
                            }
                        ],
                    )
                ],
                "metadata": {"count": 1},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=False
    ) as http:
        provider = OfficialMCPRegistryProvider(http=http)
        results = await provider.search(_source(), "sec", 5)
    from dataclasses import asdict

    dumped = json.dumps(asdict(results[0]))
    assert "super-secret-token" not in dumped
    assert "Authorization" not in dumped
