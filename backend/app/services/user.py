"""User lifecycle and role membership service (docs/05–06)."""

from __future__ import annotations

import uuid

from fastapi import status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.auth import Role, User
from app.repositories.role import RoleRepository, UserRoleRepository
from app.repositories.user import UserRepository
from app.schemas.auth import UserCreate, UserRoleReplaceRequest, UserUpdate


class UserService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._users = UserRepository(session)
        self._roles = RoleRepository(session)
        self._user_roles = UserRoleRepository(session)

    async def _require(self, user_id: uuid.UUID) -> User:
        user = await self._users.get(user_id)
        if user is None:
            raise AppError(
                code="NOT_FOUND",
                message="User not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return user

    def _raise_version_conflict(self) -> None:
        raise AppError(
            code="RESOURCE_VERSION_CONFLICT",
            message="User lock_version does not match.",
            status_code=status.HTTP_409_CONFLICT,
        )

    async def create(self, data: UserCreate) -> User:
        if await self._users.get_by_username(data.username) is not None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="username is already in use.",
                status_code=status.HTTP_409_CONFLICT,
            )
        try:
            user = await self._users.create(
                username=data.username,
                display_name=data.display_name,
                email=data.email,
                status=str(data.status),
            )
            await self._session.commit()
            await self._session.refresh(user)
            return user
        except IntegrityError as exc:
            await self._session.rollback()
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="username is already in use.",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc

    async def get(self, user_id: uuid.UUID) -> User:
        return await self._require(user_id)

    async def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status_filter: str | None = None,
        q: str | None = None,
        sort: str = "-updated_at",
    ) -> tuple[list[User], int]:
        return await self._users.list(
            page=page,
            page_size=page_size,
            status=status_filter,
            q=q,
            sort=sort,
        )

    async def update(
        self,
        user_id: uuid.UUID,
        data: UserUpdate,
        *,
        expected_lock_version: int,
    ) -> User:
        user = await self._users.lock_for_update(user_id)
        if user is None:
            raise AppError(
                code="NOT_FOUND",
                message="User not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if user.lock_version != expected_lock_version:
            await self._session.rollback()
            self._raise_version_conflict()

        payload = data.model_dump(exclude_unset=True, exclude={"lock_version"})
        if "status" in payload and payload["status"] is not None:
            payload["status"] = str(payload["status"])
        if not payload:
            await self._session.commit()
            return user

        updated = await self._users.update_atomic(
            user_id,
            expected_lock_version=expected_lock_version,
            **payload,
        )
        if updated is None:
            await self._session.rollback()
            current = await self._users.get(user_id)
            if current is None:
                raise AppError(
                    code="NOT_FOUND",
                    message="User not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            self._raise_version_conflict()
        await self._session.commit()
        await self._session.refresh(updated)
        return updated

    async def list_roles(self, user_id: uuid.UUID) -> list[Role]:
        await self._require(user_id)
        return await self._user_roles.list_roles(user_id)

    async def replace_roles(
        self,
        user_id: uuid.UUID,
        data: UserRoleReplaceRequest,
        *,
        expected_lock_version: int,
    ) -> list[Role]:
        user = await self._users.lock_for_update(user_id)
        if user is None:
            raise AppError(
                code="NOT_FOUND",
                message="User not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if user.lock_version != expected_lock_version:
            await self._session.rollback()
            self._raise_version_conflict()

        unique_ids = list(dict.fromkeys(data.role_ids))
        if len(unique_ids) != len(data.role_ids):
            raise AppError(
                code="VALIDATION_ERROR",
                message="role_ids must not contain duplicates.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        for role_id in unique_ids:
            role = await self._roles.get(role_id)
            if role is None:
                await self._session.rollback()
                raise AppError(
                    code="VALIDATION_ERROR",
                    message=f"role_id {role_id} does not exist.",
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )

        current_ids = set(await self._user_roles.list_role_ids(user_id))
        desired_ids = set(unique_ids)
        if current_ids == desired_ids:
            await self._session.commit()
            return await self._user_roles.list_roles(user_id)

        await self._user_roles.replace_all(user_id, unique_ids)
        bumped = await self._users.bump_lock_version(
            user_id, expected_lock_version=expected_lock_version
        )
        if bumped is None:
            await self._session.rollback()
            self._raise_version_conflict()
        await self._session.commit()
        return await self._user_roles.list_roles(user_id)
