"""Role and Permission repositories (docs/05)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import Permission, Role, RolePermission, UserRole
from app.schemas.common_page import parse_sort

ALLOWED_ROLE_SORT = {"updated_at", "created_at", "code", "name"}
ALLOWED_PERMISSION_SORT = {"code", "name", "created_at"}


class RoleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _live(self) -> Select[tuple[Role]]:
        return select(Role).where(Role.deleted_at.is_(None))

    async def create(
        self, *, code: str, name: str, description: str | None = None
    ) -> Role:
        role = Role(code=code, name=name, description=description)
        self._session.add(role)
        await self._session.flush()
        await self._session.refresh(role)
        return role

    async def get(self, role_id: uuid.UUID) -> Role | None:
        result = await self._session.execute(self._live().where(Role.id == role_id))
        return result.scalar_one_or_none()

    async def get_by_code(self, code: str) -> Role | None:
        result = await self._session.execute(self._live().where(Role.code == code))
        return result.scalar_one_or_none()

    async def lock_for_update(self, role_id: uuid.UUID) -> Role | None:
        result = await self._session.execute(
            self._live().where(Role.id == role_id).with_for_update()
        )
        return result.scalar_one_or_none()

    async def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        q: str | None = None,
        sort: str = "-updated_at",
    ) -> tuple[list[Role], int]:
        field, direction = parse_sort(sort, allowed=ALLOWED_ROLE_SORT)
        stmt = self._live()
        if q:
            pattern = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(
                    Role.code.ilike(pattern),
                    Role.name.ilike(pattern),
                    Role.description.ilike(pattern),
                )
            )
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())
        sort_col = getattr(Role, field)
        order = sort_col.desc() if direction == "desc" else sort_col.asc()
        rows = list(
            (
                await self._session.execute(
                    stmt.order_by(order)
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        return rows, total

    async def update_atomic(
        self,
        role_id: uuid.UUID,
        *,
        expected_lock_version: int,
        **fields: Any,
    ) -> Role | None:
        values = {
            key: value
            for key, value in fields.items()
            if hasattr(Role, key)
            and key not in {"id", "code", "lock_version", "created_at"}
        }
        values["lock_version"] = Role.lock_version + 1
        values["updated_at"] = func.now()
        stmt = (
            update(Role)
            .where(
                Role.id == role_id,
                Role.lock_version == expected_lock_version,
                Role.deleted_at.is_(None),
            )
            .values(**values)
            .returning(Role)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def bump_lock_version(
        self, role_id: uuid.UUID, *, expected_lock_version: int
    ) -> Role | None:
        return await self.update_atomic(
            role_id, expected_lock_version=expected_lock_version
        )


class PermissionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, permission_id: uuid.UUID) -> Permission | None:
        result = await self._session.execute(
            select(Permission).where(Permission.id == permission_id)
        )
        return result.scalar_one_or_none()

    async def get_by_code(self, code: str) -> Permission | None:
        result = await self._session.execute(
            select(Permission).where(Permission.code == code)
        )
        return result.scalar_one_or_none()

    async def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        q: str | None = None,
        sort: str = "code",
    ) -> tuple[list[Permission], int]:
        field, direction = parse_sort(
            sort, allowed=ALLOWED_PERMISSION_SORT, default_field="code"
        )
        stmt = select(Permission)
        if q:
            pattern = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(
                    Permission.code.ilike(pattern),
                    Permission.name.ilike(pattern),
                    Permission.description.ilike(pattern),
                )
            )
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())
        sort_col = getattr(Permission, field)
        order = sort_col.desc() if direction == "desc" else sort_col.asc()
        rows = list(
            (
                await self._session.execute(
                    stmt.order_by(order)
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        return rows, total

    async def list_by_ids(self, permission_ids: list[uuid.UUID]) -> list[Permission]:
        if not permission_ids:
            return []
        result = await self._session.execute(
            select(Permission).where(Permission.id.in_(permission_ids))
        )
        return list(result.scalars().all())


class UserRoleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_role_ids(self, user_id: uuid.UUID) -> list[uuid.UUID]:
        result = await self._session.execute(
            select(UserRole.role_id)
            .join(Role, Role.id == UserRole.role_id)
            .where(UserRole.user_id == user_id, Role.deleted_at.is_(None))
        )
        return list(result.scalars().all())

    async def list_roles(self, user_id: uuid.UUID) -> list[Role]:
        stmt = (
            select(Role)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user_id, Role.deleted_at.is_(None))
            .order_by(Role.code.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def replace_all(self, user_id: uuid.UUID, role_ids: list[uuid.UUID]) -> None:
        await self._session.execute(delete(UserRole).where(UserRole.user_id == user_id))
        for role_id in role_ids:
            self._session.add(UserRole(user_id=user_id, role_id=role_id))
        await self._session.flush()


class RolePermissionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_permission_ids(self, role_id: uuid.UUID) -> list[uuid.UUID]:
        result = await self._session.execute(
            select(RolePermission.permission_id).where(
                RolePermission.role_id == role_id
            )
        )
        return list(result.scalars().all())

    async def list_permissions(self, role_id: uuid.UUID) -> list[Permission]:
        stmt = (
            select(Permission)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(RolePermission.role_id == role_id)
            .order_by(Permission.code.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def replace_all(
        self, role_id: uuid.UUID, permission_ids: list[uuid.UUID]
    ) -> None:
        await self._session.execute(
            delete(RolePermission).where(RolePermission.role_id == role_id)
        )
        for permission_id in permission_ids:
            self._session.add(
                RolePermission(role_id=role_id, permission_id=permission_id)
            )
        await self._session.flush()
