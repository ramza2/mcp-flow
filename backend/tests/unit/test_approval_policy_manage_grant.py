"""ADMIN-only grant procedure for approval.policy.manage."""

from __future__ import annotations

import uuid

import pytest
from app.bootstrap.approval_policy_manage import (
    ADMIN_ROLE_CODE,
    APPROVAL_POLICY_MANAGE,
    grant_approval_policy_manage_to_admin_role,
    verify_admin_approval_policy_manage_grant,
)
from app.bootstrap.permissions import seed_bootstrap_permissions
from app.models.auth import RolePermission
from app.repositories.role import (
    PermissionRepository,
    RolePermissionRepository,
    RoleRepository,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_grant_targets_admin_only_never_all_roles(
    db_session: AsyncSession,
) -> None:
    await seed_bootstrap_permissions(db_session)

    other = await RoleRepository(db_session).create(
        code=f"OPS-{uuid.uuid4().hex[:6]}",
        name="Ops",
        description=None,
    )
    admin = await RoleRepository(db_session).create(
        code=ADMIN_ROLE_CODE,
        name="Administrator",
        description="Platform admin",
    )
    await db_session.commit()

    before = await verify_admin_approval_policy_manage_grant(db_session)
    assert before.permission_present is True
    assert before.admin_role_present is True
    assert before.granted is False

    status = await grant_approval_policy_manage_to_admin_role(db_session)
    await db_session.commit()

    assert status.granted is True
    assert status.admin_role_present is True
    assert status.permission_present is True

    manage = await PermissionRepository(db_session).get_by_code(APPROVAL_POLICY_MANAGE)
    assert manage is not None
    admin_perms = await RolePermissionRepository(db_session).list_permission_ids(
        admin.id
    )
    other_perms = await RolePermissionRepository(db_session).list_permission_ids(
        other.id
    )
    assert manage.id in admin_perms
    assert manage.id not in other_perms

    again = await grant_approval_policy_manage_to_admin_role(db_session)
    await db_session.commit()
    assert again.granted is True
    assert again.foreign_role_grants == 0


@pytest.mark.asyncio
async def test_grant_without_admin_role_is_noop(
    db_session: AsyncSession,
) -> None:
    await seed_bootstrap_permissions(db_session)
    await RoleRepository(db_session).create(
        code=f"NOT-ADMIN-{uuid.uuid4().hex[:6]}",
        name="Other",
        description=None,
    )
    await db_session.commit()

    status = await grant_approval_policy_manage_to_admin_role(db_session)
    await db_session.commit()
    assert status.admin_role_present is False
    assert status.granted is False

    manage = await PermissionRepository(db_session).get_by_code(APPROVAL_POLICY_MANAGE)
    assert manage is not None
    holders = list(
        (
            await db_session.execute(
                select(RolePermission.role_id).where(
                    RolePermission.permission_id == manage.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert holders == []
