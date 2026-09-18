"""Current MCP (2026-07-28) Streamable HTTP client — discovery, tools/list, tools/call."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from app.domain.enums import CURRENT_MCP_PROTOCOL_VERSION
from app.mcp.auth_headers import redact_headers_for_meta
from app.mcp.contracts import NormalizedToolResult
from app.mcp.errors import DiscoverUnsupportedError, MCPClientError, MCPResultTooLargeError
from app.mcp.normalize import RemoteToolDescriptor

logger = logging.getLogger(__name__)

_CLIENT_INFO = {"name": "mcpflow", "version": "0.1.0"}
_JSONRPC_METHOD_NOT_FOUND = -32601
_INPUT_REQUIRED_RESULT_TYPE = "input_required"


class CurrentMCPClient:
    """HTTP client for Current MCP wire format.

    ``Mcp-Name`` is set for named tool operations (``tools/call``) and omitted
    for ``server/discover`` / ``tools/list``.

    Never logs Authorization headers, request/response bodies, or MRTR
    ``requestState``.
    """

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http
        self._owns_http = http is None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient()
            self._owns_http = True
        return self._http

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    def _build_headers(self, method: str, *, mcp_name: str | None = None) -> dict[str, str]:
        headers = {
            "MCP-Protocol-Version": CURRENT_MCP_PROTOCOL_VERSION,
            "Mcp-Method": method,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if mcp_name:
            headers["Mcp-Name"] = mcp_name
        return headers

    def _build_params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": CURRENT_MCP_PROTOCOL_VERSION,
                "clientInfo": dict(_CLIENT_INFO),
            }
        }
        if extra:
            params.update(extra)
        return params

    def _build_body(self, method: str, params: dict[str, Any], request_id: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }

    def _map_http_error(self, status_code: int) -> MCPClientError:
        if status_code in {401, 403}:
            return MCPClientError(
                error_layer="AUTH",
                error_code="MCP_AUTH_FAILED",
                message="MCP server rejected authentication.",
                retryable=False,
            )
        if 400 <= status_code < 500:
            return MCPClientError(
                error_layer="PROTOCOL",
                error_code="MCP_HTTP_CLIENT_ERROR",
                message=f"MCP server returned HTTP {status_code}.",
                retryable=False,
            )
        return MCPClientError(
            error_layer="NETWORK",
            error_code="MCP_HTTP_SERVER_ERROR",
            message=f"MCP server returned HTTP {status_code}.",
            retryable=True,
        )

    def _map_transport_error(
        self, exc: Exception, *, request_sent: bool = False
    ) -> MCPClientError:
        """Map a transport-level exception with conservative outcome certainty.

        Classification for ``tools/call`` (and shared transport mapping):

        - ``ConnectTimeout`` / ``ConnectError`` / ``PoolTimeout``: request was
          never transmitted → ``outcome_unknown=false``
        - ``ReadTimeout`` / ``ReadError``: response-phase failure after dispatch
          (including waiting for response headers) → ``outcome_unknown=true``
        - ``WriteTimeout`` / ``WriteError``: transmission may have begun →
          ``outcome_unknown=true``
        - Remaining timeout/network types fall back to ``request_sent`` so
          pre-send failures stay non-ambiguous and post-send stay conservative.

        Do not blindly mark every NETWORK error unknown — connect/pool failures
        remain ``outcome_unknown=false``.
        """
        # Pre-send / pre-dispatch: connection never established, or pool wait.
        if isinstance(exc, (httpx.ConnectTimeout, httpx.PoolTimeout)):
            return MCPClientError(
                error_layer="TIMEOUT",
                error_code="MCP_CONNECTION_TIMEOUT",
                message="MCP server connection timed out.",
                retryable=True,
                outcome_unknown=False,
            )
        if isinstance(exc, httpx.ConnectError):
            return MCPClientError(
                error_layer="NETWORK",
                error_code="MCP_NETWORK_ERROR",
                message="Failed to connect to MCP server.",
                retryable=True,
                outcome_unknown=False,
            )

        # Post-dispatch / possible partial transmission.
        if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout)):
            return MCPClientError(
                error_layer="TIMEOUT",
                error_code="MCP_CONNECTION_TIMEOUT",
                message="MCP server connection timed out.",
                retryable=True,
                outcome_unknown=True,
            )
        if isinstance(exc, (httpx.ReadError, httpx.WriteError)):
            return MCPClientError(
                error_layer="NETWORK",
                error_code="MCP_NETWORK_ERROR",
                message="Failed to complete MCP request/response.",
                retryable=True,
                outcome_unknown=True,
            )

        if isinstance(exc, httpx.TimeoutException):
            return MCPClientError(
                error_layer="TIMEOUT",
                error_code="MCP_CONNECTION_TIMEOUT",
                message="MCP server connection timed out.",
                retryable=True,
                outcome_unknown=request_sent,
            )
        if isinstance(exc, httpx.NetworkError):
            return MCPClientError(
                error_layer="NETWORK",
                error_code="MCP_NETWORK_ERROR",
                message="Failed to connect to MCP server.",
                retryable=True,
                outcome_unknown=request_sent,
            )
        return MCPClientError(
            error_layer="PROTOCOL",
            error_code="MCP_PROTOCOL_ERROR",
            message="Unexpected MCP client transport failure.",
            retryable=False,
            outcome_unknown=False,
        )

    def _validate_jsonrpc_envelope(
        self,
        payload: Any,
        *,
        request_id: str,
        outcome_unknown: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise MCPClientError(
                error_layer="PROTOCOL",
                error_code="MCP_INVALID_JSONRPC",
                message="MCP server returned a non-object JSON-RPC payload.",
                retryable=False,
                outcome_unknown=outcome_unknown,
            )
        if payload.get("jsonrpc") != "2.0":
            raise MCPClientError(
                error_layer="PROTOCOL",
                error_code="MCP_INVALID_JSONRPC",
                message="MCP JSON-RPC version must be '2.0'.",
                retryable=False,
                outcome_unknown=outcome_unknown,
            )
        if payload.get("id") != request_id:
            raise MCPClientError(
                error_layer="PROTOCOL",
                error_code="MCP_INVALID_JSONRPC",
                message="MCP JSON-RPC response id does not match request id.",
                retryable=False,
                outcome_unknown=outcome_unknown,
            )
        has_result = "result" in payload
        has_error = "error" in payload
        if has_result == has_error:
            # Exactly one of result/error is required for a response.
            raise MCPClientError(
                error_layer="PROTOCOL",
                error_code="MCP_INVALID_JSONRPC",
                message="MCP JSON-RPC response must contain exactly one of result or error.",
                retryable=False,
                outcome_unknown=outcome_unknown,
            )
        if has_error and not isinstance(payload.get("error"), dict):
            raise MCPClientError(
                error_layer="PROTOCOL",
                error_code="MCP_INVALID_JSONRPC",
                message="MCP JSON-RPC error must be an object.",
                retryable=False,
                outcome_unknown=outcome_unknown,
            )
        return payload

    async def _post_rpc(
        self,
        *,
        endpoint: str,
        method: str,
        timeout_ms: int,
        params_extra: dict[str, Any] | None = None,
        mcp_name: str | None = None,
        auth_headers: Mapping[str, str] | None = None,
        request_id: str | None = None,
    ) -> Any:
        timeout = httpx.Timeout(timeout_ms / 1000.0)
        rpc_id = request_id or str(uuid.uuid4())
        headers = self._build_headers(method, mcp_name=mcp_name)
        if auth_headers:
            headers.update(dict(auth_headers))
        body = self._build_body(method, self._build_params(params_extra), rpc_id)
        client = await self._client()

        # Never log Authorization headers or request/response bodies.
        logger.info("MCP RPC request method=%s endpoint_host_only", method)

        request_sent = False
        try:
            response = await client.post(endpoint, headers=headers, json=body, timeout=timeout)
            request_sent = True
        except Exception as exc:
            raise self._map_transport_error(exc, request_sent=request_sent) from exc

        if response.status_code >= 400:
            raise self._map_http_error(response.status_code)

        try:
            payload = response.json()
        except ValueError as exc:
            raise MCPClientError(
                error_layer="PROTOCOL",
                error_code="MCP_INVALID_JSON",
                message="MCP server returned non-JSON response.",
                retryable=False,
            ) from exc

        envelope = self._validate_jsonrpc_envelope(payload, request_id=rpc_id)

        error = envelope.get("error")
        if error is not None:
            code = error.get("code") if isinstance(error, dict) else None
            err_message = (
                error.get("message")
                if isinstance(error, dict) and isinstance(error.get("message"), str)
                else "MCP JSON-RPC error."
            )
            if method == "server/discover" and code == _JSONRPC_METHOD_NOT_FOUND:
                raise DiscoverUnsupportedError(message=err_message)
            raise MCPClientError(
                error_layer="PROTOCOL",
                error_code="MCP_JSONRPC_ERROR",
                message=err_message,
                retryable=False,
            )

        return envelope.get("result")

    async def discover_capabilities(
        self,
        endpoint: str,
        timeout_ms: int = 10000,
    ) -> dict[str, Any]:
        """Call optional ``server/discover``. Raises DiscoverUnsupportedError if unsupported."""

        result = await self._post_rpc(
            endpoint=endpoint,
            method="server/discover",
            timeout_ms=timeout_ms,
        )
        if result is None:
            return {}
        if not isinstance(result, dict):
            raise MCPClientError(
                error_layer="PROTOCOL",
                error_code="MCP_INVALID_DISCOVER_RESULT",
                message="server/discover result must be an object.",
                retryable=False,
            )
        return result

    def _parse_tool(self, item: Any) -> RemoteToolDescriptor | None:
        if not isinstance(item, dict):
            return None
        name = item.get("name")
        if not isinstance(name, str) or not name:
            return None
        description = item.get("description")
        # Preserve remote schema wire values — do not coerce malformed schemas to None.
        if "inputSchema" in item:
            input_schema = item.get("inputSchema")
        elif "input_schema" in item:
            input_schema = item.get("input_schema")
        else:
            input_schema = None
        if "outputSchema" in item:
            output_schema = item.get("outputSchema")
        elif "output_schema" in item:
            output_schema = item.get("output_schema")
        else:
            output_schema = None
        annotations = item.get("annotations")
        return RemoteToolDescriptor(
            name=name,
            description=description if isinstance(description, str) else None,
            input_schema=input_schema,
            output_schema=output_schema,
            annotations=annotations if isinstance(annotations, dict) else None,
            raw=dict(item),
        )

    async def list_tools(
        self,
        endpoint: str,
        timeout_ms: int = 60000,
    ) -> list[RemoteToolDescriptor]:
        """Collect all tools via ``tools/list`` with cursor pagination."""

        tools: list[RemoteToolDescriptor] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()

        while True:
            extra: dict[str, Any] = {}
            if cursor is not None:
                extra["cursor"] = cursor

            result = await self._post_rpc(
                endpoint=endpoint,
                method="tools/list",
                timeout_ms=timeout_ms,
                params_extra=extra or None,
            )
            if result is None:
                break
            if not isinstance(result, dict):
                raise MCPClientError(
                    error_layer="PROTOCOL",
                    error_code="MCP_INVALID_TOOLS_LIST",
                    message="tools/list result must be an object.",
                    retryable=False,
                )

            raw_tools = result.get("tools") or []
            if not isinstance(raw_tools, list):
                raise MCPClientError(
                    error_layer="PROTOCOL",
                    error_code="MCP_INVALID_TOOLS_LIST",
                    message="tools/list.tools must be an array.",
                    retryable=False,
                )
            for item in raw_tools:
                parsed = self._parse_tool(item)
                if parsed is not None:
                    tools.append(parsed)

            next_cursor = result.get("nextCursor") or result.get("next_cursor")
            if not next_cursor:
                break
            if not isinstance(next_cursor, str):
                raise MCPClientError(
                    error_layer="PROTOCOL",
                    error_code="MCP_INVALID_TOOLS_LIST",
                    message="tools/list nextCursor must be a string.",
                    retryable=False,
                )
            if next_cursor in seen_cursors:
                raise MCPClientError(
                    error_layer="PROTOCOL",
                    error_code="MCP_TOOLS_LIST_CURSOR_LOOP",
                    message="tools/list pagination cursor repeated.",
                    retryable=False,
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        return tools

    async def call_tool(
        self,
        endpoint: str,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_ms: int,
        remote_request_id: str,
        auth_headers: Mapping[str, str] | None = None,
        max_result_bytes: int | None = None,
    ) -> tuple[NormalizedToolResult, dict[str, Any], datetime | None]:
        """Call ``tools/call``. Never logs Authorization headers, bodies, or requestState.

        ``remote_request_id`` is caller-supplied (``ToolCall.remote_request_id``)
        so the JSON-RPC id is stable across a Phase C finalizer's fencing check.
        The response body is streamed and fenced against ``max_result_bytes``;
        exceeding it raises :class:`MCPResultTooLargeError` before the full body
        is buffered.

        Returns ``(NormalizedToolResult, response_meta, first_byte_at)``.
        MRTR ``resultType=input_required`` is unsupported in this slice and
        raises ``MCP_INPUT_REQUIRED_UNSUPPORTED`` without inspecting/logging
        the opaque ``requestState``.
        """
        timeout = httpx.Timeout(timeout_ms / 1000.0)
        headers = self._build_headers("tools/call", mcp_name=tool_name)
        if auth_headers:
            headers.update(dict(auth_headers))
        body = self._build_body(
            "tools/call",
            self._build_params({"name": tool_name, "arguments": arguments}),
            remote_request_id,
        )
        client = await self._client()

        # Never log Authorization headers, request bodies, or tool arguments.
        logger.info(
            "MCP RPC request method=tools/call tool_name=%s endpoint_host_only",
            tool_name,
        )

        # ``request_sent`` tracks whether we observed response headers (definite
        # post-dispatch). Transport mapping also classifies Read*/Write* as
        # post-send even when headers never arrived — a ReadTimeout while
        # waiting for headers must not be treated as pre-send.
        request_sent = False
        first_byte_at: datetime | None = None
        raw_chunks = bytearray()
        status_code: int | None = None
        safe_response_headers: dict[str, str] = {}
        started_monotonic = time.monotonic()

        try:
            async with client.stream(
                "POST", endpoint, headers=headers, json=body, timeout=timeout
            ) as response:
                request_sent = True
                status_code = response.status_code
                safe_response_headers = redact_headers_for_meta(dict(response.headers))
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    if first_byte_at is None:
                        first_byte_at = datetime.now(UTC)
                    raw_chunks.extend(chunk)
                    if (
                        max_result_bytes is not None
                        and len(raw_chunks) > max_result_bytes
                    ):
                        raise MCPResultTooLargeError(max_result_bytes=max_result_bytes)
        except MCPResultTooLargeError:
            raise
        except Exception as exc:
            raise self._map_transport_error(exc, request_sent=request_sent) from exc

        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        response_meta: dict[str, Any] = {
            "http_status": status_code,
            "response_headers": safe_response_headers,
            "duration_ms": duration_ms,
        }

        if status_code is not None and status_code >= 400:
            raise self._map_http_error(status_code)

        # tools/call was sent and a body arrived — malformed JSON / JSON-RPC is
        # post-send ambiguity (external side effect may already have occurred).
        try:
            payload = json.loads(bytes(raw_chunks).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise MCPClientError(
                error_layer="PROTOCOL",
                error_code="MCP_INVALID_JSON",
                message="MCP server returned non-JSON response.",
                retryable=False,
                outcome_unknown=True,
            ) from exc

        envelope = self._validate_jsonrpc_envelope(
            payload, request_id=remote_request_id, outcome_unknown=True
        )

        error = envelope.get("error")
        if error is not None:
            err_message = (
                error.get("message")
                if isinstance(error, dict) and isinstance(error.get("message"), str)
                else "MCP JSON-RPC error."
            )
            raise MCPClientError(
                error_layer="PROTOCOL",
                error_code="MCP_JSONRPC_ERROR",
                message=err_message,
                retryable=False,
            )

        result = envelope.get("result")
        if not isinstance(result, dict):
            raise MCPClientError(
                error_layer="PROTOCOL",
                error_code="MCP_INVALID_TOOL_RESULT",
                message="tools/call result must be an object.",
                retryable=False,
            )

        result_type = result.get("resultType")
        if result_type == _INPUT_REQUIRED_RESULT_TYPE:
            # MRTR requestState is opaque — never log/store/interpret it here.
            raise MCPClientError(
                error_layer="PROTOCOL",
                error_code="MCP_INPUT_REQUIRED_UNSUPPORTED",
                message=(
                    "MCP tools/call requested runtime input; MRTR is unsupported"
                    " by MCP Tool Runner in this slice."
                ),
                retryable=False,
            )

        content = result.get("content")
        if content is None:
            content = []
        if not isinstance(content, list):
            raise MCPClientError(
                error_layer="PROTOCOL",
                error_code="MCP_INVALID_TOOL_RESULT",
                message="tools/call result.content must be an array.",
                retryable=False,
            )

        structured_content = result.get("structuredContent")
        if structured_content is not None and not isinstance(structured_content, (dict, list)):
            raise MCPClientError(
                error_layer="PROTOCOL",
                error_code="MCP_INVALID_TOOL_RESULT",
                message="tools/call result.structuredContent must be an object or array.",
                retryable=False,
            )

        metadata = result.get("_meta")
        task_handle = result.get("taskHandle")
        normalized = NormalizedToolResult(
            protocol_success=True,
            tool_error=bool(result.get("isError", False)),
            content=list(content),
            structured_content=structured_content,
            metadata=metadata if isinstance(metadata, dict) else {},
            task_handle=task_handle if isinstance(task_handle, str) else None,
            raw_size_bytes=len(raw_chunks),
            duration_ms=duration_ms,
            truncated=False,
            result_type=result_type if isinstance(result_type, str) else None,
        )
        return normalized, response_meta, first_byte_at
