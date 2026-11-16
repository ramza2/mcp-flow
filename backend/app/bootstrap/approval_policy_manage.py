"""ADMIN Role grant helpers for ``approval.policy.manage``.

Procedure (docs/06 §6 / §13):

1. Ensure permission catalog contains ``approval.policy.manage``
   (Alembic ``20261116_0039`` or ``seed_bootstrap_permissions``).
2. Ensure a live Role with code ``ADMIN`` exists (operators create it —
   helpers never invent the Role).
3. Call ``grant_approval_policy_manage_to_admin_role`` (idempotent), or rely
   on migration 0039 which grants only when Role ``ADMIN`` already exists.
4. Call ``verify_admin_approval_policy_manage_grant``; when ADMIN is present,
   require ``granted is True``.

Hard rules:

* Never grant to all Roles or all Users.
* Never gate ApprovalPolicy admin API on ``approval.decide``.
* Non-ADMIN Roles keep existing memberships unless operators grant explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bootstrap.permissions import permission_seed_id
from app.models.auth import Permission, RolePermission
from app.repositories.role import PermissionRepository, RoleRepository

ADMIN_ROLE_CODE = "ADMIN"
APPROVAL_POLICY_MANAGE = "approval.policy.manage"


@dataclass(frozen=True, slots=True)
class AdminApprovalPolicyManageGrantStatus:
    permission_present: bool
    admin_role_present: bool
    granted: bool
    foreign_role_grants: int
    """Count of non-ADMIN Roles that hold the permission (explicit ops grants)."""


async def grant_approval_policy_manage_to_admin_role(
    session: AsyncSession,
) -> AdminApprovalPolicyManageGrantStatus:
    """Idempotently grant ``approval.policy.manage`` to live Role ``ADMIN`` only.

    Does not create the ADMIN Role. Does not grant to other Roles or Users.
    """
    perms = PermissionRepository(session)
    roles = RoleRepository(session)

    permission = await perms.get_by_code(APPROVAL_POLICY_MANAGE)
    if permission is None:
        session.add(
            Permission(
                id=permission_seed_id(APPROVAL_POLICY_MANAGE),
                code=APPROVAL_POLICY_MANAGE,
                name="Approval Policy Manage",
                description=(
                    "Create/read/update/activate/deactivate ApprovalPolicy definitions."
                ),
            )
        )
        await session.flush()
        permission = await perms.get_by_code(APPROVAL_POLICY_MANAGE)
        assert permission is not None

    admin = await roles.get_by_code(ADMIN_ROLE_CODE)
    if admin is None:
        return await verify_admin_approval_policy_manage_grant(session)

    existing = await session.execute(
        select(RolePermission).where(
            RolePermission.role_id == admin.id,
            RolePermission.permission_id == permission.id,
        )
    )
    if existing.scalar_one_or_none() is None:
        session.add(RolePermission(role_id=admin.id, permission_id=permission.id))
        await session.flush()

    return await verify_admin_approval_policy_manage_grant(session)


async def verify_admin_approval_policy_manage_grant(
    session: AsyncSession,
) -> AdminApprovalPolicyManageGrantStatus:
    """Read-only verification of the ADMIN grant procedure."""
    permission = await PermissionRepository(session).get_by_code(
        APPROVAL_POLICY_MANAGE
    )
    admin = await RoleRepository(session).get_by_code(ADMIN_ROLE_CODE)

    granted = False
    foreign = 0
    if permission is not None:
        rows = list(
            (
                await session.execute(
                    select(RolePermission.role_id).where(
                        RolePermission.permission_id == permission.id
                    )
                )
            )
            .scalars()
            .all()
        )
        admin_id = admin.id if admin is not None else None
        for role_id in rows:
            if admin_id is not None and role_id == admin_id:
                granted = True
            else:
                foreign += 1

    return AdminApprovalPolicyManageGrantStatus(
        permission_present=permission is not None,
        admin_role_present=admin is not None,
        granted=granted,
        foreign_role_grants=foreign,
    )
