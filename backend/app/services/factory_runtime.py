"""Factory RuntimeBinding lifecycle + lineage-validated package load (Issue #101)."""

from __future__ import annotations

import logging
import uuid

from fastapi import status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.url_validation import validate_outbound_base_url
from app.domain.enums import (
    FactoryRuntimeBindingStatus,
    MCPAuthType,
)
from app.factory.errors import (
    FACTORY_PACKAGE_HASH_MISMATCH,
    FACTORY_PACKAGE_LINEAGE_MISMATCH,
    FACTORY_RUNTIME_ALREADY_EXISTS,
    FACTORY_RUNTIME_BASE_URL_FORBIDDEN,
    FACTORY_RUNTIME_CONFIG_INVALID,
    FACTORY_RUNTIME_CREDENTIAL_PARAM_FORBIDDEN,
    FACTORY_RUNTIME_LINEAGE_MISMATCH,
    FACTORY_RUNTIME_NOT_READY,
    FACTORY_RUNTIME_PUBLISH_INVALID,
)
from app.factory.import_input_schema import build_mcp_callable_input_schema
from app.factory.runtime_http_adapter import (
    DeclarativeToolContract,
    FactoryRuntimeAdapterError,
    package_tool_to_contract,
)
from app.factory.runtime_limits import (
    DEFAULT_CALL_TIMEOUT_MS,
    DEFAULT_CONNECT_TIMEOUT_MS,
    DEFAULT_MAX_RESPONSE_BYTES,
)
from app.factory.runtime_mcp import build_mcp_endpoint_url
from app.infrastructure.object_storage import ObjectStorageClient, get_object_storage
from app.models.factory import (
    ToolFactoryGeneratedPackage,
    ToolFactoryPublish,
    ToolFactoryReview,
    ToolFactoryRuntimeBinding,
)
from app.models.mcp import MCPServer
from app.repositories.factory import FactoryRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.schemas.factory import (
    FactoryRuntimeBindingResponse,
    FactoryRuntimeBindingUpdate,
)
from app.services.factory import FactoryService
from app.services.mcp_server import MCPServerService

logger = logging.getLogger(__name__)

# RuntimeBinding fields that change outbound execution semantics. Mutations
# invalidate Factory MCPServer Activate evidence (#97/#99) because the MCP
# endpoint URL stays stable on binding_id.
_EXECUTION_RELEVANT_KEYS = frozenset(
    {
        "base_url",
        "auth_type",
        "auth_secret_id",
        "connect_timeout_ms",
        "call_timeout_ms",
        "max_response_bytes",
    }
)


