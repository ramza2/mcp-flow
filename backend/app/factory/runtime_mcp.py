"""Current MCP JSON-RPC surface for Factory RuntimeBinding (tools/list|call)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import status

from app.domain.enums import CURRENT_MCP_PROTOCOL_VERSION, FactoryRuntimeBindingStatus
from app.factory.errors import (
    FACTORY_RUNTIME_HTTP_FAILED,
    FACTORY_RUNTIME_INPUT_INVALID,
    FACTORY_RUNTIME_NOT_READY,
    FACTORY_RUNTIME_TOOL_UNKNOWN,
)
from app.factory.mcp_output_schema import factory_mcp_output_schema
from app.factory.runtime_http_adapter import (
    DeclarativeToolContract,
    FactoryRuntimeAdapterError,
    execute_declarative_http,
)
from app.factory.runtime_limits import DEFAULT_MAX_TEXT_PREVIEW_CHARS

logger = logging.getLogger(__name__)

_JSONRPC_METHOD_NOT_FOUND = -32601
_JSONRPC_INVALID_PARAMS = -32602
_JSONRPC_INTERNAL = -32603


def mcp_endpoint_path(binding_id: uuid.UUID) -> str:
    return f"/factory-runtime/{binding_id}/mcp"


def build_mcp_endpoint_url(*, public_base_url: str, binding_id: uuid.UUID) -> str:
    base = public_base_url.rstrip("/")
    return f"{base}{mcp_endpoint_path(binding_id)}"


def _jsonrpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(
    request_id: Any, *, code: int, message: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": err}


class FactoryRuntimeMcpHandler:
    """Serve Current MCP methods for one loaded READY binding."""

    def __init__(
        self,
        *,
        binding_id: uuid.UUID,
        status_value: str,
        base_url: str | None,
        contracts_by_name: dict[str, DeclarativeToolContract],
        auth_headers: dict[str, str] | None,
        connect_timeout_ms: int,
        call_timeout_ms: int,
        max_response_bytes: int,
        hostname_allowlist: list[str] | None = None,
        http_client: Any | None = None,
    ) -> None:
        self._binding_id = binding_id
        self._status = status_value
        self._base_url = base_url
        self._contracts = contracts_by_name
        self._auth_headers = auth_headers
        self._connect_timeout_ms = connect_timeout_ms
        self._call_timeout_ms = call_timeout_ms
        self._max_response_bytes = max_response_bytes
        self._hostname_allowlist = hostname_allowlist
        self._http_client = http_client

    def ensure_ready(self) -> None:
        if self._status != FactoryRuntimeBindingStatus.READY.value:
            raise FactoryRuntimeAdapterError(
                FACTORY_RUNTIME_NOT_READY,
                "Factory runtime binding is not READY.",
            )
        if not self._base_url:
            raise FactoryRuntimeAdapterError(
                FACTORY_RUNTIME_NOT_READY,
                "Factory runtime binding has no base_url.",
            )

    async def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = payload.get("id")
        if payload.get("jsonrpc") != "2.0":
            return _jsonrpc_error(
                request_id, code=_JSONRPC_INVALID_PARAMS, message="jsonrpc must be 2.0"
            )
        method = payload.get("method")
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        try:
            self.ensure_ready()
        except FactoryRuntimeAdapterError as exc:
            return _jsonrpc_error(
                request_id,
                code=_JSONRPC_INTERNAL,
                message=exc.message,
                data={"code": exc.code},
            )

        if method == "initialize":
            return self._initialize(request_id, params)
        if method == "server/discover":
            # Optional discover → Current client treats -32601 as INFERRED_CURRENT.
            return _jsonrpc_error(
                request_id,
                code=_JSONRPC_METHOD_NOT_FOUND,
                message=f"Method not found: {method}",
            )
        if method == "tools/list":
            return await self._tools_list(request_id, params)
        if method == "tools/call":
            return await self._tools_call(request_id, params)
        return _jsonrpc_error(
            request_id,
            code=_JSONRPC_METHOD_NOT_FOUND,
            message=f"Method not found: {method}",
        )

    def _initialize(
        self, request_id: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Minimal Current MCP initialize / capability negotiation."""

        requested = None
        if isinstance(params, dict):
            requested = params.get("protocolVersion") or params.get("protocol_version")
        negotiated = CURRENT_MCP_PROTOCOL_VERSION
        if isinstance(requested, str) and requested.strip():
            # Accept only the repository Current MCP reference version.
            if requested.strip() != CURRENT_MCP_PROTOCOL_VERSION:
                return _jsonrpc_error(
                    request_id,
                    code=_JSONRPC_INVALID_PARAMS,
                    message=(
                        "Unsupported protocolVersion; "
                        f"expected {CURRENT_MCP_PROTOCOL_VERSION}."
                    ),
                )
        return _jsonrpc_result(
            request_id,
            {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": "mcpflow-factory-runtime",
                    "version": "1.0.0",
                },
            },
        )

    async def _tools_list(
        self, request_id: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        del params  # no cursor paging for bounded reviewed sets
        tools = []
        for name in sorted(self._contracts):
            contract = self._contracts[name]
            item: dict[str, Any] = {
                "name": contract.remote_name,
                "inputSchema": contract.input_schema,
            }
            if contract.description is not None:
                item["description"] = contract.description
            if contract.output_schema is not None:
                item["outputSchema"] = factory_mcp_output_schema(contract.output_schema)
            tools.append(item)
        logger.info(
            "factory_runtime_tools_list binding_id=%s count=%s",
            self._binding_id,
            len(tools),
        )
        return _jsonrpc_result(request_id, {"tools": tools})

    async def _tools_call(
        self, request_id: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments")
        if not isinstance(name, str) or not name:
            return _jsonrpc_error(
                request_id,
                code=_JSONRPC_INVALID_PARAMS,
                message="tools/call requires name.",
            )
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return _jsonrpc_error(
                request_id,
                code=_JSONRPC_INVALID_PARAMS,
                message="tools/call arguments must be an object.",
            )
        contract = self._contracts.get(name)
        if contract is None:
            return _jsonrpc_error(
                request_id,
                code=_JSONRPC_INVALID_PARAMS,
                message="Unknown tool.",
                data={"code": FACTORY_RUNTIME_TOOL_UNKNOWN},
            )
        assert self._base_url is not None
        try:
            result = await execute_declarative_http(
                base_url=self._base_url,
                contract=contract,
                arguments=arguments,
                auth_headers=self._auth_headers,
                connect_timeout_ms=self._connect_timeout_ms,
                call_timeout_ms=self._call_timeout_ms,
                max_response_bytes=self._max_response_bytes,
                hostname_allowlist=self._hostname_allowlist,
                http_client=self._http_client,
            )
        except FactoryRuntimeAdapterError as exc:
            logger.info(
                "factory_runtime_tools_call binding_id=%s tool=%s error=%s",
                self._binding_id,
                name,
                exc.code,
            )
            code = (
                _JSONRPC_INVALID_PARAMS
                if exc.code == FACTORY_RUNTIME_INPUT_INVALID
                else _JSONRPC_INTERNAL
            )
            return _jsonrpc_error(
                request_id,
                code=code,
                message=exc.message,
                data={"code": exc.code},
            )

        structured: dict[str, Any] = {
            "ok": result.ok,
            "http_status": result.http_status,
            "latency_ms": result.latency_ms,
            "protocol_version": CURRENT_MCP_PROTOCOL_VERSION,
        }
        if result.body_json is not None:
            structured["body"] = result.body_json
        elif result.body_text is not None:
            structured["body_text"] = result.body_text[:DEFAULT_MAX_TEXT_PREVIEW_CHARS]
        if not result.ok:
            structured["error_category"] = result.error_category or FACTORY_RUNTIME_HTTP_FAILED
            structured["error_message"] = result.error_message

        content = [
            {
                "type": "text",
                "text": (
                    f"HTTP {result.http_status}"
                    if result.ok
                    else (result.error_message or "HTTP error")
                ),
            }
        ]
        logger.info(
            "factory_runtime_tools_call binding_id=%s tool=%s http_status=%s ok=%s",
            self._binding_id,
            name,
            result.http_status,
            result.ok,
        )
        return _jsonrpc_result(
            request_id,
            {
                "content": content,
                "structuredContent": structured,
                "isError": not result.ok,
            },
        )


def http_status_for_unavailable() -> int:
    return status.HTTP_404_NOT_FOUND
