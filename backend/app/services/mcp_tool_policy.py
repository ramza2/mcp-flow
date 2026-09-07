"""MCP Tool Policy service (docs/05 §8.3, docs/06 §9)."""

from __future__ import annotations

import uuid

from fastapi import status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import ApprovalPolicyStatus
from app.models.mcp import MCPToolPolicy
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.schemas.mcp_tool import MCPToolPolicyPut


class MCPToolPolicyService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._tools = MCPToolRepository(session)
        self._policies = MCPToolPolicyRepository(session)
        self._approval_policies = ApprovalPolicyRepository(session)

    async def _require_tool(self, tool_id: uuid.UUID) -> None:
        tool = await self._tools.get(tool_id)
        if tool is None:
            raise AppError(
                code="NOT_FOUND",
                message="MCP tool not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

    def _raise_concurrent_create(self) -> None:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                "MCP tool policy was created concurrently; "
                "refetch and update with lock_version."
            ),
            status_code=status.HTTP_409_CONFLICT,
        )

    async def get(self, tool_id: uuid.UUID) -> MCPToolPolicy:
        await self._require_tool(tool_id)
        policy = await self._policies.get_by_tool_id(tool_id)
        if policy is None:
            raise AppError(
                code="NOT_FOUND",
                message="MCP tool policy not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return policy

    async def _validate_approval_reference(self, data: MCPToolPolicyPut) -> uuid.UUID | None:
        if data.requires_approval:
            if data.approval_policy_id is None:
                raise AppError(
                    code="VALIDATION_ERROR",
                    message="approval_policy_id is required when requires_approval is true.",
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            approval = await self._approval_policies.get(data.approval_policy_id)
            if approval is None:
                raise AppError(
                    code="VALIDATION_ERROR",
                    message="approval_policy_id does not reference an existing ApprovalPolicy.",
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            if approval.status != ApprovalPolicyStatus.ACTIVE:
                raise AppError(
                    code="VALIDATION_ERROR",
                    message="approval_policy_id must reference an ACTIVE ApprovalPolicy.",
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            return data.approval_policy_id

        if data.approval_policy_id is not None:
            raise AppError(
                code="VALIDATION_ERROR",
                message="approval_policy_id must be null when requires_approval is false.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        return None

    async def _create_under_tool_lock(
        self,
        tool_id: uuid.UUID,
        fields: dict,
    ) -> MCPToolPolicy:
        locked = await self._tools.lock_for_update(tool_id)
        if locked is None:
            raise AppError(
                code="NOT_FOUND",
                message="MCP tool not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        existing = await self._policies.get_by_tool_id(tool_id)
        if existing is not None:
            self._raise_concurrent_create()

        try:
            created = await self._policies.create(mcp_tool_id=tool_id, **fields)
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            self._raise_concurrent_create()

        await self._session.refresh(created)
        return created

    async def put(
        self,
        tool_id: uuid.UUID,
        data: MCPToolPolicyPut,
        *,
        expected_lock_version: int | None,
    ) -> MCPToolPolicy:
        await self._require_tool(tool_id)
        approval_policy_id = await self._validate_approval_reference(data)
        existing = await self._policies.get_by_tool_id(tool_id)

        fields = {
            "risk_class": str(data.risk_class),
            "requires_confirmation": data.requires_confirmation,
            "requires_approval": data.requires_approval,
            "approval_policy_id": approval_policy_id,
            "timeout_ms": data.timeout_ms,
            "max_attempts": data.max_attempts,
            "backoff_policy": data.backoff_policy,
            "max_result_bytes": data.max_result_bytes,
            "allow_auto_select": data.allow_auto_select,
            "data_classification": data.data_classification,
            "policy_metadata": data.policy_metadata,
        }

        if existing is None:
            return await self._create_under_tool_lock(tool_id, fields)

        if expected_lock_version is None:
            raise AppError(
                code="VALIDATION_ERROR",
                message="Updating an existing Tool Policy requires If-Match or body.lock_version.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        updated = await self._policies.update_atomic(
            existing.id,
            expected_lock_version=expected_lock_version,
            **fields,
        )
        if updated is None:
            current = await self._policies.get_by_tool_id(tool_id)
            if current is None:
                raise AppError(
                    code="NOT_FOUND",
                    message="MCP tool policy not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            raise AppError(
                code="RESOURCE_VERSION_CONFLICT",
                message="MCP tool policy lock_version does not match.",
                status_code=status.HTTP_409_CONFLICT,
            )

        await self._session.commit()
        await self._session.refresh(updated)
        return updated
