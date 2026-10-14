"""MCP Server lifecycle service (docs/05–06)."""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.canonical_hash import compute_canonical_json_hash
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
# transport_config key: sha256 of connection-relevant fields verified by
# a SUCCEEDED Factory Connection Test (does not delete historical Checks).
_CONNECTION_TEST_VERIFIED_FP_KEY = "connection_test_verified_fingerprint"


def _slugify_name(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.lower().strip()).strip("-")
    return (slug[:48] if slug else "server")


def _generate_server_code(name: str) -> str:
    return f"{_slugify_name(name)}-{uuid.uuid4().hex[:8]}"


def _protocol_era_for_transport(transport: MCPTransportType) -> MCPProtocolEra:
    if transport == MCPTransportType.LEGACY_HTTP_SSE:
        return MCPProtocolEra.LEGACY
    return MCPProtocolEra.CURRENT


def is_connection_deferred(server: MCPServer) -> bool:
    """True when transport_config marks connection as still deferred."""

    cfg = server.transport_config
    return isinstance(cfg, dict) and cfg.get("connection_deferred") is True


def is_factory_imported(server: MCPServer) -> bool:
    """True for MCP Servers created by Tool Factory Publish/Import."""

    cfg = server.transport_config
    if not isinstance(cfg, dict):
        return False
    return (
        cfg.get("factory_publish") is True
        or cfg.get("source") == "TOOL_FACTORY_PUBLISH"
    )


def has_usable_endpoint(server: MCPServer) -> bool:
    return bool(server.endpoint_url and str(server.endpoint_url).strip())


def connection_deferred_effective(server: MCPServer) -> bool:
    """Deferred Factory DRAFT still blocked from Connection Test / Activate."""

    return is_connection_deferred(server) and not has_usable_endpoint(server)


