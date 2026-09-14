"""Alembic round-trip for tool selection + clarification tables."""

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
def test_alembic_tool_selection_clarification_downgrade_upgrade(
    integration_database_url: str,
) -> None:
    cfg = _cfg(integration_database_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "20260911_0008")
    command.upgrade(cfg, "head")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tool_selection_clarification_schema_constraints(
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
                        'tool_selection_runs',
                        'tool_selection_candidates',
                        'clarification_requests'
                      )
                    ORDER BY table_name
                    """
                )
            )
        ).scalars().all()
        assert tables == [
            "clarification_requests",
            "tool_selection_candidates",
            "tool_selection_runs",
        ]
        checks = (
            await session.execute(
                text(
                    """
                    SELECT conname FROM pg_constraint
                    WHERE conrelid = 'tool_selection_runs'::regclass
                      AND contype = 'c'
                    ORDER BY conname
                    """
                )
            )
        ).scalars().all()
        assert any("decision" in name for name in checks)
        uniques = (
            await session.execute(
                text(
                    """
                    SELECT conname FROM pg_constraint
                    WHERE conrelid = 'tool_selection_candidates'::regclass
                      AND contype = 'u'
                    ORDER BY conname
                    """
                )
            )
        ).scalars().all()
        assert uniques
        fks = (
            await session.execute(
                text(
                    """
                    SELECT conname FROM pg_constraint
                    WHERE conrelid = 'tool_selection_runs'::regclass
                      AND contype = 'f'
                    ORDER BY conname
                    """
                )
            )
        ).scalars().all()
        assert any("selected_tool_version" in name for name in fks)
