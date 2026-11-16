"""PostgreSQL Alembic 0039: approval.policy.manage + ADMIN grant + data preserve."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool


def _alembic_cfg(url: str) -> Config:
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    cfg = Config(os.path.join(backend_dir, "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    os.environ["MCPFLOW_DATABASE_URL"] = url
    from app.core.config import get_settings

    get_settings.cache_clear()
    return cfg


def _run_async(coro_factory: Callable[[], Awaitable[Any]]) -> Any:
    """Run async DB work off the pytest-asyncio loop (sync Alembic tests)."""

    def _runner() -> Any:
        return asyncio.run(coro_factory())

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_runner).result()


async def _with_conn(
    url: str, work: Callable[[AsyncConnection], Awaitable[Any]]
) -> Any:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            return await work(conn)
    finally:
        await engine.dispose()


@pytest.mark.integration
def test_alembic_0039_admin_grant_preserves_existing_data(
    integration_database_url: str,
) -> None:
    """0038→seed→0039: permission + ADMIN-only grant; ApprovalPolicy rows preserved."""
    cfg = _alembic_cfg(integration_database_url)
    url = integration_database_url

    # Isolation harness leaves DB at head; walk back to parent of 0039.
    command.downgrade(cfg, "20261028_0038")

    admin_role_id = uuid.uuid5(uuid.NAMESPACE_URL, "mcpflow:test:admin-role")
    other_role_id = uuid.uuid5(uuid.NAMESPACE_URL, "mcpflow:test:other-role")
    policy_id = uuid.uuid5(uuid.NAMESPACE_URL, "mcpflow:test:approval-policy")
    policy_code = f"pre-0039-{uuid.uuid4().hex[:8]}"
    other_code = f"OTHER-{uuid.uuid4().hex[:6]}"

    async def _seed(conn: AsyncConnection) -> None:
        before_perm = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM permissions "
                    "WHERE code = 'approval.policy.manage'"
                )
            )
        ).scalar_one()
        assert before_perm == 0

        await conn.execute(
            text(
                """
                INSERT INTO roles (id, code, name, description, lock_version)
                VALUES
                  (:admin_id, 'ADMIN', 'Administrator', 'test admin', 1),
                  (:other_id, :other_code, 'Other Role', 'not admin', 1)
                """
            ),
            {
                "admin_id": admin_role_id,
                "other_id": other_role_id,
                "other_code": other_code,
            },
        )
        await conn.execute(
            text(
                """
                INSERT INTO approval_policies (
                  id, code, name, description, status, decision_mode,
                  required_approvals, approver_scope, default_expiry_seconds,
                  allow_self_approval, reject_comment_required, lock_version
                ) VALUES (
                  :id, :code, 'Pre Migration Policy', 'preserve me', 'ACTIVE',
                  'ANY', 1, NULL, 3600, false, false, 1
                )
                """
            ),
            {"id": policy_id, "code": policy_code},
        )

    _run_async(lambda: _with_conn(url, _seed))

    command.upgrade(cfg, "20261116_0039")

    async def _assert_after_upgrade(conn: AsyncConnection) -> None:
        perm = (
            await conn.execute(
                text(
                    "SELECT id, code, name FROM permissions "
                    "WHERE code = 'approval.policy.manage'"
                )
            )
        ).one()
        assert perm.code == "approval.policy.manage"

        admin_grant = (
            await conn.execute(
                text(
                    """
                    SELECT count(*) FROM role_permissions rp
                    JOIN roles r ON r.id = rp.role_id
                    JOIN permissions p ON p.id = rp.permission_id
                    WHERE r.code = 'ADMIN'
                      AND p.code = 'approval.policy.manage'
                    """
                )
            )
        ).scalar_one()
        assert admin_grant == 1

        other_grant = (
            await conn.execute(
                text(
                    """
                    SELECT count(*) FROM role_permissions rp
                    WHERE rp.role_id = :other_id
                      AND rp.permission_id = :perm_id
                    """
                ),
                {"other_id": other_role_id, "perm_id": perm.id},
            )
        ).scalar_one()
        assert other_grant == 0

        total_grants = (
            await conn.execute(
                text(
                    """
                    SELECT count(*) FROM role_permissions rp
                    JOIN permissions p ON p.id = rp.permission_id
                    WHERE p.code = 'approval.policy.manage'
                    """
                )
            )
        ).scalar_one()
        assert total_grants == 1

        preserved = (
            await conn.execute(
                text(
                    "SELECT code, status, name FROM approval_policies WHERE id = :id"
                ),
                {"id": policy_id},
            )
        ).one()
        assert preserved.code == policy_code
        assert preserved.status == "ACTIVE"
        assert preserved.name == "Pre Migration Policy"

        assert (
            await conn.execute(text("SELECT count(*) FROM user_roles"))
        ).scalar_one() == 0

    _run_async(lambda: _with_conn(url, _assert_after_upgrade))

    command.downgrade(cfg, "20261028_0038")

    async def _assert_after_downgrade(conn: AsyncConnection) -> None:
        assert (
            await conn.execute(
                text(
                    "SELECT count(*) FROM permissions "
                    "WHERE code = 'approval.policy.manage'"
                )
            )
        ).scalar_one() == 0
        assert (
            await conn.execute(
                text("SELECT count(*) FROM approval_policies WHERE id = :id"),
                {"id": policy_id},
            )
        ).scalar_one() == 1

    _run_async(lambda: _with_conn(url, _assert_after_downgrade))

    command.upgrade(cfg, "head")

    async def _assert_head(conn: AsyncConnection) -> None:
        assert (
            await conn.execute(
                text(
                    "SELECT count(*) FROM permissions "
                    "WHERE code = 'approval.policy.manage'"
                )
            )
        ).scalar_one() == 1
        assert (
            await conn.execute(
                text(
                    """
                    SELECT count(*) FROM role_permissions rp
                    JOIN roles r ON r.id = rp.role_id
                    JOIN permissions p ON p.id = rp.permission_id
                    WHERE r.code = 'ADMIN'
                      AND p.code = 'approval.policy.manage'
                    """
                )
            )
        ).scalar_one() == 1
        assert (
            await conn.execute(
                text("SELECT count(*) FROM approval_policies WHERE id = :id"),
                {"id": policy_id},
            )
        ).scalar_one() == 1

    _run_async(lambda: _with_conn(url, _assert_head))
