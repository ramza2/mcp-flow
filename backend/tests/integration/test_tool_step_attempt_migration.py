"""Alembic round-trip and PostgreSQL schema checks for StepAttempt foundation."""

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
def test_alembic_step_attempt_downgrade_upgrade(
    integration_database_url: str,
) -> None:
    cfg = _cfg(integration_database_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "20260915_0014")
    command.upgrade(cfg, "head")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_step_attempt_schema_constraints(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        columns = (
            await session.execute(
                text(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema='public' AND table_name='step_attempts'
                    ORDER BY column_name
                    """
                )
            )
        ).scalars().all()
        for required in (
            "id",
            "step_execution_id",
            "attempt_no",
            "status",
            "worker_id",
            "lease_expires_at",
            "idempotency_key",
            "request_snapshot",
            "result_inline",
            "result_blob_id",
            "error_layer",
            "error_code",
            "error_message",
            "is_retryable",
            "started_at",
            "finished_at",
        ):
            assert required in columns

        checks = (
            await session.execute(
                text(
                    """
                    SELECT conname, pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conrelid = 'step_attempts'::regclass
                      AND contype = 'c'
                    """
                )
            )
        ).all()
        check_map = {row[0]: row[1] for row in checks}
        status_def = _check_constraint_definition(check_map, "ck_step_attempts_status")
        assert "STARTED" in status_def
        assert "UNKNOWN_OUTCOME" in status_def
        attempt_def = _check_constraint_definition(
            check_map, "ck_step_attempts_attempt_no"
        )
        assert "attempt_no" in attempt_def.lower()
        # pg_get_constraintdef may include spaces; normalize before matching.
        assert ">=1" in attempt_def.replace(" ", "")

        uniques = (
            await session.execute(
                text(
                    """
                    SELECT tc.constraint_name
                    FROM information_schema.table_constraints tc
                    WHERE tc.table_name = 'step_attempts'
                      AND tc.constraint_type = 'UNIQUE'
                    """
                )
            )
        ).scalars().all()
        assert any("attempt_no" in name for name in uniques)

        fks = (
            await session.execute(
                text(
                    """
                    SELECT kcu.column_name, ccu.table_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                    JOIN information_schema.constraint_column_usage ccu
                      ON ccu.constraint_name = tc.constraint_name
                     AND ccu.table_schema = tc.table_schema
                    WHERE tc.table_name = 'step_attempts'
                      AND tc.constraint_type = 'FOREIGN KEY'
                    """
                )
            )
        ).all()
        fk_map = {row[0]: row[1] for row in fks}
        assert fk_map["step_execution_id"] == "execution_steps"

        indexes = (
            await session.execute(
                text(
                    """
                    SELECT indexname FROM pg_indexes
                    WHERE tablename = 'step_attempts'
                    ORDER BY indexname
                    """
                )
            )
        ).scalars().all()
        assert any("step_execution_id" in name for name in indexes)
        assert any("idempotency_key" in name for name in indexes)
