"""Unit tests for MCP discovery client, normalize, and schemas."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import pytest
from app.domain.enums import (
    CURRENT_MCP_PROTOCOL_VERSION,
    MCPProtocolEra,
    MCPTransportType,
    ToolVersionValidationStatus,
)
from app.mcp import (
    CurrentMCPClient,
    DiscoverUnsupportedError,
    MCPClientError,
    MCPHttpClient,
    RemoteToolDescriptor,
    content_hash,
    validate_tool_schemas,
)
from app.schemas.mcp_server import (
    DiscoveryCreateRequest,
    MCPServerCreate,
    MCPServerResponse,
)
from app.schemas.mcp_tool import MCPToolResponse, MCPToolVersionResponse
from pydantic import ValidationError


def test_content_hash_stable_sorted_safe_fields_only() -> None:
    desc = RemoteToolDescriptor(
        name="weather",
        description="lookup",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        output_schema={"type": "object"},
        annotations={"readOnlyHint": True},
        raw={"name": "weather", "secret": "SHOULD_NOT_HASH"},
    )
    expected_payload = {
        "annotations": {"readOnlyHint": True},
        "description": "lookup",
        "inputSchema": {"properties": {"q": {"type": "string"}}, "type": "object"},
        "name": "weather",
        "outputSchema": {"type": "object"},
    }
    encoded = json.dumps(
        expected_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    expected = hashlib.sha256(encoded).hexdigest()
    assert content_hash(desc) == expected
    assert "secret" not in json.dumps(
        {
            "name": desc.name,
            "description": desc.description,
            "inputSchema": desc.input_schema,
            "outputSchema": desc.output_schema,
            "annotations": desc.annotations,
        }
    )


@pytest.mark.parametrize(
    ("input_schema", "output_schema", "status"),
    [
        (None, None, ToolVersionValidationStatus.VALID),
        ({"type": "object"}, {"type": "object"}, ToolVersionValidationStatus.VALID),
        ({"properties": {}}, None, ToolVersionValidationStatus.WARNING),
        (["not", "a", "dict"], None, ToolVersionValidationStatus.INVALID),
        ({"type": "array"}, None, ToolVersionValidationStatus.INVALID),
    ],
)
def test_validate_tool_schemas(
    input_schema: Any,
    output_schema: Any,
    status: ToolVersionValidationStatus,
) -> None:
    result, errors = validate_tool_schemas(input_schema, output_schema)
    assert result == status
    if status == ToolVersionValidationStatus.VALID:
        assert errors == []
    else:
        assert errors


@pytest.mark.asyncio
async def test_discover_capabilities_wire_format() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": captured["body"]["id"],
                "result": {"tools": {"listChanged": True}},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = CurrentMCPClient(http=http)
        result = await client.discover_capabilities("https://mcp.example/mcp", timeout_ms=5000)

    assert result == {"tools": {"listChanged": True}}
    assert captured["headers"]["mcp-protocol-version"] == CURRENT_MCP_PROTOCOL_VERSION
    assert captured["headers"]["mcp-method"] == "server/discover"
    assert captured["headers"]["content-type"] == "application/json"
    body = captured["body"]
    assert body["jsonrpc"] == "2.0"
    assert body["method"] == "server/discover"
    meta = body["params"]["_meta"]
    assert meta["io.modelcontextprotocol/protocolVersion"] == CURRENT_MCP_PROTOCOL_VERSION
    assert "clientInfo" in meta
    assert "authorization" not in {k.lower() for k in captured["headers"]}


@pytest.mark.asyncio
async def test_discover_method_not_found_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {"code": -32601, "message": "Method not found"},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = CurrentMCPClient(http=http)
        with pytest.raises(DiscoverUnsupportedError) as exc:
            await client.discover_capabilities("https://mcp.example/mcp")
    assert exc.value.error_layer == "PROTOCOL"
    assert exc.value.retryable is False


@pytest.mark.asyncio
async def test_list_tools_pagination() -> None:
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        cursor = (body.get("params") or {}).get("cursor")
        if cursor is None:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "tools": [
                            {
                                "name": "a",
                                "description": "A",
                                "inputSchema": {"type": "object"},
                            }
                        ],
                        "nextCursor": "page-2",
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "tools": [{"name": "b", "inputSchema": {"type": "object"}}],
                },
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = CurrentMCPClient(http=http)
        tools = await client.list_tools("https://mcp.example/mcp")

    assert [t.name for t in tools] == ["a", "b"]
    assert calls[0]["method"] == "tools/list"
    assert "cursor" not in calls[0]["params"]
    assert calls[1]["params"]["cursor"] == "page-2"


@pytest.mark.asyncio
async def test_timeout_maps_to_mcp_connection_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = CurrentMCPClient(http=http)
        with pytest.raises(MCPClientError) as exc:
            await client.discover_capabilities("https://mcp.example/mcp", timeout_ms=1)
    assert exc.value.error_layer == "TIMEOUT"
    assert exc.value.error_code == "MCP_CONNECTION_TIMEOUT"
    assert exc.value.retryable is True


@pytest.mark.asyncio
async def test_auth_http_maps_to_auth_layer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = CurrentMCPClient(http=http)
        with pytest.raises(MCPClientError) as exc:
            await client.list_tools("https://mcp.example/mcp")
    assert exc.value.error_layer == "AUTH"


@pytest.mark.asyncio
async def test_http_facade_rejects_legacy() -> None:
    client = MCPHttpClient()
    with pytest.raises(MCPClientError) as exc:
        await client.discover_capabilities(
            "https://mcp.example/mcp",
            protocol_era=MCPProtocolEra.LEGACY,
        )
    assert exc.value.error_layer == "PROTOCOL"
    assert exc.value.error_code == "MCP_LEGACY_UNSUPPORTED"


def test_mcp_server_create_defaults() -> None:
    created = MCPServerCreate(
        name="Weather MCP",
        transport_type=MCPTransportType.STREAMABLE_HTTP,
        endpoint_url="https://mcp.example/mcp",
    )
    assert created.auth_type == "NONE"
    assert created.connect_timeout_ms == 10000
    assert created.call_timeout_ms == 60000
    assert created.max_concurrency == 5
    assert created.description is None


def test_discovery_create_defaults() -> None:
    req = DiscoveryCreateRequest()
    assert req.mode == "FULL"
    assert req.apply_changes is False


def test_mcp_server_response_rejects_unknown_secret_field() -> None:
    # Response model should not require secret plaintext fields.
    fields = set(MCPServerResponse.model_fields)
    assert "auth_secret_id" in fields
    assert "password" not in fields
    assert "token" not in fields


def test_tool_schema_models_importable() -> None:
    assert "remote_name" in MCPToolResponse.model_fields
    assert "content_hash" in MCPToolVersionResponse.model_fields
    with pytest.raises(ValidationError):
        MCPServerCreate(name="", transport_type=MCPTransportType.STREAMABLE_HTTP)
