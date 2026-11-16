"""PostgreSQL Alembic 0039: approval.policy.manage + ADMIN grant + data preserve."""

from __future__ import annotations

import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


def _alembic_cfg(url: str) -> Config:
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    cfg = Config(os.path.join(backend_dir, "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    os.environ["MCPFLOW_DATABASE_URL"] = url
    from app.core.config import get_settings

    get_settings.cache_clear()
    return cfg


def _sync_url(async_url: str) -> str:
    if async_url.startswith("postgresql+asyncpg://"):
        return async_url.replace(
            "postgresql+asyncpg://", "postgresql+psycopg://", 1
        )
    if async_url.startswith("postgresql://"):
        return async_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return async_url


@pytest.mark.integration
def test_alembic_0039_admin_grant_preserves_existing_data(
    integration_database_url: str,
) -> None:
    """0038→seed→0039: permission + ADMIN-only grant; ApprovalPolicy rows preserved."""
    sync_url = _sync_url(integration_database_url)
    cfg = _alembic_cfg(integration_database_url)

    # Isolation harness leaves DB at head; walk back to parent of 0039.
    command.downgrade(cfg, "20261028_0038")

    engine = create_engine(sync_url)
    Session = sessionmaker(bind=engine)
    admin_role_id = uuid.uuid5(uuid.NAMESPACE_URL, "mcpflow:test:admin-role")
    other_role_id = uuid.uuid5(uuid.NAMESPACE_URL, "mcpflow:test:other-role")
    policy_id = uuid.uuid5(uuid.NAMESPACE_URL, "mcpflow:test:approval-policy")
    policy_code = f"pre-0039-{uuid.uuid4().hex[:8]}"
    other_code = f"OTHER-{uuid.uuid4().hex[:6]}"

    with Session() as session:
        assert (
            session.execute(
                text(
                    "SELECT count(*) FROM permissions "
                    "WHERE code = 'approval.policy.manage'"
                )
            ).scalar_one()
            == 0
        )

        session.execute(
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
        session.execute(
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
        session.commit()

    command.upgrade(cfg, "20261116_0039")

    with Session() as session:
        perm = session.execute(
            text(
                "SELECT id, code, name FROM permissions "
                "WHERE code = 'approval.policy.manage'"
            )
        ).one()
        assert perm.code == "approval.policy.manage"

        admin_grant = session.execute(
            text(
                """
                SELECT count(*) FROM role_permissions rp
                JOIN roles r ON r.id = rp.role_id
                JOIN permissions p ON p.id = rp.permission_id
                WHERE r.code = 'ADMIN'
                  AND p.code = 'approval.policy.manage'
                """
            )
        ).scalar_one()
        assert admin_grant == 1

        other_grant = session.execute(
            text(
                """
                SELECT count(*) FROM role_permissions rp
                WHERE rp.role_id = :other_id
                  AND rp.permission_id = :perm_id
                """
            ),
            {"other_id": other_role_id, "perm_id": perm.id},
        ).scalar_one()
        assert other_grant == 0

        total_grants = session.execute(
            text(
                """
                SELECT count(*) FROM role_permissions rp
                JOIN permissions p ON p.id = rp.permission_id
                WHERE p.code = 'approval.policy.manage'
                """
            )
        ).scalar_one()
        assert total_grants == 1

        preserved = session.execute(
            text(
                "SELECT code, status, name FROM approval_policies WHERE id = :id"
            ),
            {"id": policy_id},
        ).one()
        assert preserved.code == policy_code
        assert preserved.status == "ACTIVE"
        assert preserved.name == "Pre Migration Policy"

        # Migration never touches user_roles.
        assert (
            session.execute(text("SELECT count(*) FROM user_roles")).scalar_one()
            == 0
        )

    command.downgrade(cfg, "20261028_0038")
    with Session() as session:
        assert (
            session.execute(
                text(
                    "SELECT count(*) FROM permissions "
                    "WHERE code = 'approval.policy.manage'"
                )
            ).scalar_one()
            == 0
        )
        assert (
            session.execute(
                text("SELECT count(*) FROM approval_policies WHERE id = :id"),
                {"id": policy_id},
            ).scalar_one()
            == 1
        )

    command.upgrade(cfg, "head")
    with Session() as session:
        assert (
            session.execute(
                text(
                    "SELECT count(*) FROM permissions "
                    "WHERE code = 'approval.policy.manage'"
                )
            ).scalar_one()
            == 1
        )
        assert (
            session.execute(
                text(
                    """
                    SELECT count(*) FROM role_permissions rp
                    JOIN roles r ON r.id = rp.role_id
                    JOIN permissions p ON p.id = rp.permission_id
                    WHERE r.code = 'ADMIN'
                      AND p.code = 'approval.policy.manage'
                    """
                )
            ).scalar_one()
            == 1
        )
        assert (
            session.execute(
                text("SELECT count(*) FROM approval_policies WHERE id = :id"),
                {"id": policy_id},
            ).scalar_one()
            == 1
        )

    engine.dispose()
