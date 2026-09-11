"""Authorization snapshot repository — single-statement security decisions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, and_, exists, or_, select
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


def build_live_user_predicate(user_id: uuid.UUID) -> ColumnElement[bool]:
    """User row is present and not soft-deleted."""
    return and_(User.id == user_id, User.deleted_at.is_(None))


def build_effective_permission_exists(
    user_id: uuid.UUID,
    permission_code: str,
) -> ColumnElement[bool]:
    """Live ACTIVE user has permission via a live (non-deleted) Role."""
    live_user = build_live_user_predicate(user_id)
    return exists(
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


def build_effective_resource_grant_exists(
    user_id: uuid.UUID,
    *,
    resource_type: str,
    resource_id: uuid.UUID | ColumnElement[Any],
) -> ColumnElement[bool]:
    """Exact ResourceGrant via direct user grant or live Role grant.

    ``MCP_SERVER`` grants never imply ``MCP_TOOL`` — callers must pass the
    exact resource_type / resource_id pair to evaluate.
    """
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
    return exists(
        select(1)
        .select_from(ResourceGrant)
        .where(
            ResourceGrant.resource_type == resource_type,
            ResourceGrant.resource_id == resource_id,
            or_(ResourceGrant.user_id == user_id, live_role_grant),
        )
    )


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

        live_user = build_live_user_predicate(user_id)
        user_exists_sq = exists(select(1).select_from(User).where(live_user))
        user_active_sq = exists(
            select(1)
            .select_from(User)
            .where(live_user, User.status == UserStatus.ACTIVE)
        )
        permission_present_sq = build_effective_permission_exists(
            user_id, permission_code
        )
        resource_grant_present_sq = build_effective_resource_grant_exists(
            user_id,
            resource_type=resource_type,
            resource_id=resource_id,
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
