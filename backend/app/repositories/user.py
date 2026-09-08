"""User repository (docs/05 users)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import User
from app.schemas.common_page import parse_sort

ALLOWED_USER_SORT = {"updated_at", "created_at", "username", "display_name", "status"}


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _live(self) -> Select[tuple[User]]:
        return select(User).where(User.deleted_at.is_(None))

    async def create(
        self,
        *,
        username: str,
        display_name: str,
        email: str,
        status: str,
    ) -> User:
        user = User(
            username=username,
            display_name=display_name,
            email=email,
            status=status,
            password_hash=None,
        )
        self._session.add(user)
        await self._session.flush()
        await self._session.refresh(user)
        return user

    async def get(self, user_id: uuid.UUID) -> User | None:
        result = await self._session.execute(self._live().where(User.id == user_id))
        return result.scalar_one_or_none()

    async def get_by_username(self, username: str) -> User | None:
        result = await self._session.execute(
            self._live().where(User.username == username)
        )
        return result.scalar_one_or_none()

    async def lock_for_update(self, user_id: uuid.UUID) -> User | None:
        result = await self._session.execute(
            self._live().where(User.id == user_id).with_for_update()
        )
        return result.scalar_one_or_none()

    async def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: str | None = None,
        q: str | None = None,
        sort: str = "-updated_at",
    ) -> tuple[list[User], int]:
        field, direction = parse_sort(sort, allowed=ALLOWED_USER_SORT)
        stmt = self._live()
        if status:
            statuses = [part.strip() for part in status.split(",") if part.strip()]
            if len(statuses) == 1:
                stmt = stmt.where(User.status == statuses[0])
            elif statuses:
                stmt = stmt.where(User.status.in_(statuses))
        if q:
            pattern = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(
                    User.username.ilike(pattern),
                    User.display_name.ilike(pattern),
                    User.email.ilike(pattern),
                )
            )
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())
        sort_col = getattr(User, field)
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
        user_id: uuid.UUID,
        *,
        expected_lock_version: int,
        **fields: Any,
    ) -> User | None:
        values = {
            key: value
            for key, value in fields.items()
            if hasattr(User, key)
            and key
            not in {
                "id",
                "username",
                "password_hash",
                "last_login_at",
                "lock_version",
                "created_at",
            }
        }
        values["lock_version"] = User.lock_version + 1
        values["updated_at"] = func.now()
        stmt = (
            update(User)
            .where(
                User.id == user_id,
                User.lock_version == expected_lock_version,
                User.deleted_at.is_(None),
            )
            .values(**values)
            .returning(User)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def bump_lock_version(
        self, user_id: uuid.UUID, *, expected_lock_version: int
    ) -> User | None:
        return await self.update_atomic(
            user_id, expected_lock_version=expected_lock_version
        )
