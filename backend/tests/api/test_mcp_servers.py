"""SQLite-backed API tests for MCP Server Discovery vertical slice."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from app.domain.enums import MCPDiscoveryMode
from app.mcp.client import MCPHttpClient
from app.models.mcp import MCPServer
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fixtures.test_mcp_server import TestMCPScenario

API_SERVERS = "/api/v1/mcp/servers"
API_TOOLS = "/api/v1/mcp/tools"


class SpyMCPHttpClient(MCPHttpClient):
    """Counts remote MCP calls; subclasses should raise if invoked."""

    def __init__(self) -> None:
        super().__init__(
            http=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(500, text="should not be called")
                )
            )
        )
        self.calls = 0

    async def discover_capabilities(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        raise AssertionError("discover_capabilities should not be called")

    async def list_tools(self, *args: Any, **kwargs: Any) -> list[Any]:
        self.calls += 1
        raise AssertionError("list_tools should not be called")


def _server_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "Weather MCP",
        "transport_type": "STREAMABLE_HTTP",
        "endpoint_url": "https://mcp.test/mcp",
    }
    payload.update(overrides)
    return payload


async def _create_server(client: AsyncClient, **overrides: Any) -> dict[str, Any]:
    response = await client.post(API_SERVERS, json=_server_payload(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_create_streamable_http_server(db_client: AsyncClient) -> None:
    body = await _create_server(db_client, name="Echo MCP")
    assert body["transport_type"] == "STREAMABLE_HTTP"
    assert body["endpoint_url"] == "https://mcp.test/mcp"
    assert body["status"] == "DRAFT"
    assert body["protocol_era"] == "CURRENT"
    assert body["lock_version"] == 1
    assert "code" in body


@pytest.mark.asyncio
async def test_list_detail_patch_with_if_match(db_client: AsyncClient) -> None:
    created = await _create_server(db_client)
    server_id = created["id"]

    detail = await db_client.get(f"{API_SERVERS}/{server_id}")
    assert detail.status_code == 200
    assert detail.json()["id"] == server_id

    patched = await db_client.patch(
        f"{API_SERVERS}/{server_id}",
        headers={"If-Match": str(created["lock_version"])},
        json={"name": "Renamed MCP"},
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Renamed MCP"
    assert patched.json()["lock_version"] == created["lock_version"] + 1


@pytest.mark.asyncio
async def test_patch_optimistic_lock_conflict_409(db_client: AsyncClient) -> None:
    created = await _create_server(db_client)
    server_id = created["id"]

    response = await db_client.patch(
        f"{API_SERVERS}/{server_id}",
        headers={"If-Match": "999"},
        json={"name": "Stale"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RESOURCE_VERSION_CONFLICT"


@pytest.mark.asyncio
async def test_get_server_not_found_404(db_client: AsyncClient) -> None:
    missing = uuid.uuid4()
    response = await db_client.get(f"{API_SERVERS}/{missing}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_list_servers_pagination_and_filters(db_client: AsyncClient) -> None:
    await _create_server(db_client, name="Alpha Server")
    await _create_server(db_client, name="Beta Server")

    page1 = await db_client.get(API_SERVERS, params={"page": 1, "page_size": 1})
    assert page1.status_code == 200
    data = page1.json()
    assert data["total"] >= 2
    assert len(data["items"]) == 1
    assert data["has_next"] is True

    filtered = await db_client.get(
        API_SERVERS,
        params={"status": "DRAFT", "transport_type": "STREAMABLE_HTTP", "q": "Alpha"},
    )
    assert filtered.status_code == 200
    names = [item["name"] for item in filtered.json()["items"]]
    assert "Alpha Server" in names


@pytest.mark.asyncio
async def test_activate_and_deactivate(db_client: AsyncClient) -> None:
    created = await _create_server(db_client)
    server_id = created["id"]

    activated = await db_client.post(f"{API_SERVERS}/{server_id}/activate")
    assert activated.status_code == 200
    assert activated.json()["status"] == "ACTIVE"

    deactivated = await db_client.post(f"{API_SERVERS}/{server_id}/deactivate")
    assert deactivated.status_code == 200
    assert deactivated.json()["status"] == "INACTIVE"


@pytest.mark.asyncio
async def test_activate_invalid_transition_409(db_client: AsyncClient) -> None:
    created = await _create_server(db_client)
    server_id = created["id"]
    await db_client.post(f"{API_SERVERS}/{server_id}/activate")

    again = await db_client.post(f"{API_SERVERS}/{server_id}/activate")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "RESOURCE_CONFLICT"


@pytest.mark.asyncio
async def test_invalid_endpoint_url_422(db_client: AsyncClient) -> None:
    response = await db_client.post(
        API_SERVERS,
        json=_server_payload(endpoint_url="ftp://bad.example/mcp"),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_http_missing_endpoint_422(db_client: AsyncClient) -> None:
    response = await db_client.post(
        API_SERVERS,
        json={
            "name": "No URL",
            "transport_type": "STREAMABLE_HTTP",
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_stdio_requires_manifest_rejects_endpoint(db_client: AsyncClient) -> None:
    missing = await db_client.post(
        API_SERVERS,
        json={"name": "Stdio", "transport_type": "STDIO"},
    )
    assert missing.status_code == 422

    with_endpoint = await db_client.post(
        API_SERVERS,
        json={
            "name": "Stdio",
            "transport_type": "STDIO",
            "stdio_manifest_id": "manifest-1",
            "endpoint_url": "https://mcp.test/mcp",
        },
    )
    assert with_endpoint.status_code == 422

    ok = await db_client.post(
        API_SERVERS,
        json={
            "name": "Stdio",
            "transport_type": "STDIO",
            "stdio_manifest_id": "manifest-1",
        },
    )
    assert ok.status_code == 201
    assert ok.json()["stdio_manifest_id"] == "manifest-1"
    assert ok.json()["endpoint_url"] is None


@pytest.mark.asyncio
async def test_create_rejects_raw_credential_fields(db_client: AsyncClient) -> None:
    before = await db_client.get(API_SERVERS)
    assert before.status_code == 200
    total_before = before.json()["total"]

    response = await db_client.post(
        API_SERVERS,
        json={
            **_server_payload(),
            "password": "secret",
            "token": "abc",
            "api_key": "key-123",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    after = await db_client.get(API_SERVERS)
    assert after.status_code == 200
    assert after.json()["total"] == total_before


@pytest.mark.asyncio
async def test_patch_status_forbidden_422(db_client: AsyncClient) -> None:
    created = await _create_server(db_client)
    server_id = created["id"]

    with_body = await db_client.patch(
        f"{API_SERVERS}/{server_id}",
        json={"status": "ACTIVE", "lock_version": created["lock_version"]},
    )
    assert with_body.status_code == 422
    assert with_body.json()["error"]["code"] == "VALIDATION_ERROR"

    with_header = await db_client.patch(
        f"{API_SERVERS}/{server_id}",
        headers={"If-Match": str(created["lock_version"])},
        json={"status": "ACTIVE"},
    )
    assert with_header.status_code == 422
    assert with_header.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_invalid_timeout_and_concurrency_422(db_client: AsyncClient) -> None:
    for field, value in (
        ("connect_timeout_ms", 0),
        ("call_timeout_ms", 0),
        ("max_concurrency", 0),
    ):
        response = await db_client.post(
            API_SERVERS,
            json=_server_payload(**{field: value}),
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_stdio_connection_test_501(db_client: AsyncClient) -> None:
    created = await db_client.post(
        API_SERVERS,
        json={
            "name": "Stdio",
            "transport_type": "STDIO",
            "stdio_manifest_id": "manifest-1",
        },
    )
    server_id = created.json()["id"]
    response = await db_client.post(f"{API_SERVERS}/{server_id}/connection-tests")
    assert response.status_code == 501
    assert response.json()["error"]["code"] == "MCP_STDIO_UNSUPPORTED"


@pytest.mark.asyncio
async def test_legacy_transport_create_ok_connection_test_501(db_client: AsyncClient) -> None:
    created = await db_client.post(
        API_SERVERS,
        json=_server_payload(
            name="Legacy",
            transport_type="LEGACY_HTTP_SSE",
            endpoint_url="https://legacy.test/mcp",
        ),
    )
    assert created.status_code == 201
    server_id = created.json()["id"]
    response = await db_client.post(f"{API_SERVERS}/{server_id}/connection-tests")
    assert response.status_code == 501
    assert response.json()["error"]["code"] == "MCP_LEGACY_UNSUPPORTED"


@pytest.mark.asyncio
async def test_connection_test_success(
    db_client: AsyncClient,
    override_mcp_client,
) -> None:
    created = await _create_server(db_client)
    override_mcp_client(TestMCPScenario(TestMCPScenario.HEALTHY).build_http_client())

    response = await db_client.post(f"{API_SERVERS}/{created['id']}/connection-tests")
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "SUCCEEDED"
    assert body["discovery_mode"] == MCPDiscoveryMode.EXPLICIT_DISCOVERY

    detail = await db_client.get(f"{API_SERVERS}/{created['id']}")
    assert detail.json()["status"] == "DRAFT"
    assert detail.json()["discovery_mode"] == MCPDiscoveryMode.EXPLICIT_DISCOVERY


@pytest.mark.asyncio
async def test_connection_test_timeout_keeps_draft(
    db_client: AsyncClient,
    override_mcp_client,
) -> None:
    created = await _create_server(db_client, connect_timeout_ms=50, call_timeout_ms=50)
    override_mcp_client(TestMCPScenario(TestMCPScenario.TIMEOUT).build_http_client())

    response = await db_client.post(f"{API_SERVERS}/{created['id']}/connection-tests")
    assert response.status_code == 201
    assert response.json()["status"] == "TIMED_OUT"

    detail = await db_client.get(f"{API_SERVERS}/{created['id']}")
    assert detail.json()["status"] == "DRAFT"


@pytest.mark.asyncio
async def test_connection_test_failure_keeps_draft(
    db_client: AsyncClient,
    override_mcp_client,
) -> None:
    created = await _create_server(db_client)
    override_mcp_client(
        TestMCPScenario(TestMCPScenario.CONNECTION_FAILURE).build_http_client()
    )

    response = await db_client.post(f"{API_SERVERS}/{created['id']}/connection-tests")
    assert response.status_code == 201
    assert response.json()["status"] == "FAILED"

    detail = await db_client.get(f"{API_SERVERS}/{created['id']}")
    assert detail.json()["status"] == "DRAFT"


@pytest.mark.asyncio
async def test_connection_test_bearer_without_secret_failed(
    db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    secret_id = uuid.uuid4()
    created = await _create_server(
        db_client,
        auth_type="BEARER",
        auth_secret_id=str(secret_id),
    )

    async with db_session_factory() as session:
        result = await session.execute(
            select(MCPServer).where(MCPServer.id == uuid.UUID(created["id"]))
        )
        server = result.scalar_one()
        server.auth_secret_id = None
        await session.commit()

    response = await db_client.post(f"{API_SERVERS}/{created['id']}/connection-tests")
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "FAILED"
    assert body["error_code"] == "MCP_AUTH_SECRET_UNAVAILABLE"


@pytest.mark.asyncio
async def test_connection_test_bearer_with_secret_id_fail_closed_no_http(
    db_client: AsyncClient,
    override_mcp_client,
) -> None:
    secret_id = uuid.uuid4()
    created = await _create_server(
        db_client,
        auth_type="BEARER",
        auth_secret_id=str(secret_id),
    )
    spy = SpyMCPHttpClient()
    override_mcp_client(spy)

    response = await db_client.post(f"{API_SERVERS}/{created['id']}/connection-tests")
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "FAILED"
    assert body["error_code"] == "MCP_AUTH_SECRET_UNAVAILABLE"
    assert spy.calls == 0


@pytest.mark.asyncio
async def test_discovery_bearer_with_secret_id_fail_closed_no_http(
    db_client: AsyncClient,
    override_mcp_client,
) -> None:
    secret_id = uuid.uuid4()
    created = await _create_server(
        db_client,
        auth_type="BEARER",
        auth_secret_id=str(secret_id),
    )
    spy = SpyMCPHttpClient()
    override_mcp_client(spy)

    response = await db_client.post(
        f"{API_SERVERS}/{created['id']}/discoveries",
        json={"apply_changes": False},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MCP_AUTH_SECRET_UNAVAILABLE"
    assert spy.calls == 0


@pytest.mark.asyncio
async def test_discovery_apply_malformed_schema_invalid_version(
    db_client: AsyncClient,
    override_mcp_client,
) -> None:
    created = await _create_server(db_client)
    override_mcp_client(
        TestMCPScenario(TestMCPScenario.MALFORMED_SCHEMA).build_http_client()
    )
    server_id = created["id"]

    response = await db_client.post(
        f"{API_SERVERS}/{server_id}/discoveries",
        json={"apply_changes": True},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["success"] is True
    assert body["diff"]["added"] == 2

    tools = await db_client.get(
        f"{API_SERVERS}/{server_id}/tools",
        params={"q": "broken_tool"},
    )
    assert tools.status_code == 200
    assert tools.json()["total"] == 1
    broken = tools.json()["items"][0]
    assert broken["remote_name"] == "broken_tool"

    versions = await db_client.get(f"{API_TOOLS}/{broken['id']}/versions")
    assert versions.status_code == 200
    assert versions.json()["total"] == 1
    version = versions.json()["items"][0]
    assert version["validation_status"] == "INVALID"
    assert version["input_schema"] == ["not", "an", "object"]


@pytest.mark.asyncio
async def test_discovery_malformed_schema_fingerprint_change(
    db_client: AsyncClient,
    override_mcp_client,
) -> None:
    created = await _create_server(db_client)
    override_mcp_client(
        TestMCPScenario(TestMCPScenario.MALFORMED_SCHEMA).build_http_client()
    )
    server_id = created["id"]

    await db_client.post(
        f"{API_SERVERS}/{server_id}/discoveries",
        json={"apply_changes": True},
    )

    second = await db_client.post(
        f"{API_SERVERS}/{server_id}/discoveries",
        json={"apply_changes": True},
    )
    assert second.status_code == 201
    body = second.json()
    assert body["success"] is True
    assert body["diff"]["changed"] == 1
    assert body["diff"]["unchanged"] == 1

    tools = await db_client.get(
        f"{API_SERVERS}/{server_id}/tools",
        params={"q": "broken_tool"},
    )
    broken = tools.json()["items"][0]
    versions = await db_client.get(f"{API_TOOLS}/{broken['id']}/versions")
    assert versions.json()["total"] == 2
    hashes = {item["content_hash"] for item in versions.json()["items"]}
    assert len(hashes) == 2


@pytest.mark.asyncio
async def test_discovery_preview_does_not_apply_tools(
    db_client: AsyncClient,
    override_mcp_client,
) -> None:
    created = await _create_server(db_client)
    override_mcp_client(TestMCPScenario(TestMCPScenario.HEALTHY).build_http_client())

    response = await db_client.post(
        f"{API_SERVERS}/{created['id']}/discoveries",
        json={"apply_changes": False},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["success"] is True
    assert body["diff"]["added"] == 2
    assert body["apply_changes"] is False

    tools = await db_client.get(f"{API_SERVERS}/{created['id']}/tools")
    assert tools.json()["total"] == 0


@pytest.mark.asyncio
async def test_discovery_apply_creates_tools_and_versions(
    db_client: AsyncClient,
    override_mcp_client,
) -> None:
    created = await _create_server(db_client)
    override_mcp_client(TestMCPScenario(TestMCPScenario.HEALTHY).build_http_client())

    response = await db_client.post(
        f"{API_SERVERS}/{created['id']}/discoveries",
        json={"apply_changes": True},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["success"] is True
    assert body["diff"]["added"] == 2

    tools = await db_client.get(f"{API_SERVERS}/{created['id']}/tools")
    assert tools.json()["total"] == 2
    for item in tools.json()["items"]:
        assert item["status"] == "DISCOVERED"
        assert item["current_version_id"] is not None


@pytest.mark.asyncio
async def test_discovery_twice_unchanged(
    db_client: AsyncClient,
    override_mcp_client,
) -> None:
    created = await _create_server(db_client)
    override_mcp_client(TestMCPScenario(TestMCPScenario.HEALTHY).build_http_client())
    server_id = created["id"]

    first = await db_client.post(
        f"{API_SERVERS}/{server_id}/discoveries",
        json={"apply_changes": True},
    )
    assert first.json()["diff"]["added"] == 2

    second = await db_client.post(
        f"{API_SERVERS}/{server_id}/discoveries",
        json={"apply_changes": True},
    )
    body = second.json()
    assert body["diff"]["added"] == 0
    assert body["diff"]["unchanged"] == 2

    tools = await db_client.get(f"{API_SERVERS}/{server_id}/tools")
    assert tools.json()["total"] == 2


@pytest.mark.asyncio
async def test_discovery_schema_change_creates_new_version(
    db_client: AsyncClient,
    override_mcp_client,
) -> None:
    created = await _create_server(db_client)
    scenario = TestMCPScenario(TestMCPScenario.SCHEMA_CHANGE)
    override_mcp_client(scenario.build_http_client())
    server_id = created["id"]

    await db_client.post(
        f"{API_SERVERS}/{server_id}/discoveries",
        json={"apply_changes": True},
    )

    changed = await db_client.post(
        f"{API_SERVERS}/{server_id}/discoveries",
        json={"apply_changes": True},
    )
    assert changed.json()["diff"]["changed"] == 1
    assert changed.json()["diff"]["unchanged"] == 1

    tools = await db_client.get(f"{API_SERVERS}/{server_id}/tools", params={"q": "echo"})
    echo = tools.json()["items"][0]
    versions = await db_client.get(f"{API_TOOLS}/{echo['id']}/versions")
    assert versions.json()["total"] == 2


@pytest.mark.asyncio
async def test_discovery_tool_removed_apply_marks_missing(
    db_client: AsyncClient,
    override_mcp_client,
) -> None:
    created = await _create_server(db_client)
    scenario = TestMCPScenario(TestMCPScenario.TOOL_REMOVED)
    override_mcp_client(scenario.build_http_client())
    server_id = created["id"]

    await db_client.post(
        f"{API_SERVERS}/{server_id}/discoveries",
        json={"apply_changes": True},
    )

    preview = await db_client.post(
        f"{API_SERVERS}/{server_id}/discoveries",
        json={"apply_changes": False},
    )
    assert preview.json()["diff"]["missing"] == 1

    applied = await db_client.post(
        f"{API_SERVERS}/{server_id}/discoveries",
        json={"apply_changes": True},
    )
    assert applied.json()["diff"]["missing"] == 1

    tools = await db_client.get(
        f"{API_SERVERS}/{server_id}/tools",
        params={"status": "MISSING"},
    )
    assert tools.json()["total"] == 1
    assert tools.json()["items"][0]["remote_name"] == "lookup_weather"


@pytest.mark.asyncio
async def test_discovery_discover_unsupported_inferred_current(
    db_client: AsyncClient,
    override_mcp_client,
) -> None:
    created = await _create_server(db_client)
    override_mcp_client(
        TestMCPScenario(TestMCPScenario.DISCOVER_UNSUPPORTED).build_http_client()
    )

    response = await db_client.post(
        f"{API_SERVERS}/{created['id']}/discoveries",
        json={"apply_changes": True},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["success"] is True
    assert body["discovery_mode"] == MCPDiscoveryMode.INFERRED_CURRENT
    assert body["diff"]["added"] == 2


@pytest.mark.asyncio
async def test_tool_query_endpoints(
    db_client: AsyncClient,
    override_mcp_client,
) -> None:
    created = await _create_server(db_client, name="Tool Query Server")
    override_mcp_client(TestMCPScenario(TestMCPScenario.HEALTHY).build_http_client())
    server_id = created["id"]

    await db_client.post(
        f"{API_SERVERS}/{server_id}/discoveries",
        json={"apply_changes": True},
    )

    server_tools = await db_client.get(f"{API_SERVERS}/{server_id}/tools")
    assert server_tools.status_code == 200
    assert server_tools.json()["total"] == 2

    global_tools = await db_client.get(
        API_TOOLS,
        params={"mcp_server_id": server_id, "q": "echo"},
    )
    assert global_tools.status_code == 200
    assert global_tools.json()["total"] >= 1

    tool_id = global_tools.json()["items"][0]["id"]
    detail = await db_client.get(f"{API_TOOLS}/{tool_id}")
    assert detail.status_code == 200
    assert detail.json()["remote_name"]

    versions = await db_client.get(f"{API_TOOLS}/{tool_id}/versions")
    assert versions.status_code == 200
    assert versions.json()["total"] >= 1
    version_id = versions.json()["items"][0]["id"]

    version_detail = await db_client.get(f"{API_TOOLS}/{tool_id}/versions/{version_id}")
    assert version_detail.status_code == 200
    assert version_detail.json()["input_schema"]["type"] == "object"

    missing_tool = await db_client.get(f"{API_TOOLS}/{uuid.uuid4()}")
    assert missing_tool.status_code == 404

    missing_version = await db_client.get(
        f"{API_TOOLS}/{tool_id}/versions/{uuid.uuid4()}"
    )
    assert missing_version.status_code == 404
