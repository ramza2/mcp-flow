"""MCP Server lifecycle service (docs/05–06)."""

from __future__ import annotations

import re
import uuid

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.url_validation import validate_mcp_endpoint_url
from app.domain.enums import (
    MCPAuthType,
    MCPProtocolEra,
    MCPServerStatus,
    MCPTransportType,
)
from app.models.mcp import MCPServer
from app.repositories.mcp_server import MCPServerRepository
from app.schemas.mcp_server import MCPServerCreate, MCPServerUpdate

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify_name(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.lower().strip()).strip("-")
    return (slug[:48] if slug else "server")


def _generate_server_code(name: str) -> str:
    return f"{_slugify_name(name)}-{uuid.uuid4().hex[:8]}"


def _protocol_era_for_transport(transport: MCPTransportType) -> MCPProtocolEra:
    if transport == MCPTransportType.LEGACY_HTTP_SSE:
        return MCPProtocolEra.LEGACY
    return MCPProtocolEra.CURRENT


class MCPServerService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._servers = MCPServerRepository(session)

    async def _require(self, server_id: uuid.UUID) -> MCPServer:
        server = await self._servers.get(server_id)
        if server is None:
            raise AppError(
                code="NOT_FOUND",
                message="MCP server not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return server

    def _validate_transport_auth(
        self,
        *,
        transport_type: MCPTransportType,
        endpoint_url: str | None,
        stdio_manifest_id: str | None,
        auth_type: MCPAuthType,
        auth_secret_id: uuid.UUID | None,
    ) -> str | None:
        """Validate transport/auth fields. Returns normalized endpoint_url when applicable."""

        if transport_type in {
            MCPTransportType.STREAMABLE_HTTP,
            MCPTransportType.LEGACY_HTTP_SSE,
        }:
            if stdio_manifest_id:
                raise AppError(
                    code="VALIDATION_ERROR",
                    message="stdio_manifest_id is not allowed for HTTP transports.",
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            normalized = validate_mcp_endpoint_url(endpoint_url or "")
        elif transport_type == MCPTransportType.STDIO:
            if endpoint_url:
                raise AppError(
                    code="VALIDATION_ERROR",
                    message="endpoint_url is not allowed for STDIO transport.",
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            if not stdio_manifest_id or not str(stdio_manifest_id).strip():
                raise AppError(
                    code="VALIDATION_ERROR",
                    message="stdio_manifest_id is required for STDIO transport.",
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            normalized = None
        else:
            raise AppError(
                code="VALIDATION_ERROR",
                message=f"Unsupported transport_type: {transport_type}.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        if auth_type != MCPAuthType.NONE and auth_secret_id is None:
            raise AppError(
                code="VALIDATION_ERROR",
                message="auth_secret_id is required when auth_type is not NONE.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        return normalized

    def _raise_version_conflict(self) -> None:
        raise AppError(
            code="RESOURCE_VERSION_CONFLICT",
            message="MCP server lock_version does not match.",
            status_code=status.HTTP_409_CONFLICT,
        )

    async def create(self, data: MCPServerCreate) -> MCPServer:
        endpoint = self._validate_transport_auth(
            transport_type=data.transport_type,
            endpoint_url=data.endpoint_url,
            stdio_manifest_id=data.stdio_manifest_id,
            auth_type=data.auth_type,
            auth_secret_id=data.auth_secret_id,
        )
        era = _protocol_era_for_transport(data.transport_type)
        code = _generate_server_code(data.name)
        # Extremely unlikely collision; retry once.
        if await self._servers.get_by_code(code) is not None:
            code = _generate_server_code(data.name)

        server = await self._servers.create(
            code=code,
            name=data.name,
            description=data.description,
            transport_type=str(data.transport_type),
            endpoint_url=endpoint,
            stdio_manifest_id=(
                data.stdio_manifest_id.strip() if data.stdio_manifest_id else None
            ),
            auth_type=str(data.auth_type),
            auth_secret_id=data.auth_secret_id,
            status=MCPServerStatus.DRAFT,
            protocol_era=str(era),
            connect_timeout_ms=data.connect_timeout_ms,
            call_timeout_ms=data.call_timeout_ms,
            max_concurrency=data.max_concurrency,
        )
        await self._session.commit()
        await self._session.refresh(server)
        return server

    async def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status_filter: str | None = None,
        transport_type: str | None = None,
        q: str | None = None,
        sort: str = "-updated_at",
    ) -> tuple[list[MCPServer], int]:
        return await self._servers.list(
            page=page,
            page_size=page_size,
            status=status_filter,
            transport_type=transport_type,
            q=q,
            sort=sort,
        )

    async def get(self, server_id: uuid.UUID) -> MCPServer:
        return await self._require(server_id)

    async def update(
        self,
        server_id: uuid.UUID,
        data: MCPServerUpdate,
        *,
        expected_lock_version: int,
    ) -> MCPServer:
        server = await self._require(server_id)
        if server.lock_version != expected_lock_version:
            self._raise_version_conflict()

        payload = data.model_dump(exclude_unset=True, exclude={"lock_version", "status"})
        transport = MCPTransportType(
            payload.get("transport_type", server.transport_type)
        )
        endpoint_url = payload.get("endpoint_url", server.endpoint_url)
        stdio_manifest_id = payload.get("stdio_manifest_id", server.stdio_manifest_id)
        auth_type = MCPAuthType(payload.get("auth_type", server.auth_type))
        auth_secret_id = payload.get("auth_secret_id", server.auth_secret_id)

        normalized = self._validate_transport_auth(
            transport_type=transport,
            endpoint_url=endpoint_url,
            stdio_manifest_id=stdio_manifest_id,
            auth_type=auth_type,
            auth_secret_id=auth_secret_id,
        )

        fields: dict = {}
        for key, value in payload.items():
            if key == "transport_type":
                fields[key] = str(value)
            elif key == "auth_type":
                fields[key] = str(value)
            elif key == "endpoint_url":
                fields[key] = normalized
            elif key == "stdio_manifest_id" and value is not None:
                fields[key] = str(value).strip() or None
            else:
                fields[key] = value

        if "transport_type" in fields:
            fields["protocol_era"] = str(_protocol_era_for_transport(transport))
            if transport == MCPTransportType.STDIO:
                fields["endpoint_url"] = None
            else:
                fields["stdio_manifest_id"] = None
                fields["endpoint_url"] = normalized

        try:
            updated = await self._servers.update(
                server,
                expected_lock_version=expected_lock_version,
                **fields,
            )
        except ValueError as exc:
            if str(exc) == "RESOURCE_VERSION_CONFLICT":
                self._raise_version_conflict()
            raise

        await self._session.commit()
        await self._session.refresh(updated)
        return updated

    async def activate(self, server_id: uuid.UUID) -> MCPServer:
        server = await self._require(server_id)
        if server.status not in {
            MCPServerStatus.DRAFT,
            MCPServerStatus.INACTIVE,
        }:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"Cannot activate server in status {server.status}; "
                    "expected DRAFT or INACTIVE."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )
        updated = await self._servers.update(
            server,
            status=MCPServerStatus.ACTIVE,
        )
        await self._session.commit()
        await self._session.refresh(updated)
        return updated

    async def deactivate(self, server_id: uuid.UUID) -> MCPServer:
        server = await self._require(server_id)
        if server.status != MCPServerStatus.ACTIVE:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"Cannot deactivate server in status {server.status}; "
                    "expected ACTIVE."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )
        updated = await self._servers.update(
            server,
            status=MCPServerStatus.INACTIVE,
        )
        await self._session.commit()
        await self._session.refresh(updated)
        return updated
