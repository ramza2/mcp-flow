"""Authorization snapshot repository — single-statement security decisions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import UserStatus
from app.models.auth import (
    Permission,
    ResourceGrant,
    Role,
    RolePermission,
    User,
    UserRole,
)


@dataclass(frozen=True, slots=True)
class AuthorizationSnapshot:
    """Transient authorization view — not a persisted Domain entity."""

    user_exists: bool
    user_active: bool
    permission_present: bool
    resource_grant_present: bool


class AuthorizationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_resource_authorization_snapshot(
        self,
        user_id: uuid.UUID,
        *,
        permission_code: str,
        resource_type: str,
        resource_id: uuid.UUID,
    ) -> AuthorizationSnapshot:
        """Evaluate user / permission / grant flags in one SQL statement."""

        live_user = and_(User.id == user_id, User.deleted_at.is_(None))
        user_exists_sq = exists(select(1).select_from(User).where(live_user))
        user_active_sq = exists(
            select(1)
            .select_from(User)
            .where(live_user, User.status == UserStatus.ACTIVE)
        )

        permission_present_sq = exists(
            select(1)
            .select_from(User)
            .join(UserRole, UserRole.user_id == User.id)
            .join(Role, Role.id == UserRole.role_id)
            .join(RolePermission, RolePermission.role_id == Role.id)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(
                live_user,
                User.status == UserStatus.ACTIVE,
                Role.deleted_at.is_(None),
                Permission.code == permission_code,
            )
        )

        live_role_grant = and_(
            ResourceGrant.role_id.is_not(None),
            exists(
                select(1)
                .select_from(UserRole)
                .join(Role, Role.id == UserRole.role_id)
                .where(
                    UserRole.user_id == user_id,
                    UserRole.role_id == ResourceGrant.role_id,
                    Role.deleted_at.is_(None),
                )
            ),
        )
        resource_grant_present_sq = exists(
            select(1)
            .select_from(ResourceGrant)
            .where(
                ResourceGrant.resource_type == resource_type,
                ResourceGrant.resource_id == resource_id,
                or_(ResourceGrant.user_id == user_id, live_role_grant),
            )
        )

        stmt = select(
            user_exists_sq.label("user_exists"),
            user_active_sq.label("user_active"),
            permission_present_sq.label("permission_present"),
            resource_grant_present_sq.label("resource_grant_present"),
        )
        row = (await self._session.execute(stmt)).one()
        return AuthorizationSnapshot(
            user_exists=bool(row.user_exists),
            user_active=bool(row.user_active),
            permission_present=bool(row.permission_present),
            resource_grant_present=bool(row.resource_grant_present),
        )
