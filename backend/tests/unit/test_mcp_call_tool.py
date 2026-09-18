"""Unit tests for ``CurrentMCPClient.call_tool`` (docs/04 §14 tools/call)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from app.domain.enums import CURRENT_MCP_PROTOCOL_VERSION
from app.mcp.current import CurrentMCPClient
from app.mcp.errors import MCPClientError, MCPResultTooLargeError


def _rpc_id() -> str:
    return str(uuid.uuid4())


async def _call(
    handler,
    *,
    tool_name: str = "weather_lookup",
    arguments: dict[str, Any] | None = None,
    auth_headers: dict[str, str] | None = None,
    max_result_bytes: int | None = None,
    remote_request_id: str | None = None,
    timeout_ms: int = 5000,
):
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = CurrentMCPClient(http=http)
        return await client.call_tool(
            "https://mcp.example/mcp",
            tool_name=tool_name,
            arguments=arguments or {"location": "Seoul"},
            timeout_ms=timeout_ms,
            remote_request_id=remote_request_id or _rpc_id(),
            auth_headers=auth_headers,
            max_result_bytes=max_result_bytes,
        )


@pytest.mark.asyncio
async def test_happy_path_returns_structured_content() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": captured["body"]["id"],
                "result": {
                    "content": [{"type": "text", "text": "sunny"}],
                    "structuredContent": {"temp_c": 21},
                    "isError": False,
                },
            },
        )

    result, response_meta, first_byte_at = await _call(handler)

    assert result.protocol_success is True
    assert result.tool_error is False
    assert result.structured_content == {"temp_c": 21}
    assert result.content == [{"type": "text", "text": "sunny"}]
    assert response_meta["http_status"] == 200
    assert first_byte_at is not None
    body = captured["body"]
    assert body["method"] == "tools/call"
    assert body["params"]["name"] == "weather_lookup"
    assert body["params"]["arguments"] == {"location": "Seoul"}
    assert captured["headers"]["mcp-protocol-version"] == CURRENT_MCP_PROTOCOL_VERSION
    assert captured["headers"]["mcp-name"] == "weather_lookup"


@pytest.mark.asyncio
async def test_is_error_true_marks_tool_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "content": [{"type": "text", "text": "boom"}],
                    "isError": True,
                },
            },
        )

    result, _meta, _first_byte = await _call(handler)
    assert result.protocol_success is True
    assert result.tool_error is True


@pytest.mark.asyncio
async def test_oversized_body_raises_without_retaining_body() -> None:
    big_chunk = b"x" * 100

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"structuredContent": {"blob": big_chunk.decode()}},
            }
        ).encode("utf-8")
        return httpx.Response(200, content=payload)

    with pytest.raises(MCPResultTooLargeError) as exc:
        await _call(handler, max_result_bytes=10)

    assert exc.value.error_code == "MCP_RESULT_TOO_LARGE"
    assert exc.value.retryable is False
    assert exc.value.outcome_unknown is False
    # The raised error must never carry/retain the oversized body content.
    assert big_chunk.decode() not in str(exc.value)
    assert not hasattr(exc.value, "body")
    assert not hasattr(exc.value, "content")


@pytest.mark.asyncio
async def test_input_required_raises_unsupported_without_leaking_request_state() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "resultType": "input_required",
                    "inputRequests": [{"name": "otp"}],
                    "requestState": "opaque-state-blob-should-never-leak",
                },
            },
        )

    with pytest.raises(MCPClientError) as exc:
        await _call(handler)

    assert exc.value.error_code == "MCP_INPUT_REQUIRED_UNSUPPORTED"
    assert exc.value.retryable is False
    assert "opaque-state-blob-should-never-leak" not in exc.value.message
    assert "opaque-state-blob-should-never-leak" not in str(exc.value)


@pytest.mark.asyncio
async def test_connect_timeout_outcome_unknown_false() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out", request=request)

    with pytest.raises(MCPClientError) as exc:
        await _call(handler)

    assert exc.value.error_layer == "TIMEOUT"
    assert exc.value.error_code == "MCP_CONNECTION_TIMEOUT"
    assert exc.value.retryable is True
    # Connect-phase timeout: request was never sent, no ambiguous side effect.
    assert exc.value.outcome_unknown is False


@pytest.mark.asyncio
async def test_pool_timeout_outcome_unknown_false() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.PoolTimeout("pool acquire timed out", request=request)

    with pytest.raises(MCPClientError) as exc:
        await _call(handler)

    assert exc.value.error_layer == "TIMEOUT"
    assert exc.value.outcome_unknown is False


@pytest.mark.asyncio
async def test_read_timeout_waiting_for_response_headers_outcome_unknown_true() -> None:
    """ReadTimeout before stream context sets request_sent must still be post-send.

    Waiting for response headers means the request was already dispatched; a
    headers-phase ReadTimeout must not be misclassified as pre-send.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("headers timed out", request=request)

    with pytest.raises(MCPClientError) as exc:
        await _call(handler)

    assert exc.value.error_layer == "TIMEOUT"
    assert exc.value.error_code == "MCP_CONNECTION_TIMEOUT"
    assert exc.value.outcome_unknown is True