def _transport_config_with_deferred_cleared(
    transport_config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Clear connection_deferred while preserving Factory lineage metadata."""

    cfg = dict(transport_config) if isinstance(transport_config, dict) else {}
    cfg["connection_deferred"] = False
    return cfg


def connection_config_fingerprint(
    *,
    transport_type: str,
    endpoint_url: str | None,
    auth_type: str,
    auth_secret_id: uuid.UUID | str | None,
    connect_timeout_ms: int,
    call_timeout_ms: int,
) -> str:
    """Deterministic fingerprint of connection-relevant MCP Server fields.

    Includes transport/endpoint/auth and timeouts used by Connection Test.
    Excludes non-connection metadata such as name/description.
    """

    endpoint = str(endpoint_url).strip() if endpoint_url else None
    if endpoint == "":
        endpoint = None
    return compute_canonical_json_hash(
        {
            "transport_type": str(transport_type),
            "endpoint_url": endpoint,
            "auth_type": str(auth_type),
            "auth_secret_id": str(auth_secret_id) if auth_secret_id else None,
            "connect_timeout_ms": int(connect_timeout_ms),
            "call_timeout_ms": int(call_timeout_ms),
        }
    )


def connection_config_fingerprint_for_server(server: MCPServer) -> str:
    return connection_config_fingerprint(
        transport_type=server.transport_type,
        endpoint_url=server.endpoint_url,
        auth_type=server.auth_type,
        auth_secret_id=server.auth_secret_id,
        connect_timeout_ms=int(server.connect_timeout_ms),
        call_timeout_ms=int(server.call_timeout_ms),
    )


def get_verified_connection_fingerprint(server: MCPServer) -> str | None:
    cfg = server.transport_config
    if not isinstance(cfg, dict):
        return None
    value = cfg.get(_CONNECTION_TEST_VERIFIED_FP_KEY)
    if isinstance(value, str) and len(value) == 64:
        return value
    return None


def transport_config_with_verified_fingerprint(
    transport_config: dict[str, Any] | None,
    *,
    fingerprint: str,
) -> dict[str, Any]:
    """Store verified Connection Test fingerprint; preserve lineage keys."""

    cfg = dict(transport_config) if isinstance(transport_config, dict) else {}
    cfg[_CONNECTION_TEST_VERIFIED_FP_KEY] = fingerprint
    return cfg


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
        allow_deferred_http: bool = False,
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
            if allow_deferred_http and not (endpoint_url and str(endpoint_url).strip()):
                # Factory Publish deferred-DRAFT only: no fabricated endpoint.
                normalized = None
            else:
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

    async def create_draft_uncommitted(
        self,
        data: MCPServerCreate,
        *,
        created_by: uuid.UUID | None = None,
    ) -> MCPServer:
        """Create a DRAFT MCP Server without committing (for composing callers)."""

        endpoint = self._validate_transport_auth(
            transport_type=data.transport_type,
            endpoint_url=data.endpoint_url,
            stdio_manifest_id=data.stdio_manifest_id,
            auth_type=data.auth_type,
            auth_secret_id=data.auth_secret_id,
            allow_deferred_http=False,
        )
        era = _protocol_era_for_transport(data.transport_type)
        code = _generate_server_code(data.name)
        # Extremely unlikely collision; retry once.
        if await self._servers.get_by_code(code) is not None:
            code = _generate_server_code(data.name)

        return await self._servers.create(
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
            created_by=created_by,
        )

    async def create_factory_deferred_draft_uncommitted(
        self,
        *,
        name: str,
        description: str | None,
        transport_config: dict[str, Any],
        created_by: uuid.UUID | None = None,
        code: str | None = None,
    ) -> MCPServer:
        """Canonical Factory Publish path: DRAFT HTTP server without endpoint.

        Regular create still requires endpoint_url. This path is only for
        connection_deferred=true DRAFT imports; Activate / Connection Test remain
        blocked until an endpoint is set later.
        """

        if not isinstance(transport_config, dict):
            raise AppError(
                code="VALIDATION_ERROR",
                message="transport_config must be an object for deferred DRAFT.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        if transport_config.get("connection_deferred") is not True:
            raise AppError(
                code="VALIDATION_ERROR",
                message=(
                    "connection_deferred=true is required for Factory "
                    "deferred DRAFT creation."
                ),
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        # Refuse fabricated endpoints on the deferred path.
        self._validate_transport_auth(
            transport_type=MCPTransportType.STREAMABLE_HTTP,
            endpoint_url=None,
            stdio_manifest_id=None,
            auth_type=MCPAuthType.NONE,
            auth_secret_id=None,
            allow_deferred_http=True,
        )
        era = _protocol_era_for_transport(MCPTransportType.STREAMABLE_HTTP)
        server_code = (code or "").strip() or _generate_server_code(name)
        if await self._servers.get_by_code(server_code) is not None:
            if code:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="MCP server code already exists.",
                    status_code=status.HTTP_409_CONFLICT,
                )
            server_code = _generate_server_code(name)

        return await self._servers.create(
            code=server_code,
            name=name,
            description=description,
            transport_type=str(MCPTransportType.STREAMABLE_HTTP),
            endpoint_url=None,
            stdio_manifest_id=None,
            transport_config=dict(transport_config),
            auth_type=str(MCPAuthType.NONE),
            auth_secret_id=None,
            status=MCPServerStatus.DRAFT,
            protocol_era=str(era),
            created_by=created_by,
        )

    async def create(self, data: MCPServerCreate) -> MCPServer:
        server = await self.create_draft_uncommitted(data)
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
        # Do not rely on Python lock_version pre-check — CAS happens in update_atomic.

        payload = data.model_dump(exclude_unset=True, exclude={"lock_version"})
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

        # Effective endpoint after this update (fields may omit endpoint_url).
        effective_endpoint = fields.get("endpoint_url", normalized)
        usable_after = bool(
            effective_endpoint and str(effective_endpoint).strip()
        )
        if usable_after and is_connection_deferred(server):
            # Valid connection configuration persisted → deferred no longer
            # effective. Preserve Factory lineage keys in transport_config.
            fields["transport_config"] = _transport_config_with_deferred_cleared(
                server.transport_config
            )

        updated = await self._servers.update_atomic(
            server_id,
            expected_lock_version=expected_lock_version,
            **fields,
        )
        if updated is None:
            current = await self._servers.get(server_id)
            if current is None:
                raise AppError(
                    code="NOT_FOUND",
                    message="MCP server not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            self._raise_version_conflict()

        await self._session.commit()
        await self._session.refresh(updated)
        return updated

    async def activate(self, server_id: uuid.UUID) -> MCPServer:
        from app.repositories.mcp_discovery import MCPDiscoveryRepository

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
        if connection_deferred_effective(server):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Cannot activate connection_deferred DRAFT MCP Server "
                    "without endpoint_url. Set a real endpoint, then Connection "
                    "Test, before Activate."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )
        if is_factory_imported(server):
            # Factory lifecycle: require configured endpoint + deferred cleared
            # + verified Connection Test fingerprint matching current connection
            # config + latest Check SUCCEEDED. Discovery is a later slice.
            if is_connection_deferred(server) or not has_usable_endpoint(server):
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        "Cannot activate Factory-imported MCP Server without a "
                        "configured non-deferred endpoint_url."
                    ),
                    status_code=status.HTTP_409_CONFLICT,
                )
            current_fp = connection_config_fingerprint_for_server(server)
            verified_fp = get_verified_connection_fingerprint(server)
            if verified_fp is None or verified_fp != current_fp:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        "Cannot activate Factory-imported MCP Server without a "
                        "SUCCEEDED Connection Test for the current connection "
                        "configuration."
                    ),
                    status_code=status.HTTP_409_CONFLICT,
                )
            latest = await MCPDiscoveryRepository(self._session).get_latest_check(
                server.id
            )
            if latest is None or latest.status != "SUCCEEDED":
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        "Cannot activate Factory-imported MCP Server without a "
                        "latest SUCCEEDED Connection Test."
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
