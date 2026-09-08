"""Role lifecycle and permission membership service (docs/05–06)."""

from __future__ import annotations

import uuid

from fastapi import status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.auth import Permission, Role
from app.repositories.role import (
    PermissionRepository,
    RolePermissionRepository,
    RoleRepository,
)
from app.schemas.auth import RoleCreate, RolePermissionReplaceRequest, RoleUpdate


class RoleService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._roles = RoleRepository(session)
        self._permissions = PermissionRepository(session)
        self._role_permissions = RolePermissionRepository(session)

    async def _require(self, role_id: uuid.UUID) -> Role:
        role = await self._roles.get(role_id)
        if role is None:
            raise AppError(
                code="NOT_FOUND",
                message="Role not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return role

    def _raise_version_conflict(self) -> None:
        raise AppError(
            code="RESOURCE_VERSION_CONFLICT",
            message="Role lock_version does not match.",
            status_code=status.HTTP_409_CONFLICT,
        )

    async def create(self, data: RoleCreate) -> Role:
        if await self._roles.get_by_code(data.code) is not None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="role code is already in use.",
                status_code=status.HTTP_409_CONFLICT,
            )
        try:
            role = await self._roles.create(
                code=data.code, name=data.name, description=data.description
            )
            await self._session.commit()
            await self._session.refresh(role)
            return role
        except IntegrityError as exc:
            await self._session.rollback()
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="role code is already in use.",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc

    async def get(self, role_id: uuid.UUID) -> Role:
        return await self._require(role_id)

    async def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        q: str | None = None,
        sort: str = "-updated_at",
    ) -> tuple[list[Role], int]:
        return await self._roles.list(page=page, page_size=page_size, q=q, sort=sort)

    async def update(
        self,
        role_id: uuid.UUID,
        data: RoleUpdate,
        *,
        expected_lock_version: int,
    ) -> Role:
        role = await self._roles.lock_for_update(role_id)
        if role is None:
            raise AppError(
                code="NOT_FOUND",
                message="Role not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if role.lock_version != expected_lock_version:
            await self._session.rollback()
            self._raise_version_conflict()

        payload = data.model_dump(exclude_unset=True, exclude={"lock_version"})
        if not payload:
            await self._session.commit()
            return role

        updated = await self._roles.update_atomic(
            role_id,
            expected_lock_version=expected_lock_version,
            **payload,
        )
        if updated is None:
            await self._session.rollback()
            current = await self._roles.get(role_id)
            if current is None:
                raise AppError(
                    code="NOT_FOUND",
                    message="Role not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            self._raise_version_conflict()
        await self._session.commit()
        await self._session.refresh(updated)
        return updated

    async def list_permissions(self, role_id: uuid.UUID) -> list[Permission]:
        await self._require(role_id)
        return await self._role_permissions.list_permissions(role_id)

    async def replace_permissions(
        self,
        role_id: uuid.UUID,
        data: RolePermissionReplaceRequest,
        *,
        expected_lock_version: int,
    ) -> list[Permission]:
        role = await self._roles.lock_for_update(role_id)
        if role is None:
            raise AppError(
                code="NOT_FOUND",
                message="Role not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if role.lock_version != expected_lock_version:
            await self._session.rollback()
            self._raise_version_conflict()

        unique_ids = list(dict.fromkeys(data.permission_ids))
        if len(unique_ids) != len(data.permission_ids):
            raise AppError(
                code="VALIDATION_ERROR",
                message="permission_ids must not contain duplicates.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        found = await self._permissions.list_by_ids(unique_ids)
        if len(found) != len(unique_ids):
            await self._session.rollback()
            raise AppError(
                code="VALIDATION_ERROR",
                message="One or more permission_ids do not exist.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        current_ids = set(await self._role_permissions.list_permission_ids(role_id))
        desired_ids = set(unique_ids)
        if current_ids == desired_ids:
            await self._session.commit()
            return await self._role_permissions.list_permissions(role_id)

        await self._role_permissions.replace_all(role_id, unique_ids)
        bumped = await self._roles.bump_lock_version(
            role_id, expected_lock_version=expected_lock_version
        )
        if bumped is None:
            await self._session.rollback()
            self._raise_version_conflict()
        await self._session.commit()
        return await self._role_permissions.list_permissions(role_id)


class PermissionService:
    def __init__(self, session: AsyncSession) -> None:
        self._permissions = PermissionRepository(session)

    async def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        q: str | None = None,
        sort: str = "code",
    ) -> tuple[list[Permission], int]:
        return await self._permissions.list(
            page=page, page_size=page_size, q=q, sort=sort
        )
