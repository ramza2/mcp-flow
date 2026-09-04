"""Current MCP (2026-07-28) Streamable HTTP client — discovery and tools/list only."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx

from app.domain.enums import CURRENT_MCP_PROTOCOL_VERSION
from app.mcp.errors import DiscoverUnsupportedError, MCPClientError
from app.mcp.normalize import RemoteToolDescriptor

logger = logging.getLogger(__name__)

_CLIENT_INFO = {"name": "mcpflow", "version": "0.1.0"}
_JSONRPC_METHOD_NOT_FOUND = -32601


class CurrentMCPClient:
    """HTTP client for Current MCP wire format (no tools/call)."""

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

    def _build_headers(self, method: str) -> dict[str, str]:
        return {
            "MCP-Protocol-Version": CURRENT_MCP_PROTOCOL_VERSION,
            "Mcp-Method": method,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

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

    def _build_body(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params,
        }

    def _map_http_error(self, response: httpx.Response) -> MCPClientError:
        status = response.status_code
        if status in {401, 403}:
            return MCPClientError(
                error_layer="AUTH",
                error_code="MCP_AUTH_FAILED",
                message="MCP server rejected authentication.",
                retryable=False,
            )
        if 400 <= status < 500:
            return MCPClientError(
                error_layer="PROTOCOL",
                error_code="MCP_HTTP_CLIENT_ERROR",
                message=f"MCP server returned HTTP {status}.",
                retryable=False,
            )
        return MCPClientError(
            error_layer="NETWORK",
            error_code="MCP_HTTP_SERVER_ERROR",
            message=f"MCP server returned HTTP {status}.",
            retryable=True,
        )

    def _map_transport_error(self, exc: Exception) -> MCPClientError:
        if isinstance(exc, httpx.TimeoutException):
            return MCPClientError(
                error_layer="TIMEOUT",
                error_code="MCP_CONNECTION_TIMEOUT",
                message="MCP server connection timed out.",
                retryable=True,
            )
        if isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
            return MCPClientError(
                error_layer="NETWORK",
                error_code="MCP_NETWORK_ERROR",
                message="Failed to connect to MCP server.",
                retryable=True,
            )
        return MCPClientError(
            error_layer="PROTOCOL",
            error_code="MCP_PROTOCOL_ERROR",
            message="Unexpected MCP client transport failure.",
            retryable=False,
        )

    async def _post_rpc(
        self,
        *,
        endpoint: str,
        method: str,
        timeout_ms: int,
        params_extra: dict[str, Any] | None = None,
    ) -> Any:
        timeout = httpx.Timeout(timeout_ms / 1000.0)
        headers = self._build_headers(method)
        body = self._build_body(method, self._build_params(params_extra))
        client = await self._client()

        # Never log Authorization headers or request/response bodies.
        logger.info("MCP RPC request method=%s endpoint_host_only", method)

        try:
            response = await client.post(endpoint, headers=headers, json=body, timeout=timeout)
        except Exception as exc:
            raise self._map_transport_error(exc) from exc

        if response.status_code >= 400:
            raise self._map_http_error(response)

        try:
            payload = response.json()
        except ValueError as exc:
            raise MCPClientError(
                error_layer="PROTOCOL",
                error_code="MCP_INVALID_JSON",
                message="MCP server returned non-JSON response.",
                retryable=False,
            ) from exc

        if not isinstance(payload, dict):
            raise MCPClientError(
                error_layer="PROTOCOL",
                error_code="MCP_INVALID_JSONRPC",
                message="MCP server returned a non-object JSON-RPC payload.",
                retryable=False,
            )

        error = payload.get("error")
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

        return payload.get("result")

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
        input_schema = item.get("inputSchema", item.get("input_schema"))
        output_schema = item.get("outputSchema", item.get("output_schema"))
        annotations = item.get("annotations")
        return RemoteToolDescriptor(
            name=name,
            description=description if isinstance(description, str) else None,
            input_schema=input_schema if isinstance(input_schema, dict) else None,
            output_schema=output_schema if isinstance(output_schema, dict) else None,
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