@pytest.mark.asyncio
async def test_read_timeout_after_send_outcome_unknown_true() -> None:
    class _StreamThatTimesOutMidBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"jsonrpc": "2.0", '
            raise httpx.ReadTimeout("slow body", request=None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_StreamThatTimesOutMidBody())

    with pytest.raises(MCPClientError) as exc:
        await _call(handler)

    assert exc.value.error_layer == "TIMEOUT"
    assert exc.value.error_code == "MCP_CONNECTION_TIMEOUT"
    # The request was already sent and partially streamed — the external tool
    # call may already have taken effect, so outcome is ambiguous.
    assert exc.value.outcome_unknown is True


@pytest.mark.asyncio
async def test_write_timeout_outcome_unknown_true() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.WriteTimeout("write timed out", request=request)

    with pytest.raises(MCPClientError) as exc:
        await _call(handler)

    assert exc.value.error_layer == "TIMEOUT"
    assert exc.value.outcome_unknown is True


@pytest.mark.asyncio
async def test_invalid_json_after_send_outcome_unknown_true() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json{")

    with pytest.raises(MCPClientError) as exc:
        await _call(handler)

    assert exc.value.error_code == "MCP_INVALID_JSON"
    assert exc.value.retryable is False
    assert exc.value.outcome_unknown is True


@pytest.mark.asyncio
async def test_malformed_jsonrpc_after_send_outcome_unknown_true() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"jsonrpc": "1.0", "id": "ignored", "result": {}},
        )

    with pytest.raises(MCPClientError) as exc:
        await _call(handler)

    assert exc.value.error_code == "MCP_INVALID_JSONRPC"
    assert exc.value.retryable is False
    assert exc.value.outcome_unknown is True


@pytest.mark.asyncio
async def test_input_required_remains_outcome_unknown_false() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "resultType": "input_required",
                    "inputRequests": [{"name": "otp"}],
                    "requestState": "opaque",
                },
            },
        )

    with pytest.raises(MCPClientError) as exc:
        await _call(handler)

    assert exc.value.error_code == "MCP_INPUT_REQUIRED_UNSUPPORTED"
    assert exc.value.outcome_unknown is False


@pytest.mark.asyncio
async def test_authorization_header_present_on_request_but_never_in_errors() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(500, json={"error": "boom"})

    with pytest.raises(MCPClientError) as exc:
        await _call(handler, auth_headers={"Authorization": "Bearer super-secret-token"})

    assert captured["headers"]["authorization"] == "Bearer super-secret-token"
    assert "super-secret-token" not in exc.value.message
    assert "super-secret-token" not in str(exc.value)
    assert "Authorization" not in exc.value.message
    assert exc.value.error_layer == "NETWORK"
    assert exc.value.retryable is True


@pytest.mark.asyncio
async def test_authorization_header_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"content": [], "isError": False},
            },
        )

    with caplog.at_level("DEBUG"):
        await _call(handler, auth_headers={"Authorization": "Bearer super-secret-token"})

    for record in caplog.records:
        assert "super-secret-token" not in record.getMessage()
