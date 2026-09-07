"""MCP ToolVersion Verification service (docs/05 §8.4, docs/09 §13)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import ToolVerificationStatus, ToolVersionValidationStatus
from app.models.mcp import MCPToolVerification
from app.repositories.mcp_discovery import MCPDiscoveryRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_verification import MCPToolVerificationRepository
from app.schemas.mcp_tool import ToolVerificationCreate


class MCPToolVerificationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._tools = MCPToolRepository(session)
        self._verifications = MCPToolVerificationRepository(session)
        self._discoveries = MCPDiscoveryRepository(session)
        self._servers = MCPServerRepository(session)

    async def _require_version(self, tool_id: uuid.UUID, version_id: uuid.UUID):
        tool = await self._tools.get(tool_id)
        if tool is None:
            raise AppError(
                code="NOT_FOUND",
                message="MCP tool not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        version = await self._tools.get_version(version_id)
        if version is None or version.mcp_tool_id != tool_id:
            raise AppError(
                code="NOT_FOUND",
                message="MCP tool version not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return tool, version

    async def list(
        self,
        tool_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[MCPToolVerification], int]:
        await self._require_version(tool_id, version_id)
        return await self._verifications.list_for_version(
            mcp_tool_version_id=version_id,
            page=page,
            page_size=page_size,
        )

    async def get(
        self,
        tool_id: uuid.UUID,
        version_id: uuid.UUID,
        verification_id: uuid.UUID,
    ) -> MCPToolVerification:
        await self._require_version(tool_id, version_id)
        row = await self._verifications.get(verification_id)
        if row is None or row.mcp_tool_version_id != version_id:
            raise AppError(
                code="NOT_FOUND",
                message="MCP tool verification not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return row

    def _validate_verified_result_summary(self, result_summary: dict[str, Any] | None) -> None:
        if not isinstance(result_summary, dict):
            raise AppError(
                code="VALIDATION_ERROR",
                message="result_summary is required for VERIFIED status.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        required_true = (
            "schema_valid",
            "normal_call_passed",
            "error_handling_checked",
        )
        missing = [key for key in required_true if result_summary.get(key) is not True]
        if missing:
            raise AppError(
                code="VALIDATION_ERROR",
                message=(
                    "VERIFIED result_summary must set "
                    f"{', '.join(required_true)} to true."
                ),
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                details=[{"missing_or_false": missing}],
            )

    async def _assert_verified_preconditions(
        self,
        *,
        tool_id: uuid.UUID,
        version_id: uuid.UUID,
        data: ToolVerificationCreate,
    ) -> None:
        _tool, version = await self._require_version(tool_id, version_id)

        if version.validation_status != ToolVersionValidationStatus.VALID:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "VERIFIED requires ToolVersion validation_status VALID; "
                    f"current is {version.validation_status}."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )

        tool = await self._tools.get(tool_id)
        assert tool is not None
        server = await self._servers.get(tool.mcp_server_id)
        if server is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Tool's MCP Server is not available for verification.",
                status_code=status.HTTP_409_CONFLICT,
            )

        if not await self._discoveries.has_succeeded_check(server.id):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="VERIFIED requires a SUCCEEDED MCP Server connection check.",
                status_code=status.HTTP_409_CONFLICT,
            )

        if not (
            server.negotiated_protocol_version
            or server.discovery_mode
            or server.capabilities
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "VERIFIED requires protocol/discovery metadata on the MCP Server "
                    "(negotiated_protocol_version, discovery_mode, or capabilities)."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )

        if not await self._discoveries.has_successful_discovery(server.id):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="VERIFIED requires a successful Discovery history record.",
                status_code=status.HTTP_409_CONFLICT,
            )

        if data.test_execution_id is None:
            raise AppError(
                code="VALIDATION_ERROR",
                message="test_execution_id is required for VERIFIED status.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        if not data.criteria_version.strip():
            raise AppError(
                code="VALIDATION_ERROR",
                message="criteria_version is required for VERIFIED status.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        if data.evidence_blob_id is None:
            raise AppError(
                code="VALIDATION_ERROR",
                message="evidence_blob_id is required for VERIFIED status.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        self._validate_verified_result_summary(data.result_summary)

        if data.expires_at is not None:
            expires = data.expires_at
            if expires.tzinfo is None:
                raise AppError(
                    code="VALIDATION_ERROR",
                    message="expires_at must be timezone-aware ISO-8601.",
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            if expires <= datetime.now(UTC):
                raise AppError(
                    code="VALIDATION_ERROR",
                    message="expires_at must be in the future for VERIFIED evidence.",
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )

    async def create(
        self,
        tool_id: uuid.UUID,
        version_id: uuid.UUID,
        data: ToolVerificationCreate,
    ) -> MCPToolVerification:
        await self._require_version(tool_id, version_id)

        if data.status == ToolVerificationStatus.EXPIRED:
            raise AppError(
                code="VALIDATION_ERROR",
                message="EXPIRED status cannot be created directly; use maintenance transition.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        if data.status == ToolVerificationStatus.VERIFIED:
            await self._assert_verified_preconditions(
                tool_id=tool_id,
                version_id=version_id,
                data=data,
            )
        else:
            if not data.criteria_version.strip():
                raise AppError(
                    code="VALIDATION_ERROR",
                    message="criteria_version is required.",
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )

        # verified_by is intentionally null until Auth principal infrastructure exists.
        row = await self._verifications.create(
            mcp_tool_version_id=version_id,
            status=str(data.status),
            criteria_version=data.criteria_version.strip(),
            test_execution_id=data.test_execution_id,
            result_summary=data.result_summary,
            evidence_blob_id=data.evidence_blob_id,
            expires_at=data.expires_at,
            verified_by=None,
            verified_at=datetime.now(UTC),
        )
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def has_effective_current_verification(self, tool_id: uuid.UUID) -> bool:
        tool = await self._tools.get(tool_id)
        if tool is None or tool.current_version_id is None:
            return False
        return await self._verifications.has_effective_verified(tool.current_version_id)

    async def count_tools_with_effective_current_verification(self) -> int:
        return await self._verifications.count_tools_with_effective_current_verification()
