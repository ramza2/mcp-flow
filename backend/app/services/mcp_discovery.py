"""MCP connection-test and discovery services (docs/05–06)."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import status
from sqlalchemy.exc import IntegrityError
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
from app.factory.discovery_reconcile import (
    FactoryReconcilePlan,
    build_factory_reconcile_plan,
    extract_factory_lineage_annotations,
    factory_diff_as_generic_counts,
    has_immutable_factory_lineage,
    index_remote_tools_strict,
)
from app.mcp.client import MCPHttpClient
from app.mcp.errors import DiscoverUnsupportedError, MCPClientError
from app.mcp.normalize import (
    RemoteToolDescriptor,
    content_hash,
    schema_for_storage,
    validate_tool_schemas,
)
from app.mcp.secrets import SecretResolver, UnimplementedSecretResolver
from app.models.mcp import MCPServer, MCPServerCheck, MCPServerDiscovery, MCPTool
from app.repositories.mcp_discovery import MCPDiscoveryRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.schemas.mcp_server import (
    ConnectionTestResponse,
    DiscoveryDiffSummary,
    DiscoveryResponse,
    FactoryDiscoveryClassification,
    FactoryDiscoveryDiffSummary,
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


def _factory_diff_to_response(
    raw: dict[str, Any],
) -> FactoryDiscoveryDiffSummary | None:
    factory_raw = raw.get("factory_reconciliation")
    if not isinstance(factory_raw, dict):
        return None
    classifications_raw = factory_raw.get("classifications")
    classifications: list[FactoryDiscoveryClassification] = []
    if isinstance(classifications_raw, list):
        for item in classifications_raw:
            if not isinstance(item, dict):
                continue
            try:
                classifications.append(FactoryDiscoveryClassification.model_validate(item))
            except Exception:
                continue
    status = factory_raw.get("status") or "MISMATCHED"
    if status not in {"CLEAN", "MISMATCHED", "AMBIGUOUS"}:
        status = "MISMATCHED"
    return FactoryDiscoveryDiffSummary(
        status=status,  # type: ignore[arg-type]
        matched=int(factory_raw.get("matched", 0) or 0),
        missing_expected=int(factory_raw.get("missing_expected", 0) or 0),
        unexpected_remote=int(factory_raw.get("unexpected_remote", 0) or 0),
        contract_changed=int(factory_raw.get("contract_changed", 0) or 0),
        classifications=classifications,
        error_message=(
            str(factory_raw["error_message"])
            if factory_raw.get("error_message") is not None
            else None
        ),
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
        factory_diff=_factory_diff_to_response(raw),
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


def _build_remote_meta(
    remote_tools: list[RemoteToolDescriptor],
) -> tuple[dict[str, RemoteToolDescriptor], dict[str, dict[str, Any]]]:
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
    return remote_by_name, remote_meta


def _compute_diff(
    *,
    remote_by_name: dict[str, RemoteToolDescriptor],
    remote_meta: dict[str, dict[str, Any]],
    existing_by_name: dict[str, MCPTool],
    current_hashes: dict[str, str | None],
) -> tuple[list[str], list[str], list[str], list[str]]:
    added: list[str] = []
    changed: list[str] = []
    unchanged: list[str] = []
    missing: list[str] = []

    for name in remote_by_name:
        tool = existing_by_name.get(name)
        if tool is None:
            added.append(name)
            continue
        if current_hashes.get(name) == remote_meta[name]["content_hash"]:
            unchanged.append(name)
        else:
            changed.append(name)

    for name in existing_by_name:
        if name not in remote_by_name:
            missing.append(name)

    return added, changed, unchanged, missing


class MCPDiscoveryService:
    def __init__(
        self,
        session: AsyncSession,
        http_client: MCPHttpClient,
        *,
        secret_resolver: SecretResolver | None = None,
    ) -> None:
        self._session = session
        self._http = http_client
        self._secrets: SecretResolver = secret_resolver or UnimplementedSecretResolver()
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

    async def _credential_auth_blocked(self, config: ServerConfig) -> bool:
        """Fail-closed for any non-NONE auth until a real SecretResolver is available."""

        if config.auth_type == MCPAuthType.NONE:
            return False
        if config.auth_secret_id is None:
            return True
        resolved = await self._secrets.resolve(config.auth_secret_id)
        return resolved is None

    async def connection_test(self, server_id: uuid.UUID) -> ConnectionTestResponse:
        from app.services.mcp_server import connection_deferred_effective

        server = await self._require_server(server_id)
        if connection_deferred_effective(server):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Cannot Connection Test connection_deferred DRAFT MCP Server "
                    "without endpoint_url. Set a real endpoint first."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )
        config = _server_config(server)
        prior_discovery_mode = server.discovery_mode
        _reject_unsupported_transport(config)
        # End DB transaction before any remote HTTP work.
        await self._session.commit()

        if await self._credential_auth_blocked(config):
            check = await self._discoveries.create_check(
                mcp_server_id=config.id,
                check_type=MCPCheckType.MANUAL,
                status=MCPCheckStatus.FAILED,
                error_layer="AUTH",
                error_code="MCP_AUTH_SECRET_UNAVAILABLE",
                error_message=(
                    "Secret Store resolver is not available; "
                    "credential auth MCP servers cannot perform remote HTTP calls."
                ),
            )
            server = await self._require_server(config.id)
            server.last_error_at = datetime.now(UTC)
            await self._session.commit()
            await self._session.refresh(check)
            return _check_to_response(check, discovery_mode=prior_discovery_mode)

        if not config.endpoint_url:
            check = await self._discoveries.create_check(
                mcp_server_id=config.id,
                check_type=MCPCheckType.MANUAL,
                status=MCPCheckStatus.FAILED,
                error_layer="VALIDATION",
                error_code="VALIDATION_ERROR",
                error_message="endpoint_url is required for HTTP connection tests.",
            )
            server = await self._require_server(config.id)
            server.last_error_at = datetime.now(UTC)
            await self._session.commit()
            await self._session.refresh(check)
            return _check_to_response(check, discovery_mode=prior_discovery_mode)

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
            server = await self._require_server(config.id)
            # Do not auto-activate; only record health metadata.
            server.last_healthy_at = datetime.now(UTC)
            server.negotiated_protocol_version = protocol_version
            server.discovery_mode = str(discovery_mode)
            # Bind Factory success evidence to the tested connection config.
            from app.services.mcp_server import (
                connection_config_fingerprint,
                is_factory_imported,
                transport_config_with_verified_fingerprint,
            )

            if is_factory_imported(server):
                tested_fp = connection_config_fingerprint(
                    transport_type=config.transport_type,
                    endpoint_url=config.endpoint_url,
                    auth_type=config.auth_type,
                    auth_secret_id=config.auth_secret_id,
                    connect_timeout_ms=config.connect_timeout_ms,
                    call_timeout_ms=config.call_timeout_ms,
                )
                server.transport_config = transport_config_with_verified_fingerprint(
                    server.transport_config,
                    fingerprint=tested_fp,
                )
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
            server = await self._require_server(config.id)
            server.last_error_at = datetime.now(UTC)
            await self._session.commit()
            await self._session.refresh(check)
            return _check_to_response(check, discovery_mode=prior_discovery_mode)

    async def discover(
        self,
        server_id: uuid.UUID,
        *,
        mode: str = "FULL",
        apply_changes: bool = False,
    ) -> DiscoveryResponse:
        from app.services.mcp_server import (
            connection_config_fingerprint,
            connection_deferred_effective,
            get_verified_connection_fingerprint,
            is_factory_imported,
        )

        server = await self._require_server(server_id)
        factory_server = is_factory_imported(server)
        config = _server_config(server)
        _reject_unsupported_transport(config)

        if factory_server:
            if connection_deferred_effective(server):
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        "Cannot Discovery Factory connection_deferred DRAFT MCP "
                        "Server without endpoint_url. Set a real endpoint and "
                        "pass Connection Test first."
                    ),
                    status_code=status.HTTP_409_CONFLICT,
                )
            current_fp = connection_config_fingerprint(
                transport_type=config.transport_type,
                endpoint_url=config.endpoint_url,
                auth_type=config.auth_type,
                auth_secret_id=config.auth_secret_id,
                connect_timeout_ms=config.connect_timeout_ms,
                call_timeout_ms=config.call_timeout_ms,
            )
            verified_fp = get_verified_connection_fingerprint(server)
            if verified_fp is None or verified_fp != current_fp:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        "Cannot Discovery Factory-imported MCP Server without a "
                        "SUCCEEDED Connection Test for the current connection "
                        "configuration."
                    ),
                    status_code=status.HTTP_409_CONFLICT,
                )

        if await self._credential_auth_blocked(config):
            raise AppError(
                code="MCP_AUTH_SECRET_UNAVAILABLE",
                message=(
                    "Secret Store resolver is not available; "
                    "credential auth MCP servers cannot perform remote discovery."
                ),
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
        discovery_id = discovery.id
        # Commit stub discovery before remote HTTP — never hold a DB txn over network I/O.
        await self._session.commit()

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
            discovery = await self._discoveries.get_discovery(discovery_id)
            assert discovery is not None
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
            server = await self._require_server(config.id)
            server.last_error_at = finished
            if factory_server:
                from app.services.mcp_server import (
                    transport_config_clear_discovery_fingerprint,
                )

                server.transport_config = transport_config_clear_discovery_fingerprint(
                    server.transport_config
                )
            await self._session.commit()
            await self._session.refresh(discovery)
            return _discovery_to_response(discovery)

        if factory_server:
            return await self._discover_factory(
                config=config,
                discovery_id=discovery_id,
                remote_tools=remote_tools,
                capabilities=capabilities,
                discovery_mode=str(discovery_mode),
                mode=mode,
                apply_changes=apply_changes,
            )

        # Fingerprint remote descriptors outside DB mutation.
        remote_by_name, remote_meta = _build_remote_meta(remote_tools)

        if apply_changes:
            # Serialize apply for this server only — re-read tools under row lock.
            locked = await self._servers.lock_for_update(config.id)
            if locked is None:
                raise AppError(
                    code="NOT_FOUND",
                    message="MCP server not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            existing_tools, _ = await self._tools.list_tools(
                mcp_server_id=config.id,
                page=1,
                page_size=10_000,
            )
            existing_by_name = {tool.remote_name: tool for tool in existing_tools}
            current_hashes: dict[str, str | None] = {}
            for name, tool in existing_by_name.items():
                current_hash: str | None = None
                if tool.current_version_id is not None:
                    version = await self._tools.get_version(tool.current_version_id)
                    if version is not None:
                        current_hash = version.content_hash
                current_hashes[name] = current_hash

            added, changed, unchanged, missing = _compute_diff(
                remote_by_name=remote_by_name,
                remote_meta=remote_meta,
                existing_by_name=existing_by_name,
                current_hashes=current_hashes,
            )
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
            server = locked
        else:
            existing_tools, _ = await self._tools.list_tools(
                mcp_server_id=config.id,
                page=1,
                page_size=10_000,
            )
            existing_by_name = {tool.remote_name: tool for tool in existing_tools}
            current_hashes = {}
            for name, tool in existing_by_name.items():
                current_hash = None
                if tool.current_version_id is not None:
                    version = await self._tools.get_version(tool.current_version_id)
                    if version is not None:
                        current_hash = version.content_hash
                current_hashes[name] = current_hash
            added, changed, unchanged, missing = _compute_diff(
                remote_by_name=remote_by_name,
                remote_meta=remote_meta,
                existing_by_name=existing_by_name,
                current_hashes=current_hashes,
            )
            server = await self._require_server(config.id)

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

        negotiated = CURRENT_MCP_PROTOCOL_VERSION
        finished = datetime.now(UTC)
        discovery = await self._discoveries.get_discovery(discovery_id)
        assert discovery is not None
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
        await self._session.commit()
        await self._session.refresh(discovery)
        return _discovery_to_response(discovery)

    async def _load_expected_factory_contracts(
        self, server_id: uuid.UUID
    ) -> tuple[dict[str, MCPTool], dict[str, dict[str, Any]], str | None]:
        """Load Factory lineage tools only; fail closed on pollution/duplicates."""

        existing_tools, _ = await self._tools.list_tools(
            mcp_server_id=server_id,
            page=1,
            page_size=10_000,
        )
        seen_names: set[str] = set()
        for tool in existing_tools:
            if tool.remote_name in seen_names:
                return (
                    {},
                    {},
                    f"Duplicate imported tool identity: {tool.remote_name!r}.",
                )
            seen_names.add(tool.remote_name)

        existing_by_name: dict[str, MCPTool] = {}
        expected: dict[str, dict[str, Any]] = {}
        unrelated: list[str] = []
        for tool in existing_tools:
            input_schema = None
            output_schema = None
            annotations = None
            raw_descriptor = None
            schema_dialect = None
            if tool.current_version_id is not None:
                version = await self._tools.get_version(tool.current_version_id)
                if version is not None:
                    input_schema = version.input_schema
                    output_schema = version.output_schema
                    annotations = version.annotations
                    raw_descriptor = version.raw_descriptor
                    schema_dialect = version.schema_dialect
            if not has_immutable_factory_lineage(
                annotations=annotations,
                raw_descriptor=raw_descriptor,
                schema_dialect=schema_dialect,
            ):
                unrelated.append(tool.remote_name)
                continue
            existing_by_name[tool.remote_name] = tool
            expected[tool.remote_name] = {
                "input_schema": input_schema,
                "output_schema": output_schema,
            }

        if unrelated:
            names = ", ".join(repr(n) for n in sorted(unrelated))
            return (
                {},
                {},
                (
                    "Factory Discovery expected set polluted by Tools without "
                    f"immutable Factory lineage: {names}. "
                    "Resolve unrelated local Tools before reconciliation."
                ),
            )
        return existing_by_name, expected, None

    async def _discover_factory(
        self,
        *,
        config: ServerConfig,
        discovery_id: uuid.UUID,
        remote_tools: list[RemoteToolDescriptor],
        capabilities: dict[str, Any] | None,
        discovery_mode: str,
        mode: str,
        apply_changes: bool,
    ) -> DiscoveryResponse:
        from app.services.mcp_server import connection_config_fingerprint

        tested_fp = connection_config_fingerprint(
            transport_type=config.transport_type,
            endpoint_url=config.endpoint_url,
            auth_type=config.auth_type,
            auth_secret_id=config.auth_secret_id,
            connect_timeout_ms=config.connect_timeout_ms,
            call_timeout_ms=config.call_timeout_ms,
        )

        remote_by_name, remote_dup_err = index_remote_tools_strict(remote_tools)
        if remote_dup_err is not None:
            plan = FactoryReconcilePlan(
                status="AMBIGUOUS",
                entries=[],
                error_message=remote_dup_err,
            )
            return await self._finalize_factory_discovery(
                config=config,
                discovery_id=discovery_id,
                capabilities=capabilities,
                discovery_mode=discovery_mode,
                mode=mode,
                apply_changes=apply_changes,
                plan=plan,
                tested_fp=tested_fp,
                apply_plan=False,
                remote_by_name={},
                remote_meta={},
                existing_by_name={},
            )

        remote_meta: dict[str, dict[str, Any]] = {}
        for name, desc in remote_by_name.items():
            digest = content_hash(desc)
            validation_status, validation_errors = validate_tool_schemas(
                desc.input_schema,
                desc.output_schema,
            )
            remote_meta[name] = {
                "content_hash": digest,
                "validation_status": str(validation_status),
                "validation_errors": validation_errors or None,
            }

        if apply_changes:
            locked = await self._servers.lock_for_update(config.id)
            if locked is None:
                raise AppError(
                    code="NOT_FOUND",
                    message="MCP server not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            drift_err = self._factory_apply_fingerprint_drift_error(
                locked=locked,
                tested_fp=tested_fp,
            )
            if drift_err is not None:
                plan = FactoryReconcilePlan(
                    status="MISMATCHED",
                    entries=[],
                    error_message=drift_err,
                )
                await self._finalize_factory_discovery(
                    config=config,
                    discovery_id=discovery_id,
                    capabilities=capabilities,
                    discovery_mode=discovery_mode,
                    mode=mode,
                    apply_changes=apply_changes,
                    plan=plan,
                    tested_fp=tested_fp,
                    apply_plan=False,
                    remote_by_name=remote_by_name,
                    remote_meta=remote_meta,
                    existing_by_name={},
                )
                raise AppError(
                    code="FACTORY_DISCOVERY_CONNECTION_DRIFT",
                    message=drift_err,
                    status_code=status.HTTP_409_CONFLICT,
                )
            existing_by_name, expected, local_dup_err = (
                await self._load_expected_factory_contracts(config.id)
            )
        else:
            existing_by_name, expected, local_dup_err = (
                await self._load_expected_factory_contracts(config.id)
            )

        if local_dup_err is not None:
            plan = FactoryReconcilePlan(
                status="AMBIGUOUS",
                entries=[],
                error_message=local_dup_err,
            )
            return await self._finalize_factory_discovery(
                config=config,
                discovery_id=discovery_id,
                capabilities=capabilities,
                discovery_mode=discovery_mode,
                mode=mode,
                apply_changes=apply_changes,
                plan=plan,
                tested_fp=tested_fp,
                apply_plan=False,
                remote_by_name=remote_by_name,
                remote_meta=remote_meta,
                existing_by_name=existing_by_name,
            )

        plan = build_factory_reconcile_plan(
            expected_by_name=expected,
            remote_by_name=remote_by_name,
        )

        apply_plan = bool(apply_changes and plan.is_clean)
        if apply_changes and not plan.is_clean:
            # Persist evidence + fail closed with zero Tool mutations.
            await self._finalize_factory_discovery(
                config=config,
                discovery_id=discovery_id,
                capabilities=capabilities,
                discovery_mode=discovery_mode,
                mode=mode,
                apply_changes=apply_changes,
                plan=plan,
                tested_fp=tested_fp,
                apply_plan=False,
                remote_by_name=remote_by_name,
                remote_meta=remote_meta,
                existing_by_name=existing_by_name,
            )
            raise AppError(
                code="FACTORY_DISCOVERY_RECONCILE_FAILED",
                message=(
                    plan.error_message
                    or (
                        "Factory Discovery reconciliation failed; "
                        "MISSING_EXPECTED / UNEXPECTED_REMOTE / "
                        "CONTRACT_CHANGED / ambiguity must be resolved "
                        "before apply_changes."
                    )
                ),
                status_code=status.HTTP_409_CONFLICT,
            )

        if apply_plan:
            # Re-check under the same row lock immediately before writes.
            locked = await self._servers.lock_for_update(config.id)
            if locked is None:
                raise AppError(
                    code="NOT_FOUND",
                    message="MCP server not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            drift_err = self._factory_apply_fingerprint_drift_error(
                locked=locked,
                tested_fp=tested_fp,
            )
            if drift_err is not None:
                plan = FactoryReconcilePlan(
                    status="MISMATCHED",
                    entries=[],
                    error_message=drift_err,
                )
                await self._finalize_factory_discovery(
                    config=config,
                    discovery_id=discovery_id,
                    capabilities=capabilities,
                    discovery_mode=discovery_mode,
                    mode=mode,
                    apply_changes=apply_changes,
                    plan=plan,
                    tested_fp=tested_fp,
                    apply_plan=False,
                    remote_by_name=remote_by_name,
                    remote_meta=remote_meta,
                    existing_by_name=existing_by_name,
                )
                raise AppError(
                    code="FACTORY_DISCOVERY_CONNECTION_DRIFT",
                    message=drift_err,
                    status_code=status.HTTP_409_CONFLICT,
                )
            await self._apply_factory_matched(
                server_id=config.id,
                remote_by_name=remote_by_name,
                remote_meta=remote_meta,
                existing_by_name=existing_by_name,
                matched_names=plan.matched_names(),
            )

        return await self._finalize_factory_discovery(
            config=config,
            discovery_id=discovery_id,
            capabilities=capabilities,
            discovery_mode=discovery_mode,
            mode=mode,
            apply_changes=apply_changes,
            plan=plan,
            tested_fp=tested_fp,
            apply_plan=apply_plan,
            remote_by_name=remote_by_name,
            remote_meta=remote_meta,
            existing_by_name=existing_by_name,
        )

    @staticmethod
    def _factory_apply_fingerprint_drift_error(
        *,
        locked: MCPServer,
        tested_fp: str,
    ) -> str | None:
        """Require locked current FP == tested snapshot == verified Connection Test."""

        from app.services.mcp_server import (
            connection_config_fingerprint_for_server,
            get_verified_connection_fingerprint,
        )

        current_fp = connection_config_fingerprint_for_server(locked)
        verified_fp = get_verified_connection_fingerprint(locked)
        if (
            verified_fp is not None
            and current_fp == tested_fp
            and current_fp == verified_fp
        ):
            return None
        return (
            "Connection configuration changed during Factory Discovery apply; "
            "stale snapshot discarded without Tool/ToolVersion mutation."
        )

    async def _finalize_factory_discovery(
        self,
        *,
        config: ServerConfig,
        discovery_id: uuid.UUID,
        capabilities: dict[str, Any] | None,
        discovery_mode: str,
        mode: str,
        apply_changes: bool,
        plan: FactoryReconcilePlan,
        tested_fp: str,
        apply_plan: bool,
        remote_by_name: dict[str, RemoteToolDescriptor],
        remote_meta: dict[str, dict[str, Any]],
        existing_by_name: dict[str, MCPTool],
    ) -> DiscoveryResponse:
        from app.services.mcp_server import (
            transport_config_clear_discovery_fingerprint,
            transport_config_with_discovery_fingerprint,
        )

        diff_summary = factory_diff_as_generic_counts(plan)
        factory_raw = plan.to_raw()
        factory_raw["connection_fingerprint"] = tested_fp
        tool_summaries = [
            {
                "remote_name": e.remote_name,
                "change": e.classification,
                "content_hash": remote_meta.get(e.remote_name, {}).get("content_hash"),
                "validation_status": remote_meta.get(e.remote_name, {}).get(
                    "validation_status"
                ),
            }
            for e in sorted(plan.entries, key=lambda x: x.remote_name)
        ]

        negotiated = CURRENT_MCP_PROTOCOL_VERSION
        finished = datetime.now(UTC)
        discovery = await self._discoveries.get_discovery(discovery_id)
        assert discovery is not None
        clean = plan.is_clean
        discovery.success = clean
        discovery.discovery_mode = discovery_mode
        discovery.selected_version = negotiated
        discovery.capabilities = capabilities
        discovery.finished_at = finished
        if clean:
            discovery.error_code = None
            discovery.error_message = None
        else:
            discovery.error_code = (
                "FACTORY_DISCOVERY_AMBIGUOUS"
                if plan.status == "AMBIGUOUS"
                else "FACTORY_DISCOVERY_MISMATCH"
            )
            discovery.error_message = plan.error_message or (
                "Factory Discovery reconciliation reported unresolved mismatches."
            )
        discovery.raw_response = {
            "apply_changes": apply_changes,
            "mode": mode,
            "diff": diff_summary,
            "tool_summaries": tool_summaries,
            "factory_reconciliation": factory_raw,
            "factory_apply_executed": apply_plan,
        }

        server = await self._require_server(config.id)
        server.discovery_mode = discovery_mode
        server.capabilities = capabilities
        server.negotiated_protocol_version = negotiated
        if clean:
            server.last_healthy_at = finished
            server.transport_config = transport_config_with_discovery_fingerprint(
                server.transport_config,
                fingerprint=tested_fp,
            )
        else:
            server.last_error_at = finished
            server.transport_config = transport_config_clear_discovery_fingerprint(
                server.transport_config
            )
        await self._session.commit()
        await self._session.refresh(discovery)
        return _discovery_to_response(discovery)

    async def _apply_factory_matched(
        self,
        *,
        server_id: uuid.UUID,
        remote_by_name: dict[str, RemoteToolDescriptor],
        remote_meta: dict[str, dict[str, Any]],
        existing_by_name: dict[str, MCPTool],
        matched_names: list[str],
    ) -> None:
        """Reconcile MATCHED tools only; keep INACTIVE; preserve Factory lineage."""

        del server_id  # identity comes from existing tools
        for name in matched_names:
            desc = remote_by_name[name]
            meta = remote_meta[name]
            tool = existing_by_name[name]
            prior_annotations: dict[str, Any] | None = None
            if tool.current_version_id is not None:
                prior = await self._tools.get_version(tool.current_version_id)
                if prior is not None and isinstance(prior.annotations, dict):
                    prior_annotations = prior.annotations
            lineage = extract_factory_lineage_annotations(prior_annotations)
            merged_annotations = {
                **(desc.annotations or {}),
                **lineage,
            }
            stored_desc = RemoteToolDescriptor(
                name=desc.name,
                description=desc.description,
                input_schema=desc.input_schema,
                output_schema=desc.output_schema,
                annotations=merged_annotations,
                raw=desc.raw,
            )
            # content_hash identity stays the remote wire hash (meta); annotations
            # carry Factory lineage without affecting contract matching.
            version = await self._ensure_version(tool.id, stored_desc, meta)
            tool.current_version_id = version.id
            # Never auto-ACTIVE / DISCOVERED; imported Tools stay INACTIVE.
            if tool.status != MCPToolStatus.INACTIVE:
                tool.status = MCPToolStatus.INACTIVE
            await self._tools.touch_last_seen(tool)
            await self._session.flush()

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
            tool = await self._tools.get_or_create_tool(
                mcp_server_id=server_id,
                remote_name=name,
            )
            tool_row, _created = tool
            if tool_row.status == MCPToolStatus.MISSING:
                tool_row.status = MCPToolStatus.DISCOVERED
            elif _created:
                tool_row.status = MCPToolStatus.DISCOVERED
            version = await self._ensure_version(tool_row.id, desc, meta)
            tool_row.current_version_id = version.id
            await self._session.flush()

        for name in changed:
            desc = remote_by_name[name]
            meta = remote_meta[name]
            tool = existing_by_name[name]
            version = await self._ensure_version(tool.id, desc, meta)
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

    async def _ensure_version(
        self,
        tool_id: uuid.UUID,
        desc: RemoteToolDescriptor,
        meta: dict[str, Any],
    ):
        existing = await self._tools.get_version_by_hash(tool_id, meta["content_hash"])
        if existing is not None:
            return existing
        version_no = await self._tools.next_version_no(tool_id)
        try:
            async with self._session.begin_nested():
                return await self._tools.create_version(
                    mcp_tool_id=tool_id,
                    version_no=version_no,
                    content_hash=meta["content_hash"],
                    validation_status=meta["validation_status"],
                    remote_description=desc.description,
                    input_schema=schema_for_storage(desc.input_schema),
                    output_schema=schema_for_storage(desc.output_schema),
                    annotations=desc.annotations,
                    raw_descriptor=desc.raw,
                    validation_errors=meta["validation_errors"],
                )
        except IntegrityError:
            recovered = await self._tools.get_version_by_hash(tool_id, meta["content_hash"])
            if recovered is not None:
                return recovered
            raise

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
