"""Alembic round-trip and PostgreSQL schema checks for Queue / Claim foundation."""

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


def _check_constraint_definition(
    check_map: dict[str, str],
    logical_name: str,
) -> str:
    """Resolve a logical CHECK name through the SQLAlchemy naming convention.

    The project convention is ``ck_%(table_name)s_%(constraint_name)s``.  Alembic
    therefore persists an explicitly supplied logical name such as
    ``ck_executions_running_lease`` with an additional table prefix on PostgreSQL.
    Keep this regression focused on the intended logical constraint and definition
    rather than coupling it to that rendered prefix.
    """
    matches = [
        definition
        for name, definition in check_map.items()
        if name == logical_name or name.endswith(f"_{logical_name}")
    ]
    assert len(matches) == 1, (
        f"expected exactly one CHECK matching {logical_name!r}; "
        f"found names={sorted(check_map)}"
    )
    return matches[0]


@pytest.mark.integration
def test_alembic_execution_queue_claim_downgrade_upgrade(
    integration_database_url: str,
) -> None:
    cfg = _cfg(integration_database_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "20260915_0013")
    command.upgrade(cfg, "head")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_execution_queue_claim_schema_constraints(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        exec_columns = (
            await session.execute(
                text(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema='public' AND table_name='executions'
                      AND column_name IN (
                        'worker_id','lease_token','lease_expires_at','heartbeat_at'
                      )
                    ORDER BY column_name
                    """
                )
            )
        ).scalars().all()
        assert exec_columns == [
            "heartbeat_at",
            "lease_expires_at",
            "lease_token",
            "worker_id",
        ]

        exec_checks = (
            await session.execute(
                text(
                    """
                    SELECT conname, pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conrelid='executions'::regclass AND contype='c'
                    """
                )
            )
        ).all()
        check_map = {row.conname: row.pg_get_constraintdef for row in exec_checks}
        running_lease = _check_constraint_definition(
            check_map, "ck_executions_running_lease"
        )
        _check_constraint_definition(check_map, "ck_executions_queued_without_lease")
        assert "running" in running_lease.lower()
        assert "lease_token" in running_lease.lower()

        tables = (
            await session.execute(
                text(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema='public' AND table_name='outbox_events'
                    """
                )
            )
        ).scalars().all()
        assert tables == ["outbox_events"]

        outbox_columns = (
            await session.execute(
                text(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema='public' AND table_name='outbox_events'
                    ORDER BY column_name
                    """
                )
            )
        ).scalars().all()
        for expected in (
            "id",
            "event_type",
            "aggregate_type",
            "aggregate_id",
            "dedupe_key",
            "payload",
            "created_at",
            "last_attempt_at",
            "published_at",
            "publish_attempt_count",
            "last_error_code",
            "lock_version",
        ):
            assert expected in outbox_columns

        outbox_checks = (
            await session.execute(
                text(
                    """
                    SELECT pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conrelid='outbox_events'::regclass AND contype='c'
                    """
                )
            )
        ).scalars().all()
        outbox_defs = " ".join(outbox_checks).lower()
        assert "jsonb_typeof(payload)" in outbox_defs
        assert "publish_attempt_count" in outbox_defs
        assert "lock_version" in outbox_defs

        uniques = (
            await session.execute(
                text(
                    """
                    SELECT constraint_name
                    FROM information_schema.table_constraints
                    WHERE table_schema='public' AND table_name='outbox_events'
                      AND constraint_type='UNIQUE'
                    """
                )
            )
        ).scalars().all()
        assert "uq_outbox_events_dedupe_key" in uniques

        indexes = (
            await session.execute(
                text(
                    """
                    SELECT indexname, indexdef
                    FROM pg_indexes
                    WHERE tablename IN ('executions','outbox_events')
                    """
                )
            )
        ).all()
        index_names = {row.indexname for row in indexes}
        assert "ix_executions_status_lease_expires_at" in index_names
        assert "ix_outbox_events_published_at_created_at" in index_names
        assert "ix_outbox_events_aggregate" in index_names
        assert "ix_outbox_events_unpublished_created_at" in index_names
        unpublished = next(
            row.indexdef
            for row in indexes
            if row.indexname == "ix_outbox_events_unpublished_created_at"
        )
        assert "WHERE (published_at IS NULL)" in unpublished or "WHERE published_at IS NULL" in unpublished
