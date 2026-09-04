"""Deterministic MCP HTTP mock for API tests (httpx.MockTransport)."""

from __future__ import annotations

import json
from typing import Any

import httpx
from app.mcp.client import MCPHttpClient

_JSONRPC_METHOD_NOT_FOUND = -32601

_ECHO_TOOL: dict[str, Any] = {
    "name": "echo",
    "description": "Echo input back",
    "inputSchema": {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    },
}

_WEATHER_TOOL: dict[str, Any] = {
    "name": "lookup_weather",
    "description": "Lookup weather for a city",
    "inputSchema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
}

_ECHO_SCHEMA_V2: dict[str, Any] = {
    "name": "echo",
    "description": "Echo input back (v2)",
    "inputSchema": {
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "locale": {"type": "string"},
        },
        "required": ["message"],
    },
}


class TestMCPScenario:
    """Configurable MCP server mock scenarios for discovery / connection tests."""

    __test__ = False

    HEALTHY = "healthy"
    DISCOVER_UNSUPPORTED = "discover_unsupported"
    SCHEMA_CHANGE = "schema_change"
    TOOL_REMOVED = "tool_removed"
    TIMEOUT = "timeout"
    CONNECTION_FAILURE = "connection_failure"
    PROTOCOL_FAILURE = "protocol_failure"
    ZERO_TOOLS = "zero_tools"

    def __init__(self, scenario: str = HEALTHY) -> None:
        self.scenario = scenario
        self._discover_calls = 0
        self._tools_list_calls = 0

    def _jsonrpc_ok(self, request: httpx.Request, result: Any) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": result},
        )

    def _jsonrpc_error(
        self,
        request: httpx.Request,
        *,
        code: int,
        message: str,
    ) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {"code": code, "message": message},
            },
        )

    def _current_tools(self) -> list[dict[str, Any]]:
        if self.scenario == self.ZERO_TOOLS:
            return []

        if self.scenario == self.SCHEMA_CHANGE and self._tools_list_calls > 1:
            return [_ECHO_SCHEMA_V2, _WEATHER_TOOL]

        if self.scenario == self.TOOL_REMOVED and self._tools_list_calls > 1:
            return [_ECHO_TOOL]

        healthy_scenarios = {
            self.HEALTHY,
            self.DISCOVER_UNSUPPORTED,
            self.SCHEMA_CHANGE,
            self.TOOL_REMOVED,
        }
        if self.scenario in healthy_scenarios:
            return [_ECHO_TOOL, _WEATHER_TOOL]

        return [_ECHO_TOOL, _WEATHER_TOOL]

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.scenario == self.CONNECTION_FAILURE:
            raise httpx.ConnectError("connection refused", request=request)

        if self.scenario == self.TIMEOUT:
            raise httpx.ReadTimeout("timed out", request=request)

        body = json.loads(request.content.decode("utf-8"))
        method = body.get("method")

        if method == "server/discover":
            self._discover_calls += 1
            if self.scenario == self.DISCOVER_UNSUPPORTED:
                return self._jsonrpc_error(
                    request,
                    code=_JSONRPC_METHOD_NOT_FOUND,
                    message="Method not found",
                )
            return self._jsonrpc_ok(request, {"tools": {"listChanged": True}})

        if method == "tools/list":
            self._tools_list_calls += 1
            if self.scenario == self.PROTOCOL_FAILURE:
                return httpx.Response(200, text="not-json")
            tools = self._current_tools()
            return self._jsonrpc_ok(request, {"tools": tools})

        return self._jsonrpc_error(request, code=-32601, message=f"Unknown method: {method}")

    def build_transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def build_http_client(self) -> MCPHttpClient:
        http = httpx.AsyncClient(transport=self.build_transport())
        return MCPHttpClient(http=http)
