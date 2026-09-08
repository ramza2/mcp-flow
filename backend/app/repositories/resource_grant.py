"""ResourceGrant repository (docs/05 resource_grants)."""

from __future__ import annotations

import uuid

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import ResourceGrant, UserRole


class ResourceGrantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: uuid.UUID | None = None,
        role_id: uuid.UUID | None = None,
        resource_type: str,
        resource_id: uuid.UUID,
        created_by: uuid.UUID | None = None,
    ) -> ResourceGrant:
        grant = ResourceGrant(
            user_id=user_id,
            role_id=role_id,
            resource_type=resource_type,
            resource_id=resource_id,
            created_by=created_by,
        )
        self._session.add(grant)
        await self._session.flush()
        await self._session.refresh(grant)
        return grant

    async def get(self, grant_id: uuid.UUID) -> ResourceGrant | None:
        result = await self._session.execute(
            select(ResourceGrant).where(ResourceGrant.id == grant_id)
        )
        return result.scalar_one_or_none()

    async def get_for_user(
        self, user_id: uuid.UUID, grant_id: uuid.UUID
    ) -> ResourceGrant | None:
        result = await self._session.execute(
            select(ResourceGrant).where(
                ResourceGrant.id == grant_id,
                ResourceGrant.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_for_role(
        self, role_id: uuid.UUID, grant_id: uuid.UUID
    ) -> ResourceGrant | None:
        result = await self._session.execute(
            select(ResourceGrant).where(
                ResourceGrant.id == grant_id,
                ResourceGrant.role_id == role_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_user(
        self,
        user_id: uuid.UUID,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[ResourceGrant], int]:
        return await self._list(
            select(ResourceGrant).where(ResourceGrant.user_id == user_id),
            page=page,
            page_size=page_size,
        )

    async def list_for_role(
        self,
        role_id: uuid.UUID,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[ResourceGrant], int]:
        return await self._list(
            select(ResourceGrant).where(ResourceGrant.role_id == role_id),
            page=page,
            page_size=page_size,
        )

    async def _list(
        self,
        stmt: Select[tuple[ResourceGrant]],
        *,
        page: int,
        page_size: int,
    ) -> tuple[list[ResourceGrant], int]:
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())
        rows = list(
            (
                await self._session.execute(
                    stmt.order_by(ResourceGrant.created_at.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        return rows, total

    async def delete(self, grant: ResourceGrant) -> None:
        await self._session.delete(grant)
        await self._session.flush()

    async def has_effective_grant(
        self,
        user_id: uuid.UUID,
        *,
        resource_type: str,
        resource_id: uuid.UUID,
    ) -> bool:
        """Direct User grant OR any Role grant for the user's current roles."""

        role_ids_subq = select(UserRole.role_id).where(UserRole.user_id == user_id)
        stmt = select(func.count()).select_from(ResourceGrant).where(
            ResourceGrant.resource_type == resource_type,
            ResourceGrant.resource_id == resource_id,
            or_(
                ResourceGrant.user_id == user_id,
                and_(
                    ResourceGrant.role_id.is_not(None),
                    ResourceGrant.role_id.in_(role_ids_subq),
                ),
            ),
        )
        return int((await self._session.execute(stmt)).scalar_one()) > 0
