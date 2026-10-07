"""Regressions for shared-PG integration isolation + migration recovery."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.conftest import (
    current_alembic_revision_sync,
    recover_integration_database,
    truncate_application_tables_sync,
)


def _cfg(url: str):
    from alembic.config import Config
    from app.core.config import get_settings

    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    cfg = Config(os.path.join(backend_dir, "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    os.environ["MCPFLOW_DATABASE_URL"] = url
    get_settings.cache_clear()
    return cfg


def _head_revision(url: str) -> str:
    cfg = _cfg(url)
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1, heads
    return heads[0]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_isolation_cleanup_removes_committed_rows(
    integration_session_factory: async_sessionmaker[AsyncSession],
    integration_database_url: str,
) -> None:
    """Committed application rows are gone after the isolation cleanup helper."""
    marker = f"iso-{uuid.uuid4().hex[:12]}"
    async with integration_session_factory() as session:
        await session.execute(
            text(
                """
                INSERT INTO users (
                    id, username, display_name, email, status,
                    created_at, updated_at, lock_version
                ) VALUES (
                    :id, :username, 'Isolation', :email, 'ACTIVE',
                    :now, :now, 1
                )
                """
            ),
            {
                "id": uuid.uuid4(),
                "username": marker,
                "email": f"{marker}@example.com",
                "now": datetime.now(UTC),
            },
        )
        await session.commit()

    async with integration_session_factory() as session:
        before = (
            await session.execute(
                text("SELECT count(*)::int FROM users WHERE username = :u"),
                {"u": marker},
            )
        ).scalar_one()
        assert before == 1

    truncate_application_tables_sync(integration_database_url)

    async with integration_session_factory() as session:
        after = (
            await session.execute(
                text("SELECT count(*)::int FROM users WHERE username = :u"),
                {"u": marker},
            )
        ).scalar_one()
        assert after == 0
        total = (await session.execute(text("SELECT count(*)::int FROM users"))).scalar_one()
        assert total == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_isolation_seed_leaves_marker_for_next_test(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Definition-ordered pair: commit a marker that the next test must not see."""
    marker = "iso-pair-marker-user"
    async with integration_session_factory() as session:
        await session.execute(
            text(
                """
                INSERT INTO users (
                    id, username, display_name, email, status,
                    created_at, updated_at, lock_version
                ) VALUES (
                    :id, :username, 'Pair', :email, 'ACTIVE',
                    :now, :now, 1
                )
                """
            ),
            {
                "id": uuid.uuid4(),
                "username": marker,
                "email": f"{marker}@example.com",
                "now": datetime.now(UTC),
            },
        )
        await session.commit()
        count = (
            await session.execute(
                text("SELECT count(*)::int FROM users WHERE username = :u"),
                {"u": marker},
            )
        ).scalar_one()
        assert count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_isolation_next_test_does_not_see_prior_marker(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        count = (
            await session.execute(
                text(
                    "SELECT count(*)::int FROM users "
                    "WHERE username = 'iso-pair-marker-user'"
                )
            )
        ).scalar_one()
        assert count == 0
        total = (await session.execute(text("SELECT count(*)::int FROM users"))).scalar_one()
        assert total == 0


@pytest.mark.integration
def test_migration_roundtrip_ends_at_single_head(
    integration_database_url: str,
) -> None:
    cfg = _cfg(integration_database_url)
    head = _head_revision(integration_database_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "20261002_0023")
    command.upgrade(cfg, "head")
    assert current_alembic_revision_sync(integration_database_url) == head


@pytest.mark.integration
def test_partial_downgrade_is_recovered_to_head_schema(
    integration_database_url: str,
) -> None:
    """Simulate a failed roundtrip leaving a lower revision; harness recovers."""
    cfg = _cfg(integration_database_url)
    head = _head_revision(integration_database_url)
    command.downgrade(cfg, "20260923_0019")
    assert current_alembic_revision_sync(integration_database_url) != head

    recover_integration_database(integration_database_url)

    assert current_alembic_revision_sync(integration_database_url) == head
    # Head schema objects must exist after recovery (workflows added in 0020).
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    async def _check() -> None:
        engine = create_async_engine(integration_database_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                workflows = (
                    await conn.execute(text("SELECT to_regclass('public.workflows')"))
                ).scalar_one()
                assert workflows == "workflows"
                approvals = (
                    await conn.execute(
                        text("SELECT to_regclass('public.approval_requests')")
                    )
                ).scalar_one()
                assert approvals == "approval_requests"
        finally:
            await engine.dispose()

    asyncio.run(_check())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_head_schema_has_workflows_after_prior_recovery(
    integration_session_factory: async_sessionmaker[AsyncSession],
    integration_database_url: str,
) -> None:
    head = _head_revision(integration_database_url)
    assert current_alembic_revision_sync(integration_database_url) == head
    async with integration_session_factory() as session:
        workflows = (
            await session.execute(text("SELECT to_regclass('public.workflows')"))
        ).scalar_one()
        assert workflows == "workflows"
        # Suite must start empty thanks to autouse isolation.
        exec_count = (
            await session.execute(text("SELECT count(*)::int FROM executions"))
        ).scalar_one()
        assert exec_count == 0
