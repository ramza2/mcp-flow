"""Alembic round-trip for plan_validation_runs."""

from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _cfg(url: str) -> Config:
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    cfg = Config(os.path.join(backend_dir, "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    os.environ["MCPFLOW_DATABASE_URL"] = url
    from app.core.config import get_settings

    get_settings.cache_clear()
    return cfg


@pytest.mark.integration
def test_alembic_plan_validation_downgrade_upgrade(
    integration_database_url: str,
) -> None:
    cfg = _cfg(integration_database_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "20260914_0011")
    command.upgrade(cfg, "head")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_plan_validation_schema_constraints(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        tables = (
            await session.execute(
                text(
                    """
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name = 'plan_validation_runs'
                    """
                )
            )
        ).scalars().all()
        assert tables == ["plan_validation_runs"]

        fks = (
            await session.execute(
                text(
                    """
                    SELECT
                      kcu.column_name,
                      ccu.table_name AS foreign_table_name
                    FROM information_schema.table_constraints AS tc
                    JOIN information_schema.key_column_usage AS kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                    JOIN information_schema.constraint_column_usage AS ccu
                      ON ccu.constraint_name = tc.constraint_name
                     AND ccu.table_schema = tc.table_schema
                    WHERE tc.constraint_type = 'FOREIGN KEY'
                      AND tc.table_name = 'plan_validation_runs'
                    ORDER BY kcu.column_name
                    """
                )
            )
        ).all()
        fk_map = {row.column_name: row.foreign_table_name for row in fks}
        assert fk_map["agent_request_id"] == "agent_requests"
        assert fk_map["plan_generation_run_id"] == "plan_generation_runs"
        assert fk_map["clarification_request_id"] == "clarification_requests"

        checks = (
            await session.execute(
                text(
                    """
                    SELECT pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conrelid = 'plan_validation_runs'::regclass
                      AND contype = 'c'
                    """
                )
            )
        ).scalars().all()
        defs = " ".join(checks).lower()
        assert "validator_version" in defs
        assert "'1.0'" in defs
        assert "ready" in defs
        assert "waiting_confirmation" in defs
        assert "rejected" in defs
        assert "failed" in defs
        hash_checks = [c for c in checks if "plan_hash" in c.lower()]
        assert hash_checks
        hash_defs = " ".join(hash_checks).lower()
        assert "0-9a-f" in hash_defs
        assert "jsonb_typeof(errors)" in defs
        assert "jsonb_typeof(warnings)" in defs
        assert "jsonb_typeof(checks_snapshot)" in defs
        assert "jsonb_typeof(policy_snapshot)" in defs
        assert "confirmation_required" in defs
        assert "clarification_request_id" in defs

        indexes = (
            await session.execute(
                text(
                    """
                    SELECT indexname FROM pg_indexes
                    WHERE tablename = 'plan_validation_runs'
                    ORDER BY indexname
                    """
                )
            )
        ).scalars().all()
        assert any("agent_request_id_created_at" in name for name in indexes)
        assert any("plan_generation_run_id" in name for name in indexes)
        assert any("clarification_request_id" in name for name in indexes)
