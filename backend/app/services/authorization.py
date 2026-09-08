"""ResourceGrant service and AuthorizationResolver (docs/05–06, FNC-AUTH-003/004)."""

from __future__ import annotations

import uuid

from fastapi import status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import ResourceGrantResourceType, UserStatus
from app.models.auth import ResourceGrant
from app.repositories.agent import AgentRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.resource_grant import ResourceGrantRepository
from app.repositories.role import (
    RolePermissionRepository,
    RoleRepository,
    UserRoleRepository,
)
from app.repositories.user import UserRepository
from app.schemas.auth import AuthorizationDecision, ResourceGrantCreate


class ResourceGrantService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._grants = ResourceGrantRepository(session)
        self._users = UserRepository(session)
        self._roles = RoleRepository(session)
        self._agents = AgentRepository(session)
        self._servers = MCPServerRepository(session)
        self._tools = MCPToolRepository(session)

    async def _require_user(self, user_id: uuid.UUID) -> None:
        if await self._users.get(user_id) is None:
            raise AppError(
                code="NOT_FOUND",
                message="User not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

    async def _require_role(self, role_id: uuid.UUID) -> None:
        if await self._roles.get(role_id) is None:
            raise AppError(
                code="NOT_FOUND",
                message="Role not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

    async def _assert_resource_exists(
        self, resource_type: ResourceGrantResourceType, resource_id: uuid.UUID
    ) -> None:
        if resource_type == ResourceGrantResourceType.WORKFLOW:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Workflow resource registry is not implemented yet.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if resource_type == ResourceGrantResourceType.AGENT:
            if await self._agents.get(resource_id) is None:
                raise AppError(
                    code="VALIDATION_ERROR",
                    message=f"AGENT resource_id {resource_id} does not exist.",
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            return
        if resource_type == ResourceGrantResourceType.MCP_SERVER:
            if await self._servers.get(resource_id) is None:
                raise AppError(
                    code="VALIDATION_ERROR",
                    message=f"MCP_SERVER resource_id {resource_id} does not exist.",
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            return
        if resource_type == ResourceGrantResourceType.MCP_TOOL:
            if await self._tools.get(resource_id) is None:
                raise AppError(
                    code="VALIDATION_ERROR",
                    message=f"MCP_TOOL resource_id {resource_id} does not exist.",
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            return
        raise AppError(
            code="VALIDATION_ERROR",
            message=f"Unsupported resource_type: {resource_type}.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    async def list_for_user(
        self, user_id: uuid.UUID, *, page: int = 1, page_size: int = 20
    ) -> tuple[list[ResourceGrant], int]:
        await self._require_user(user_id)
        return await self._grants.list_for_user(user_id, page=page, page_size=page_size)

    async def list_for_role(
        self, role_id: uuid.UUID, *, page: int = 1, page_size: int = 20
    ) -> tuple[list[ResourceGrant], int]:
        await self._require_role(role_id)
        return await self._grants.list_for_role(role_id, page=page, page_size=page_size)

    async def create_for_user(
        self, user_id: uuid.UUID, data: ResourceGrantCreate
    ) -> ResourceGrant:
        await self._require_user(user_id)
        await self._assert_resource_exists(data.resource_type, data.resource_id)
        try:
            grant = await self._grants.create(
                user_id=user_id,
                role_id=None,
                resource_type=str(data.resource_type),
                resource_id=data.resource_id,
            )
            await self._session.commit()
            await self._session.refresh(grant)
            return grant
        except IntegrityError as exc:
            await self._session.rollback()
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Resource grant already exists for this user and resource.",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc

    async def create_for_role(
        self, role_id: uuid.UUID, data: ResourceGrantCreate
    ) -> ResourceGrant:
        await self._require_role(role_id)
        await self._assert_resource_exists(data.resource_type, data.resource_id)
        try:
            grant = await self._grants.create(
                user_id=None,
                role_id=role_id,
                resource_type=str(data.resource_type),
                resource_id=data.resource_id,
            )
            await self._session.commit()
            await self._session.refresh(grant)
            return grant
        except IntegrityError as exc:
            await self._session.rollback()
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Resource grant already exists for this role and resource.",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc

    async def delete_for_user(self, user_id: uuid.UUID, grant_id: uuid.UUID) -> None:
        await self._require_user(user_id)
        grant = await self._grants.get_for_user(user_id, grant_id)
        if grant is None:
            raise AppError(
                code="NOT_FOUND",
                message="Resource grant not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        await self._grants.delete(grant)
        await self._session.commit()

    async def delete_for_role(self, role_id: uuid.UUID, grant_id: uuid.UUID) -> None:
        await self._require_role(role_id)
        grant = await self._grants.get_for_role(role_id, grant_id)
        if grant is None:
            raise AppError(
                code="NOT_FOUND",
                message="Resource grant not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        await self._grants.delete(grant)
        await self._session.commit()


class AuthorizationResolver:
    """User Permission + ResourceGrant authorization boundary (no Session principal)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._users = UserRepository(session)
        self._user_roles = UserRoleRepository(session)
        self._role_permissions = RolePermissionRepository(session)
        self._grants = ResourceGrantRepository(session)

    async def get_effective_permission_codes(self, user_id: uuid.UUID) -> set[str]:
        user = await self._users.get(user_id)
        if user is None or user.status != UserStatus.ACTIVE:
            return set()
        role_ids = await self._user_roles.list_role_ids(user_id)
        codes: set[str] = set()
        for role_id in role_ids:
            for permission in await self._role_permissions.list_permissions(role_id):
                codes.add(permission.code)
        return codes

    async def has_permission(self, user_id: uuid.UUID, permission_code: str) -> bool:
        codes = await self.get_effective_permission_codes(user_id)
        return permission_code in codes

    async def has_resource_grant(
        self,
        user_id: uuid.UUID,
        resource_type: str | ResourceGrantResourceType,
        resource_id: uuid.UUID,
    ) -> bool:
        user = await self._users.get(user_id)
        if user is None or user.status != UserStatus.ACTIVE:
            return False
        return await self._grants.has_effective_grant(
            user_id,
            resource_type=str(resource_type),
            resource_id=resource_id,
        )

    async def authorize_resource(
        self,
        user_id: uuid.UUID,
        permission_code: str,
        resource_type: str | ResourceGrantResourceType,
        resource_id: uuid.UUID,
    ) -> AuthorizationDecision:
        user = await self._users.get(user_id)
        if user is None:
            return AuthorizationDecision(allowed=False, reason_code="USER_NOT_FOUND")
        if user.status != UserStatus.ACTIVE:
            return AuthorizationDecision(allowed=False, reason_code="USER_NOT_ACTIVE")

        if not await self.has_permission(user_id, permission_code):
            return AuthorizationDecision(
                allowed=False, reason_code="PERMISSION_MISSING"
            )

        if not await self._grants.has_effective_grant(
            user_id,
            resource_type=str(resource_type),
            resource_id=resource_id,
        ):
            return AuthorizationDecision(
                allowed=False, reason_code="RESOURCE_GRANT_MISSING"
            )

        return AuthorizationDecision(allowed=True, reason_code="ALLOWED")