class FactoryRuntimeService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        storage: ObjectStorageClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._repo = FactoryRepository(session)
        self._servers = MCPServerRepository(session)
        self._tools = MCPToolRepository(session)
        self._mcp_servers = MCPServerService(session)
        self._factory = FactoryService(session, storage=storage)
        self._storage = storage
        self._settings = settings or get_settings()

    def _storage_client(self) -> ObjectStorageClient:
        if self._storage is not None:
            return self._storage
        return get_object_storage()

    def _outbound_allowlist(self) -> list[str]:
        raw = self._settings.factory_runtime_outbound_host_allowlist or ""
        return [part.strip() for part in raw.split(",") if part.strip()]

    def _to_response(
        self, row: ToolFactoryRuntimeBinding
    ) -> FactoryRuntimeBindingResponse:
        endpoint = None
        if row.status == FactoryRuntimeBindingStatus.READY.value:
            endpoint = build_mcp_endpoint_url(
                public_base_url=self._settings.factory_runtime_public_base_url,
                binding_id=row.id,
            )
        return FactoryRuntimeBindingResponse(
            id=row.id,
            publish_id=row.publish_id,
            mcp_server_id=row.mcp_server_id,
            review_id=row.review_id,
            generated_package_id=row.generated_package_id,
            package_content_sha256=row.package_content_sha256,
            status=FactoryRuntimeBindingStatus(row.status),
            base_url=row.base_url,
            auth_type=MCPAuthType(row.auth_type),
            auth_secret_id=row.auth_secret_id,
            connect_timeout_ms=int(row.connect_timeout_ms),
            call_timeout_ms=int(row.call_timeout_ms),
            max_response_bytes=int(row.max_response_bytes),
            mcp_endpoint_url=endpoint,
            last_error_code=row.last_error_code,
            last_error_message=row.last_error_message,
            created_by=row.created_by,
            lock_version=int(row.lock_version),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def _assert_manage(self, actor_user_id: uuid.UUID) -> None:
        await self._factory._assert_manage(actor_user_id)  # noqa: SLF001

    async def _assert_read(self, actor_user_id: uuid.UUID) -> None:
        await self._factory._assert_read(actor_user_id)  # noqa: SLF001

    async def create_binding(
        self,
        *,
        actor_user_id: uuid.UUID,
        publish_id: uuid.UUID,
    ) -> FactoryRuntimeBindingResponse:
        await self._assert_manage(actor_user_id)
        publish, _review, _package, _server = await self._load_and_validate_lineage(
            publish_id=publish_id
        )
        existing = await self._repo.get_runtime_binding_for_publish(publish.id)
        if existing is not None:
            raise AppError(
                code=FACTORY_RUNTIME_ALREADY_EXISTS,
                message="RuntimeBinding already exists for this FactoryPublish.",
                status_code=status.HTTP_409_CONFLICT,
            )
        binding_id = uuid.uuid4()
        try:
            row = await self._repo.create_runtime_binding(
                binding_id=binding_id,
                publish_id=publish.id,
                mcp_server_id=publish.mcp_server_id,
                review_id=publish.review_id,
                generated_package_id=publish.generated_package_id,
                package_content_sha256=publish.package_content_sha256,
                status=FactoryRuntimeBindingStatus.DRAFT.value,
                created_by=actor_user_id,
                connect_timeout_ms=DEFAULT_CONNECT_TIMEOUT_MS,
                call_timeout_ms=DEFAULT_CALL_TIMEOUT_MS,
                max_response_bytes=DEFAULT_MAX_RESPONSE_BYTES,
            )
            await self._session.commit()
            await self._session.refresh(row)
        except IntegrityError as exc:
            await self._session.rollback()
            raise AppError(
                code=FACTORY_RUNTIME_ALREADY_EXISTS,
                message="RuntimeBinding already exists for this FactoryPublish.",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc
        logger.info(
            "factory_runtime_binding_created binding_id=%s publish_id=%s",
            row.id,
            publish.id,
        )
        return self._to_response(row)

    async def get_binding(
        self, actor_user_id: uuid.UUID, binding_id: uuid.UUID
    ) -> FactoryRuntimeBindingResponse:
        await self._assert_read(actor_user_id)
        row = await self._repo.get_runtime_binding(binding_id)
        if row is None:
            raise AppError(
                code="NOT_FOUND",
                message="RuntimeBinding not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return self._to_response(row)

    async def get_binding_for_publish(
        self, actor_user_id: uuid.UUID, publish_id: uuid.UUID
    ) -> FactoryRuntimeBindingResponse:
        await self._assert_read(actor_user_id)
        row = await self._repo.get_runtime_binding_for_publish(publish_id)
        if row is None:
            raise AppError(
                code="NOT_FOUND",
                message="RuntimeBinding not found for publish.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return self._to_response(row)

    async def update_binding(
        self,
        *,
        actor_user_id: uuid.UUID,
        binding_id: uuid.UUID,
        data: FactoryRuntimeBindingUpdate,
    ) -> FactoryRuntimeBindingResponse:
        await self._assert_manage(actor_user_id)
        row = await self._repo.lock_runtime_binding(binding_id)
        if row is None:
            raise AppError(
                code="NOT_FOUND",
                message="RuntimeBinding not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if int(row.lock_version) != int(data.lock_version):
            raise AppError(
                code="RESOURCE_VERSION_CONFLICT",
                message="RuntimeBinding lock_version does not match.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if row.status == FactoryRuntimeBindingStatus.DISABLED.value:
            raise AppError(
                code=FACTORY_RUNTIME_CONFIG_INVALID,
                message="DISABLED RuntimeBinding cannot be updated.",
                status_code=status.HTTP_409_CONFLICT,
            )

        # Revalidate lineage on every mutation.
        await self._load_and_validate_lineage(publish_id=row.publish_id)

        payload = data.model_dump(exclude_unset=True, exclude={"lock_version"})
        if "base_url" in payload and payload["base_url"] is not None:
            try:
                payload["base_url"] = validate_outbound_base_url(
                    str(payload["base_url"]),
                    hostname_allowlist=self._outbound_allowlist(),
                )
            except AppError as exc:
                if exc.code == "VALIDATION_ERROR":
                    raise AppError(
                        code=FACTORY_RUNTIME_BASE_URL_FORBIDDEN
                        if "forbidden" in exc.message.lower()
                        or "private" in exc.message.lower()
                        or "localhost" in exc.message.lower()
                        else FACTORY_RUNTIME_CONFIG_INVALID,
                        message=exc.message,
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    ) from exc
                raise
        if "auth_type" in payload and payload["auth_type"] is not None:
            auth = MCPAuthType(str(payload["auth_type"]))
            if auth not in {MCPAuthType.NONE, MCPAuthType.BEARER, MCPAuthType.BASIC}:
                raise AppError(
                    code=FACTORY_RUNTIME_CONFIG_INVALID,
                    message="Unsupported runtime auth_type for this foundation slice.",
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            payload["auth_type"] = auth.value
            if auth == MCPAuthType.NONE:
                payload["auth_secret_id"] = None
            elif payload.get("auth_secret_id", row.auth_secret_id) is None:
                raise AppError(
                    code=FACTORY_RUNTIME_CONFIG_INVALID,
                    message="auth_secret_id is required for credential auth_type.",
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )

        execution_changed = False
        for key, value in payload.items():
            if key in _EXECUTION_RELEVANT_KEYS and getattr(row, key) != value:
                execution_changed = True
            setattr(row, key, value)
        # Execution-relevant change from READY drops back to DRAFT
        # (stable binding_id MCP URL must not keep prior Activate evidence).
        if (
            execution_changed
            and row.status == FactoryRuntimeBindingStatus.READY.value
        ):
            row.status = FactoryRuntimeBindingStatus.DRAFT.value
        row.lock_version = int(row.lock_version) + 1
        if execution_changed:
            # Same TX: clear Factory Activate evidence for the associated server.
            await self._mcp_servers.invalidate_factory_runtime_evidence(
                row.mcp_server_id,
                commit=False,
            )
        await self._session.commit()
        await self._session.refresh(row)
        return self._to_response(row)

    async def mark_ready(
        self, *, actor_user_id: uuid.UUID, binding_id: uuid.UUID, lock_version: int
    ) -> FactoryRuntimeBindingResponse:
        await self._assert_manage(actor_user_id)
        row = await self._repo.lock_runtime_binding(binding_id)
        if row is None:
            raise AppError(
                code="NOT_FOUND",
                message="RuntimeBinding not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if int(row.lock_version) != int(lock_version):
            raise AppError(
                code="RESOURCE_VERSION_CONFLICT",
                message="RuntimeBinding lock_version does not match.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if row.status == FactoryRuntimeBindingStatus.DISABLED.value:
            raise AppError(
                code=FACTORY_RUNTIME_CONFIG_INVALID,
                message="DISABLED RuntimeBinding cannot become READY.",
                status_code=status.HTTP_409_CONFLICT,
            )
        await self._load_and_validate_lineage(publish_id=row.publish_id)
        if not row.base_url:
            raise AppError(
                code=FACTORY_RUNTIME_CONFIG_INVALID,
                message="base_url is required before READY.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        # Re-validate stored base_url against current policy.
        row.base_url = validate_outbound_base_url(
            row.base_url,
            hostname_allowlist=self._outbound_allowlist(),
        )
        if row.auth_type != MCPAuthType.NONE.value and row.auth_secret_id is None:
            raise AppError(
                code=FACTORY_RUNTIME_CONFIG_INVALID,
                message="auth_secret_id is required for credential auth_type.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        # Ensure reviewed tool contracts can be loaded.
        await self.load_reviewed_contracts(row)
        row.status = FactoryRuntimeBindingStatus.READY.value
        row.last_error_code = None
        row.last_error_message = None
        row.lock_version = int(row.lock_version) + 1
        await self._session.commit()
        await self._session.refresh(row)
        logger.info("factory_runtime_binding_ready binding_id=%s", row.id)
        return self._to_response(row)

    async def disable_binding(
        self, *, actor_user_id: uuid.UUID, binding_id: uuid.UUID, lock_version: int
    ) -> FactoryRuntimeBindingResponse:
        await self._assert_manage(actor_user_id)
        row = await self._repo.lock_runtime_binding(binding_id)
        if row is None:
            raise AppError(
                code="NOT_FOUND",
                message="RuntimeBinding not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if int(row.lock_version) != int(lock_version):
            raise AppError(
                code="RESOURCE_VERSION_CONFLICT",
                message="RuntimeBinding lock_version does not match.",
                status_code=status.HTTP_409_CONFLICT,
            )
        row.status = FactoryRuntimeBindingStatus.DISABLED.value
        row.lock_version = int(row.lock_version) + 1
        await self._mcp_servers.invalidate_factory_runtime_evidence(
            row.mcp_server_id,
            commit=False,
        )
        await self._session.commit()
        await self._session.refresh(row)
        return self._to_response(row)

    async def _load_and_validate_lineage(
        self, *, publish_id: uuid.UUID
    ) -> tuple[
        ToolFactoryPublish,
        ToolFactoryReview,
        ToolFactoryGeneratedPackage,
        MCPServer,
    ]:
        publish = await self._repo.get_publish(publish_id)
        if publish is None:
            raise AppError(
                code=FACTORY_RUNTIME_PUBLISH_INVALID,
                message="FactoryPublish not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        review = await self._repo.get_review(publish.review_id)
        if review is None or str(review.decision) != "APPROVE":
            raise AppError(
                code=FACTORY_RUNTIME_LINEAGE_MISMATCH,
                message="FactoryPublish requires an APPROVE Review.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if review.id != publish.review_id:
            raise AppError(
                code=FACTORY_RUNTIME_LINEAGE_MISMATCH,
                message="Publish review_id mismatch.",
                status_code=status.HTTP_409_CONFLICT,
            )
        package = await self._repo.get_generated_package(publish.generated_package_id)
        if package is None:
            raise AppError(
                code=FACTORY_RUNTIME_LINEAGE_MISMATCH,
                message="GeneratedPackage not found for FactoryPublish.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if package.package_content_sha256 != publish.package_content_sha256:
            raise AppError(
                code=FACTORY_PACKAGE_LINEAGE_MISMATCH,
                message="Publish package_content_sha256 does not match package row.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if (
            package.id != publish.generated_package_id
            or review.generated_package_id != publish.generated_package_id
        ):
            raise AppError(
                code=FACTORY_RUNTIME_LINEAGE_MISMATCH,
                message="generated_package_id lineage mismatch.",
                status_code=status.HTTP_409_CONFLICT,
            )
        # Fail closed on object bytes hash.
        try:
            await self._factory._assert_package_bytes_match(  # noqa: SLF001
                package,
                expected_content_sha256=publish.package_content_sha256,
            )
        except AppError:
            raise
        except Exception as exc:
            raise AppError(
                code=FACTORY_PACKAGE_HASH_MISMATCH,
                message="Package object revalidation failed.",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc

        server = await self._servers.get(publish.mcp_server_id)
        if server is None:
            raise AppError(
                code=FACTORY_RUNTIME_LINEAGE_MISMATCH,
                message="MCP Server for FactoryPublish not found.",
                status_code=status.HTTP_409_CONFLICT,
            )
        cfg = server.transport_config if isinstance(server.transport_config, dict) else {}
        if str(cfg.get("publish_id")) != str(publish.id):
            raise AppError(
                code=FACTORY_RUNTIME_LINEAGE_MISMATCH,
                message="MCP Server transport_config.publish_id mismatch.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if str(cfg.get("review_id")) != str(publish.review_id):
            raise AppError(
                code=FACTORY_RUNTIME_LINEAGE_MISMATCH,
                message="MCP Server transport_config.review_id mismatch.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if str(cfg.get("generated_package_id")) != str(publish.generated_package_id):
            raise AppError(
                code=FACTORY_RUNTIME_LINEAGE_MISMATCH,
                message="MCP Server transport_config.generated_package_id mismatch.",
                status_code=status.HTTP_409_CONFLICT,
            )
        package_sha = cfg.get("package_content_sha256")
        if package_sha is not None and str(package_sha) != publish.package_content_sha256:
            raise AppError(
                code=FACTORY_RUNTIME_LINEAGE_MISMATCH,
                message="MCP Server transport_config.package_content_sha256 mismatch.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if not publish.mcp_tool_ids:
            raise AppError(
                code=FACTORY_RUNTIME_PUBLISH_INVALID,
                message="FactoryPublish.mcp_tool_ids is empty.",
                status_code=status.HTTP_409_CONFLICT,
            )
        return publish, review, package, server

    async def load_reviewed_contracts(
        self, row: ToolFactoryRuntimeBinding
    ) -> dict[str, DeclarativeToolContract]:
        """Exact reviewed tool set: Publish.mcp_tool_ids ∩ package operation_key."""

        publish, _review, package, _server = await self._load_and_validate_lineage(
            publish_id=row.publish_id
        )
        payload = await self._factory._assert_package_bytes_match(  # noqa: SLF001
            package,
            expected_content_sha256=publish.package_content_sha256,
        )
        package_tools = self._factory._assert_package_payload_contract(  # noqa: SLF001
            payload,
            package=package,
            expected_generator_version=publish.generator_version,
            expected_package_schema_version=publish.package_schema_version,
        )
        package_by_key = {
            str(tool.get("operation_key")): tool
            for tool in package_tools
            if isinstance(tool, dict) and tool.get("operation_key")
        }

        contracts: dict[str, DeclarativeToolContract] = {}
        for raw_id in publish.mcp_tool_ids:
            tool_id = uuid.UUID(str(raw_id))
            tool = await self._tools.get(tool_id)
            if tool is None or tool.mcp_server_id != publish.mcp_server_id:
                raise AppError(
                    code=FACTORY_RUNTIME_LINEAGE_MISMATCH,
                    message=f"FactoryPublish tool {tool_id} missing on MCP Server.",
                    status_code=status.HTTP_409_CONFLICT,
                )
            if tool.current_version_id is None:
                raise AppError(
                    code=FACTORY_RUNTIME_LINEAGE_MISMATCH,
                    message=f"FactoryPublish tool {tool_id} has no current version.",
                    status_code=status.HTTP_409_CONFLICT,
                )
            version = await self._tools.get_version(tool.current_version_id)
            if version is None:
                raise AppError(
                    code=FACTORY_RUNTIME_LINEAGE_MISMATCH,
                    message=f"ToolVersion missing for tool {tool_id}.",
                    status_code=status.HTTP_409_CONFLICT,
                )
            pkg_tool = package_by_key.get(tool.remote_name)
            if pkg_tool is None:
                raise AppError(
                    code=FACTORY_RUNTIME_LINEAGE_MISMATCH,
                    message=(
                        f"Reviewed remote_name {tool.remote_name!r} missing from "
                        "package tools."
                    ),
                    status_code=status.HTTP_409_CONFLICT,
                )
            input_schema = version.input_schema
            if not isinstance(input_schema, dict):
                input_schema = build_mcp_callable_input_schema(
                    parameters=pkg_tool.get("parameters")
                    if isinstance(pkg_tool.get("parameters"), list)
                    else [],
                    request_schema=pkg_tool.get("request_schema"),
                    request_body_required=bool(
                        pkg_tool.get("request_body_required", False)
                    ),
                )
            try:
                contracts[tool.remote_name] = package_tool_to_contract(
                    pkg_tool,
                    input_schema=input_schema,
                    output_schema=version.output_schema,
                    remote_name=tool.remote_name,
                )
            except FactoryRuntimeAdapterError as exc:
                # Credential / cookie channels must fail at READY/load — never
                # reach outbound HTTP.
                status_code = (
                    status.HTTP_422_UNPROCESSABLE_ENTITY
                    if exc.code == FACTORY_RUNTIME_CREDENTIAL_PARAM_FORBIDDEN
                    else status.HTTP_409_CONFLICT
                )
                raise AppError(
                    code=exc.code,
                    message=exc.message,
                    status_code=status_code,
                ) from exc
        if len(contracts) != len(publish.mcp_tool_ids):
            raise AppError(
                code=FACTORY_RUNTIME_LINEAGE_MISMATCH,
                message="Reviewed tool set cardinality mismatch.",
                status_code=status.HTTP_409_CONFLICT,
            )
        return contracts

    async def load_handler_context(
        self, binding_id: uuid.UUID
    ) -> tuple[ToolFactoryRuntimeBinding, dict[str, DeclarativeToolContract]]:
        row = await self._repo.get_runtime_binding(binding_id)
        if row is None:
            raise AppError(
                code="NOT_FOUND",
                message="RuntimeBinding not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if row.status != FactoryRuntimeBindingStatus.READY.value:
            raise AppError(
                code=FACTORY_RUNTIME_NOT_READY,
                message="Factory runtime binding is not READY.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        contracts = await self.load_reviewed_contracts(row)
        return row, contracts
