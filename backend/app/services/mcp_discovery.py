"""MCP connection-test and discovery services (docs/05–06)."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import (
    CURRENT_MCP_PROTOCOL_VERSION,
    MCPAuthType,
    MCPCheckStatus,
    MCPCheckType,
    MCPDiscoveryMode,
    MCPProtocolEra,
    MCPToolStatus,
    MCPTransportType,
)
from app.mcp.client import MCPHttpClient
from app.mcp.errors import DiscoverUnsupportedError, MCPClientError
from app.mcp.normalize import RemoteToolDescriptor, content_hash, validate_tool_schemas
from app.models.mcp import MCPServer, MCPServerCheck, MCPServerDiscovery, MCPTool
from app.repositories.mcp_discovery import MCPDiscoveryRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.schemas.mcp_server import (
    ConnectionTestResponse,
    DiscoveryDiffSummary,
    DiscoveryResponse,
)


@dataclass(slots=True)
class ServerConfig:
    """Plain snapshot for HTTP work outside of long-lived ORM usage."""

    id: uuid.UUID
    transport_type: str
    protocol_era: str
    endpoint_url: str | None
    auth_type: str
    auth_secret_id: uuid.UUID | None
    connect_timeout_ms: int
    call_timeout_ms: int
    discovery_mode: str | None


def _server_config(server: MCPServer) -> ServerConfig:
    return ServerConfig(
        id=server.id,
        transport_type=server.transport_type,
        protocol_era=server.protocol_era,
        endpoint_url=server.endpoint_url,
        auth_type=server.auth_type,
        auth_secret_id=server.auth_secret_id,
        connect_timeout_ms=server.connect_timeout_ms,
        call_timeout_ms=server.call_timeout_ms,
        discovery_mode=server.discovery_mode,
    )


def _reject_unsupported_transport(config: ServerConfig) -> None:
    if config.transport_type == MCPTransportType.STDIO:
        raise AppError(
            code="MCP_STDIO_UNSUPPORTED",
            message=(
                "STDIO MCP servers are executed only by mcp-worker; "
                "HTTP connection-test and discovery are not supported on the API."
            ),
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
        )
    if config.transport_type == MCPTransportType.LEGACY_HTTP_SSE:
        raise AppError(
            code="MCP_LEGACY_UNSUPPORTED",
            message="Legacy HTTP+SSE MCP transport is not supported by this API.",
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
        )


def _discovery_to_response(discovery: MCPServerDiscovery) -> DiscoveryResponse:
    raw = discovery.raw_response if isinstance(discovery.raw_response, dict) else {}
    diff_raw = raw.get("diff") if isinstance(raw.get("diff"), dict) else {}
    return DiscoveryResponse(
        id=discovery.id,
        mcp_server_id=discovery.mcp_server_id,
        protocol_era=MCPProtocolEra(discovery.protocol_era),
        discovery_mode=(
            MCPDiscoveryMode(discovery.discovery_mode)
            if discovery.discovery_mode
            else None
        ),
        selected_version=discovery.selected_version,
        success=discovery.success,
        error_code=discovery.error_code,
        error_message=discovery.error_message,
        apply_changes=bool(raw.get("apply_changes", False)),
        diff=DiscoveryDiffSummary(
            added=int(diff_raw.get("added", 0) or 0),
            changed=int(diff_raw.get("changed", 0) or 0),
            missing=int(diff_raw.get("missing", 0) or 0),
            unchanged=int(diff_raw.get("unchanged", 0) or 0),
        ),
        started_at=discovery.started_at,
        finished_at=discovery.finished_at,
        capabilities=discovery.capabilities,
    )


def _check_to_response(
    check: MCPServerCheck,
    *,
    discovery_mode: str | None,
) -> ConnectionTestResponse:
    return ConnectionTestResponse(
        id=check.id,
        mcp_server_id=check.mcp_server_id,
        check_type=MCPCheckType(check.check_type),
        status=MCPCheckStatus(check.status),
        latency_ms=check.latency_ms,
        protocol_version=check.protocol_version,
        discovery_mode=MCPDiscoveryMode(discovery_mode) if discovery_mode else None,
        error_layer=check.error_layer,
        error_code=check.error_code,
        error_message=check.error_message,
        checked_at=check.checked_at,
    )


class MCPDiscoveryService:
    def __init__(
        self,
        session: AsyncSession,
        http_client: MCPHttpClient,
    ) -> None:
        self._session = session
        self._http = http_client
        self._servers = MCPServerRepository(session)
        self._discoveries = MCPDiscoveryRepository(session)
        self._tools = MCPToolRepository(session)

    async def _require_server(self, server_id: uuid.UUID) -> MCPServer:
        server = await self._servers.get(server_id)
        if server is None:
            raise AppError(
                code="NOT_FOUND",
                message="MCP server not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return server

    async def connection_test(self, server_id: uuid.UUID) -> ConnectionTestResponse:
        server = await self._require_server(server_id)
        config = _server_config(server)
        _reject_unsupported_transport(config)

        if config.auth_type != MCPAuthType.NONE and config.auth_secret_id is None:
            check = await self._discoveries.create_check(
                mcp_server_id=config.id,
                check_type=MCPCheckType.MANUAL,
                status=MCPCheckStatus.FAILED,
                error_layer="AUTH",
                error_code="MCP_AUTH_SECRET_UNAVAILABLE",
                error_message="auth_secret_id is required for authenticated MCP servers.",
            )
            server.last_error_at = datetime.now(UTC)
            await self._session.flush()
            await self._session.commit()
            await self._session.refresh(check)
            return _check_to_response(check, discovery_mode=server.discovery_mode)

        if not config.endpoint_url:
            check = await self._discoveries.create_check(
                mcp_server_id=config.id,
                check_type=MCPCheckType.MANUAL,
                status=MCPCheckStatus.FAILED,
                error_layer="VALIDATION",
                error_code="VALIDATION_ERROR",
                error_message="endpoint_url is required for HTTP connection tests.",
            )
            server.last_error_at = datetime.now(UTC)
            await self._session.flush()
            await self._session.commit()
            await self._session.refresh(check)
            return _check_to_response(check, discovery_mode=server.discovery_mode)

        started = time.perf_counter()
        discovery_mode: str | None = None
        protocol_version: str | None = None
        try:
            try:
                await self._http.discover_capabilities(
                    config.endpoint_url,
                    timeout_ms=config.connect_timeout_ms,
                    protocol_era=config.protocol_era,
                )
                discovery_mode = MCPDiscoveryMode.EXPLICIT_DISCOVERY
            except DiscoverUnsupportedError:
                discovery_mode = MCPDiscoveryMode.INFERRED_CURRENT

            await self._http.list_tools(
                config.endpoint_url,
                timeout_ms=config.call_timeout_ms,
                protocol_era=config.protocol_era,
            )
            protocol_version = CURRENT_MCP_PROTOCOL_VERSION
            latency_ms = int((time.perf_counter() - started) * 1000)

            check = await self._discoveries.create_check(
                mcp_server_id=config.id,
                check_type=MCPCheckType.MANUAL,
                status=MCPCheckStatus.SUCCEEDED,
                latency_ms=latency_ms,
                protocol_version=protocol_version,
            )
            server.last_healthy_at = datetime.now(UTC)
            server.negotiated_protocol_version = protocol_version
            server.discovery_mode = str(discovery_mode)
            await self._session.flush()
            await self._session.commit()
            await self._session.refresh(check)
            return _check_to_response(check, discovery_mode=str(discovery_mode))

        except MCPClientError as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            check_status = (
                MCPCheckStatus.TIMED_OUT
                if exc.error_layer == "TIMEOUT"
                else MCPCheckStatus.FAILED
            )
            check = await self._discoveries.create_check(
                mcp_server_id=config.id,
                check_type=MCPCheckType.MANUAL,
                status=check_status,
                latency_ms=latency_ms,
                error_layer=exc.error_layer,
                error_code=exc.error_code,
                error_message=exc.message,
            )
            # Do not set server.status=ERROR automatically.
            server.last_error_at = datetime.now(UTC)
            await self._session.flush()
            await self._session.commit()
            await self._session.refresh(check)
            return _check_to_response(check, discovery_mode=server.discovery_mode)

    async def discover(
        self,
        server_id: uuid.UUID,
        *,
        mode: str = "FULL",
        apply_changes: bool = False,
    ) -> DiscoveryResponse:
        server = await self._require_server(server_id)
        config = _server_config(server)
        _reject_unsupported_transport(config)

        if config.auth_type != MCPAuthType.NONE and config.auth_secret_id is None:
            raise AppError(
                code="MCP_AUTH_SECRET_UNAVAILABLE",
                message="auth_secret_id is required for authenticated MCP servers.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        if not config.endpoint_url:
            raise AppError(
                code="VALIDATION_ERROR",
                message="endpoint_url is required for HTTP discovery.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        discovery = await self._discoveries.create_discovery(
            mcp_server_id=config.id,
            protocol_era=config.protocol_era,
            adapter_name="CurrentMCPClient",
            adapter_version="0.1.0",
            success=False,
            requested_versions=[CURRENT_MCP_PROTOCOL_VERSION],
        )
        await self._session.flush()

        capabilities: dict[str, Any] | None = None
        discovery_mode = MCPDiscoveryMode.INFERRED_CURRENT
        remote_tools: list[RemoteToolDescriptor] = []

        try:
            try:
                capabilities = await self._http.discover_capabilities(
                    config.endpoint_url,
                    timeout_ms=config.connect_timeout_ms,
                    protocol_era=config.protocol_era,
                )
                discovery_mode = MCPDiscoveryMode.EXPLICIT_DISCOVERY
            except DiscoverUnsupportedError:
                discovery_mode = MCPDiscoveryMode.INFERRED_CURRENT
                capabilities = None

            remote_tools = await self._http.list_tools(
                config.endpoint_url,
                timeout_ms=config.call_timeout_ms,
                protocol_era=config.protocol_era,
            )
        except MCPClientError as exc:
            finished = datetime.now(UTC)
            discovery.success = False
            discovery.error_code = (
                "MCP_CONNECTION_TIMEOUT"
                if exc.error_layer == "TIMEOUT"
                else exc.error_code
            )
            discovery.error_message = exc.message
            discovery.finished_at = finished
            discovery.discovery_mode = str(discovery_mode)
            discovery.raw_response = {
                "apply_changes": apply_changes,
                "mode": mode,
                "diff": {"added": 0, "changed": 0, "missing": 0, "unchanged": 0},
                "tool_summaries": [],
            }
            server.last_error_at = finished
            await self._session.flush()
            await self._session.commit()
            await self._session.refresh(discovery)
            return _discovery_to_response(discovery)

        # Build descriptor digests outside mutation path.
        remote_by_name: dict[str, RemoteToolDescriptor] = {}
        remote_meta: dict[str, dict[str, Any]] = {}
        for desc in remote_tools:
            digest = content_hash(desc)
            validation_status, validation_errors = validate_tool_schemas(
                desc.input_schema,
                desc.output_schema,
            )
            remote_by_name[desc.name] = desc
            remote_meta[desc.name] = {
                "content_hash": digest,
                "validation_status": str(validation_status),
                "validation_errors": validation_errors or None,
            }

        existing_tools, _ = await self._tools.list_tools(
            mcp_server_id=config.id,
            page=1,
            page_size=10_000,
        )
        existing_by_name = {tool.remote_name: tool for tool in existing_tools}

        added: list[str] = []
        changed: list[str] = []
        unchanged: list[str] = []
        missing: list[str] = []

        for name in remote_by_name:
            tool = existing_by_name.get(name)
            if tool is None:
                added.append(name)
                continue
            current_hash: str | None = None
            if tool.current_version_id is not None:
                version = await self._tools.get_version(tool.current_version_id)
                if version is not None:
                    current_hash = version.content_hash
            if current_hash == remote_meta[name]["content_hash"]:
                unchanged.append(name)
            else:
                changed.append(name)

        for name, tool in existing_by_name.items():
            if name not in remote_by_name:
                missing.append(name)

        diff_summary = {
            "added": len(added),
            "changed": len(changed),
            "missing": len(missing),
            "unchanged": len(unchanged),
        }
        tool_summaries = [
            {
                "remote_name": name,
                "change": change,
                "content_hash": remote_meta.get(name, {}).get("content_hash"),
                "validation_status": remote_meta.get(name, {}).get("validation_status"),
            }
            for change, names in (
                ("ADDED", added),
                ("CHANGED", changed),
                ("UNCHANGED", unchanged),
                ("MISSING", missing),
            )
            for name in names
        ]

        if apply_changes:
            await self._apply_tool_diff(
                server_id=config.id,
                remote_by_name=remote_by_name,
                remote_meta=remote_meta,
                existing_by_name=existing_by_name,
                added=added,
                changed=changed,
                unchanged=unchanged,
                missing=missing,
            )

        negotiated = CURRENT_MCP_PROTOCOL_VERSION
        finished = datetime.now(UTC)
        discovery.success = True
        discovery.discovery_mode = str(discovery_mode)
        discovery.selected_version = negotiated
        discovery.capabilities = capabilities
        discovery.finished_at = finished
        discovery.error_code = None
        discovery.error_message = None
        discovery.raw_response = {
            "apply_changes": apply_changes,
            "mode": mode,
            "diff": diff_summary,
            "tool_summaries": tool_summaries,
        }

        server.discovery_mode = str(discovery_mode)
        server.capabilities = capabilities
        server.negotiated_protocol_version = negotiated
        server.last_healthy_at = finished
        await self._session.flush()
        await self._session.commit()
        await self._session.refresh(discovery)
        return _discovery_to_response(discovery)

    async def _apply_tool_diff(
        self,
        *,
        server_id: uuid.UUID,
        remote_by_name: dict[str, RemoteToolDescriptor],
        remote_meta: dict[str, dict[str, Any]],
        existing_by_name: dict[str, MCPTool],
        added: list[str],
        changed: list[str],
        unchanged: list[str],
        missing: list[str],
    ) -> None:
        for name in added:
            desc = remote_by_name[name]
            meta = remote_meta[name]
            tool = await self._tools.create_tool(
                mcp_server_id=server_id,
                remote_name=name,
                status=MCPToolStatus.DISCOVERED,
            )
            version_no = await self._tools.next_version_no(tool.id)
            version = await self._tools.create_version(
                mcp_tool_id=tool.id,
                version_no=version_no,
                content_hash=meta["content_hash"],
                validation_status=meta["validation_status"],
                remote_description=desc.description,
                input_schema=desc.input_schema,
                output_schema=desc.output_schema,
                annotations=desc.annotations,
                raw_descriptor=desc.raw,
                validation_errors=meta["validation_errors"],
            )
            tool.current_version_id = version.id
            await self._session.flush()

        for name in changed:
            desc = remote_by_name[name]
            meta = remote_meta[name]
            tool = existing_by_name[name]
            version_no = await self._tools.next_version_no(tool.id)
            version = await self._tools.create_version(
                mcp_tool_id=tool.id,
                version_no=version_no,
                content_hash=meta["content_hash"],
                validation_status=meta["validation_status"],
                remote_description=desc.description,
                input_schema=desc.input_schema,
                output_schema=desc.output_schema,
                annotations=desc.annotations,
                raw_descriptor=desc.raw,
                validation_errors=meta["validation_errors"],
            )
            tool.current_version_id = version.id
            if tool.status == MCPToolStatus.MISSING:
                tool.status = MCPToolStatus.DISCOVERED
            await self._tools.touch_last_seen(tool)

        for name in unchanged:
            tool = existing_by_name[name]
            if tool.status == MCPToolStatus.MISSING:
                tool.status = MCPToolStatus.DISCOVERED
            await self._tools.touch_last_seen(tool)

        for name in missing:
            tool = existing_by_name[name]
            tool.status = MCPToolStatus.MISSING
            await self._session.flush()

    async def list_discoveries(
        self,
        server_id: uuid.UUID,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[DiscoveryResponse], int]:
        await self._require_server(server_id)
        rows, total = await self._discoveries.list_discoveries(
            mcp_server_id=server_id,
            page=page,
            page_size=page_size,
        )
        return [_discovery_to_response(row) for row in rows], total

    async def list_server_tools(
        self,
        server_id: uuid.UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        status_filter: str | None = None,
        q: str | None = None,
        sort: str = "-updated_at",
    ) -> tuple[list[MCPTool], int]:
        await self._require_server(server_id)
        return await self._tools.list_tools(
            page=page,
            page_size=page_size,
            mcp_server_id=server_id,
            status=status_filter,
            q=q,
            sort=sort,
        )
