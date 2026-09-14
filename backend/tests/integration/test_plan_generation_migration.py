"""Alembic round-trip for plan_generation_runs / plan_generation_tool_refs."""

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
def test_alembic_plan_generation_downgrade_upgrade(
    integration_database_url: str,
) -> None:
    cfg = _cfg(integration_database_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "20260914_0010")
    command.upgrade(cfg, "head")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_plan_generation_schema_constraints(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        tables = (
            await session.execute(
                text(
                    """
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name IN (
                        'plan_generation_runs',
                        'plan_generation_tool_refs'
                      )
                    ORDER BY table_name
                    """
                )
            )
        ).scalars().all()
        assert tables == ["plan_generation_runs", "plan_generation_tool_refs"]

        run_fks = (
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
                      AND tc.table_name = 'plan_generation_runs'
                    ORDER BY kcu.column_name
                    """
                )
            )
        ).all()
        run_fk_map = {row.column_name: row.foreign_table_name for row in run_fks}
        assert run_fk_map["agent_request_id"] == "agent_requests"
        assert run_fk_map["parameter_build_run_id"] == "parameter_build_runs"
        assert run_fk_map["agent_version_id"] == "agent_versions"

        ref_fks = (
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
                      AND tc.table_name = 'plan_generation_tool_refs'
                    ORDER BY kcu.column_name
                    """
                )
            )
        ).all()
        ref_fk_map = {row.column_name: row.foreign_table_name for row in ref_fks}
        assert ref_fk_map["plan_generation_run_id"] == "plan_generation_runs"
        assert ref_fk_map["mcp_tool_version_id"] == "mcp_tool_versions"

        run_checks = (
            await session.execute(
                text(
                    """
                    SELECT pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conrelid = 'plan_generation_runs'::regclass
                      AND contype = 'c'
                    """
                )
            )
        ).scalars().all()
        run_defs = " ".join(run_checks).lower()
        assert "plan_schema_version" in run_defs
        assert "'1.0'" in run_defs
        # plan_hash must be lowercase SHA-256 hex (not length-only).
        hash_checks = [c for c in run_checks if "plan_hash" in c.lower()]
        assert hash_checks, "expected a CHECK involving plan_hash"
        hash_defs_raw = " ".join(hash_checks)
        hash_defs = hash_defs_raw.lower()
        assert "~" in hash_defs or "similar to" in hash_defs
        assert "0-9a-f" in hash_defs
        assert "{64}" in hash_defs or "64" in hash_defs
        assert "A-F" not in hash_defs_raw  # uppercase hex class must not be allowed
        assert "jsonb_typeof(plan_snapshot)" in run_defs
        assert "jsonb_typeof(planning_settings_snapshot)" in run_defs

        hash_names = (
            await session.execute(
                text(
                    """
                    SELECT conname
                    FROM pg_constraint
                    WHERE conrelid = 'plan_generation_runs'::regclass
                      AND contype = 'c'
                      AND pg_get_constraintdef(oid) ILIKE '%plan_hash%'
                    """
                )
            )
        ).scalars().all()
        assert any("plan_hash" in name for name in hash_names)

        ref_checks = (
            await session.execute(
                text(
                    """
                    SELECT pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conrelid = 'plan_generation_tool_refs'::regclass
                      AND contype = 'c'
                    """
                )
            )
        ).scalars().all()
        ref_defs = " ".join(ref_checks).lower()
        assert "btrim((step_key)::text)" in ref_defs

        run_indexes = (
            await session.execute(
                text(
                    """
                    SELECT indexname FROM pg_indexes
                    WHERE tablename = 'plan_generation_runs'
                    ORDER BY indexname
                    """
                )
            )
        ).scalars().all()
        assert any("agent_request_id_created_at" in name for name in run_indexes)
        assert any("parameter_build_run_id" in name for name in run_indexes)
        assert any("agent_version_id" in name for name in run_indexes)

        ref_indexes = (
            await session.execute(
                text(
                    """
                    SELECT indexname FROM pg_indexes
                    WHERE tablename = 'plan_generation_tool_refs'
                    ORDER BY indexname
                    """
                )
            )
        ).scalars().all()
        assert any("mcp_tool_version_id" in name for name in ref_indexes)
