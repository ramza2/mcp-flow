"""Alembic round-trip for parameter_build_runs."""

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
def test_alembic_parameter_build_runs_downgrade_upgrade(
    integration_database_url: str,
) -> None:
    cfg = _cfg(integration_database_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "20260914_0009")
    command.upgrade(cfg, "head")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_parameter_build_runs_schema_constraints(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        tables = (
            await session.execute(
                text(
                    """
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name = 'parameter_build_runs'
                    """
                )
            )
        ).scalars().all()
        assert tables == ["parameter_build_runs"]

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
                      AND tc.table_name = 'parameter_build_runs'
                    ORDER BY kcu.column_name
                    """
                )
            )
        ).all()
        fk_map = {row.column_name: row.foreign_table_name for row in fks}
        assert fk_map["agent_request_id"] == "agent_requests"
        assert fk_map["tool_selection_run_id"] == "tool_selection_runs"
        assert fk_map["tool_version_id"] == "mcp_tool_versions"

        checks = (
            await session.execute(
                text(
                    """
                    SELECT pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conrelid = 'parameter_build_runs'::regclass
                      AND contype = 'c'
                    """
                )
            )
        ).scalars().all()
        defs = " ".join(checks).lower()
        assert "jsonb_typeof(missing_fields)" in defs
        assert "jsonb_array_length(missing_fields) = 0" in defs
        assert "not is_complete" in defs

        indexes = (
            await session.execute(
                text(
                    """
                    SELECT indexname FROM pg_indexes
                    WHERE tablename = 'parameter_build_runs'
                    ORDER BY indexname
                    """
                )
            )
        ).scalars().all()
        assert any("agent_request_id_created_at" in name for name in indexes)
        assert any("tool_selection_run_id" in name for name in indexes)
        assert any("tool_version_id" in name for name in indexes)
