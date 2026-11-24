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
    assert captured["headers"]["accept"] == "application/json, text/event-stream"
    assert body["params"]["_meta"] == {
        "io.modelcontextprotocol/protocolVersion": CURRENT_MCP_PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {
            "name": "mcpflow",
            "version": "0.1.0",
        },
    }


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
    assert exc.value.outcome_unknown is True
    assert exc.value.error_layer == "PROTOCOL"
    # The raised error must never carry/retain the oversized body content.
    assert big_chunk.decode() not in str(exc.value)
    assert big_chunk.decode() not in exc.value.message
    assert not hasattr(exc.value, "body")
    assert not hasattr(exc.value, "content")


@pytest.mark.asyncio
async def test_input_required_returns_normalized_without_leaking_request_state() -> None:
    from app.mcp.contracts import NormalizedInputRequired, NormalizedToolResult

    canary = {"token": "opaque-state-blob-should-never-leak", "n": 1}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "resultType": "input_required",
                    "inputRequests": {"otp": {"type": "string"}},
                    "requestState": canary,
                },
            },
        )

    result, response_meta, first_byte_at = await _call(handler)

    assert isinstance(result, NormalizedInputRequired)
    assert not isinstance(result, NormalizedToolResult)
    assert result.input_requests == {"otp": {"type": "string"}}
    assert result.request_state == canary
    assert result.result_type == "input_required"
    assert first_byte_at is not None
    assert "requestState" not in response_meta
    assert "opaque-state-blob-should-never-leak" not in json.dumps(response_meta)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result_body",
    [
        {"resultType": "input_required", "requestState": "x"},  # missing inputRequests
        {
            "resultType": "input_required",
            "inputRequests": {},
            "requestState": "x",
        },  # empty
        {
            "resultType": "input_required",
            "inputRequests": ["otp"],
            "requestState": "x",
        },  # wrong type
        {
            "resultType": "input_required",
            "inputRequests": {"otp": {}},
        },  # missing requestState
    ],
)
async def test_malformed_input_required_is_post_send_protocol_failure(
    result_body: dict[str, Any],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": result_body},
        )

    with pytest.raises(MCPClientError) as exc:
        await _call(handler)

    assert exc.value.error_code == "MCP_INVALID_INPUT_REQUIRED"
    assert exc.value.retryable is False
    assert exc.value.outcome_unknown is True


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
async def test_valid_input_required_returns_normalized_input_required(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.mcp.contracts import NormalizedInputRequired, NormalizedToolResult

    canary = "MRTR-CANARY-REQUEST-STATE-DO-NOT-LOG"
    input_requests = {
        "city": {"type": "string", "description": "City name"},
        "units": {"type": "string", "enum": ["c", "f"]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "resultType": "input_required",
                    "inputRequests": input_requests,
                    "requestState": {"opaque": True, "token": canary},
                },
            },
        )

    with caplog.at_level("DEBUG", logger="app.mcp"):
        result, response_meta, first_byte_at = await _call(handler)

    assert isinstance(result, NormalizedInputRequired)
    assert not isinstance(result, NormalizedToolResult)
    assert result.input_requests == input_requests
    assert result.request_state == {"opaque": True, "token": canary}
    assert result.result_type == "input_required"
    assert result.raw_size_bytes > 0
    assert first_byte_at is not None
    assert "requestState" not in response_meta
    assert "request_state" not in response_meta
    assert canary not in json.dumps(response_meta)
    for record in caplog.records:
        if record.name.startswith("app."):
            assert canary not in record.getMessage()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result_body", "message_part"),
    [
        (
            {"resultType": "input_required", "requestState": {"k": 1}},
            "inputRequests",
        ),
        (
            {
                "resultType": "input_required",
                "inputRequests": {},
                "requestState": {"k": 1},
            },
            "inputRequests",
        ),
        (
            {
                "resultType": "input_required",
                "inputRequests": ["not-an-object"],
                "requestState": {"k": 1},
            },
            "inputRequests",
        ),
        (
            {
                "resultType": "input_required",
                "inputRequests": {"field": {"type": "string"}},
            },
            "requestState",
        ),
    ],
)
async def test_malformed_input_required_message_includes_missing_field(
    result_body: dict[str, Any],
    message_part: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": result_body},
        )

    with pytest.raises(MCPClientError) as exc:
        await _call(handler)

    assert exc.value.error_layer == "PROTOCOL"
    assert exc.value.error_code == "MCP_INVALID_INPUT_REQUIRED"
    assert message_part in exc.value.message
    assert exc.value.retryable is False
    assert exc.value.outcome_unknown is True


@pytest.mark.asyncio
async def test_invalid_tool_result_object_after_send_outcome_unknown_true() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": ["not-an-object"]},
        )

    with pytest.raises(MCPClientError) as exc:
        await _call(handler)

    assert exc.value.error_code == "MCP_INVALID_TOOL_RESULT"
    assert exc.value.outcome_unknown is True


@pytest.mark.asyncio
async def test_invalid_content_after_send_outcome_unknown_true() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"content": "not-an-array"},
            },
        )

    with pytest.raises(MCPClientError) as exc:
        await _call(handler)

    assert exc.value.error_code == "MCP_INVALID_TOOL_RESULT"
    assert "content" in exc.value.message
    assert exc.value.outcome_unknown is True


@pytest.mark.asyncio
async def test_invalid_structured_content_after_send_outcome_unknown_true() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"content": [], "structuredContent": "not-object-or-array"},
            },
        )

    with pytest.raises(MCPClientError) as exc:
        await _call(handler)

    assert exc.value.error_code == "MCP_INVALID_TOOL_RESULT"
    assert "structuredContent" in exc.value.message
    assert exc.value.outcome_unknown is True


@pytest.mark.asyncio
async def test_http_500_after_send_outcome_unknown_true() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal"})

    with pytest.raises(MCPClientError) as exc:
        await _call(handler)

    assert exc.value.error_layer == "NETWORK"
    assert exc.value.error_code == "MCP_HTTP_SERVER_ERROR"
    assert exc.value.retryable is True
    assert exc.value.outcome_unknown is True


@pytest.mark.asyncio
async def test_http_400_after_send_outcome_unknown_false() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad request"})

    with pytest.raises(MCPClientError) as exc:
        await _call(handler)

    assert exc.value.error_code == "MCP_HTTP_CLIENT_ERROR"
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


@pytest.mark.asyncio
async def test_sse_tool_result_stops_at_reply_before_eof() -> None:
    class ReplyStream(httpx.AsyncByteStream):
        def __init__(self, reply: bytes) -> None:
            self.reply = reply
            self.closed = False

        async def __aiter__(self):
            yield b": heartbeat\r\n\r\n"
            yield b"data: \r\n\r\n"
            yield b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
            yield b"event: message\r\nda"
            yield b"ta: " + self.reply[:12]
            yield self.reply[12:] + b"\r\n\r\n"
            raise AssertionError("must not wait for stream EOF after response")

        async def aclose(self) -> None:
            self.closed = True

    streams: list[ReplyStream] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        reply = json.dumps({
            "jsonrpc": "2.0",
            "id": body["id"],
            "result": {"content": [{"type": "text", "text": "ok"}]},
        }).encode()
        stream = ReplyStream(reply)
        streams.append(stream)
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream; charset=utf-8"},
            stream=stream,
        )

    result, meta, first_byte = await _call(handler, max_result_bytes=4096)
    assert result.protocol_success is True
    assert result.content == [{"type": "text", "text": "ok"}]
    assert result.raw_size_bytes > len(streams[0].reply)
    assert meta["http_status"] == 200
    assert first_byte is not None
    assert streams[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        (b"data: not-json\n\n", "MCP_INVALID_JSON"),
        (b": heartbeat\n\n", "MCP_SSE_RESPONSE_MISSING"),
        (
            b'data: {"jsonrpc":"2.0","id":"wrong","result":{}}\n\n',
            "MCP_INVALID_JSONRPC",
        ),
    ],
)
async def test_sse_failure_preserves_only_safe_metadata(
    body: bytes, expected_code: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/event-stream",
                "Set-Cookie": "secret=must-not-retain",
                "X-Private": "secret-canary",
            },
            content=body,
        )

    with pytest.raises(MCPClientError) as exc:
        await _call(handler)

    assert exc.value.error_code == expected_code
    assert exc.value.retryable is False
    assert exc.value.outcome_unknown is True
    meta = exc.value.response_meta
    assert meta is not None
    assert meta["http_status"] == 200
    assert meta["response_headers"] == {"content-type": "text/event-stream"}
    assert "secret" not in json.dumps(meta)


@pytest.mark.asyncio
async def test_sse_heartbeat_counts_toward_tool_policy_byte_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=b": " + b"x" * 100 + b"\n\n",
        )

    with pytest.raises(MCPResultTooLargeError) as exc:
        await _call(handler, max_result_bytes=32)

    assert exc.value.error_code == "MCP_RESULT_TOO_LARGE"
    assert exc.value.outcome_unknown is True
    assert exc.value.retryable is False


@pytest.mark.asyncio
async def test_sse_input_required_preserves_opaque_state() -> None:
    from app.mcp.contracts import NormalizedInputRequired

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": body["id"],
            "result": {
                "resultType": "input_required",
                "inputRequests": {"otp": {"type": "string"}},
                "requestState": {"token": "opaque-canary"},
            },
        }).encode()
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"},
            content=b"data: " + payload + b"\n\n",
        )

    result, meta, _first = await _call(handler)
    assert isinstance(result, NormalizedInputRequired)
    assert result.request_state == {"token": "opaque-canary"}
    assert "opaque-canary" not in json.dumps(meta)


@pytest.mark.asyncio
async def test_invalid_json_retains_only_safe_content_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Type": "text/html", "X-Private": "canary"},
            content=b"<html>private body canary</html>",
        )

    with pytest.raises(MCPClientError) as exc:
        await _call(handler)

    assert exc.value.error_code == "MCP_INVALID_JSON"
    assert exc.value.outcome_unknown is True
    assert exc.value.response_meta is not None
    assert exc.value.response_meta["http_status"] == 200
    assert exc.value.response_meta["response_headers"] == {"content-type": "text/html"}
    assert "canary" not in json.dumps(exc.value.response_meta)


@pytest.mark.asyncio
async def test_sse_total_deadline_is_bounded_even_with_heartbeats() -> None:
    import asyncio

    class Heartbeats(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.005)
                yield b": heartbeat\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"},
            stream=Heartbeats(),
        )

    with pytest.raises(MCPClientError) as exc:
        await asyncio.wait_for(_call(handler, timeout_ms=50), timeout=2)

    assert exc.value.error_code == "MCP_CONNECTION_TIMEOUT"
    assert exc.value.outcome_unknown is True
    assert exc.value.response_meta is not None
    assert exc.value.response_meta["response_headers"] == {"content-type": "text/event-stream"}

