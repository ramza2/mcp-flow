"""Alembic round-trip for execution creation foundation tables."""

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
def test_alembic_execution_creation_downgrade_upgrade(
    integration_database_url: str,
) -> None:
    cfg = _cfg(integration_database_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "20260914_0012")
    command.upgrade(cfg, "head")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_execution_creation_schema_constraints(
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
                        'executions',
                        'execution_steps',
                        'api_idempotency_records'
                      )
                    ORDER BY table_name
                    """
                )
            )
        ).scalars().all()
        assert tables == [
            "api_idempotency_records",
            "execution_steps",
            "executions",
        ]

        # --- executions ---
        exec_fks = (
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
                      AND tc.table_name = 'executions'
                    ORDER BY kcu.column_name
                    """
                )
            )
        ).all()
        exec_fk_map = {row.column_name: row.foreign_table_name for row in exec_fks}
        assert exec_fk_map["requester_id"] == "users"
        assert exec_fk_map["agent_request_id"] == "agent_requests"
        assert exec_fk_map["agent_version_id"] == "agent_versions"
        assert exec_fk_map["plan_validation_run_id"] == "plan_validation_runs"
        assert exec_fk_map["parent_execution_id"] == "executions"

        exec_checks = (
            await session.execute(
                text(
                    """
                    SELECT pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conrelid = 'executions'::regclass
                      AND contype = 'c'
                    """
                )
            )
        ).scalars().all()
        exec_defs = " ".join(exec_checks).lower()
        assert "created" in exec_defs
        assert "queued" in exec_defs
        assert "agent_request" in exec_defs
        assert "0-9a-f" in exec_defs
        assert "jsonb_typeof(plan_snapshot)" in exec_defs
        assert "jsonb_typeof(input_snapshot)" in exec_defs
        assert "jsonb_typeof(policy_snapshot)" in exec_defs
        assert "lock_version" in exec_defs

        exec_pks = (
            await session.execute(
                text(
                    """
                    SELECT kcu.column_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                    WHERE tc.table_name = 'executions'
                      AND tc.constraint_type = 'PRIMARY KEY'
                    """
                )
            )
        ).scalars().all()
        assert exec_pks == ["id"]

        exec_indexes = (
            await session.execute(
                text(
                    """
                    SELECT indexname FROM pg_indexes
                    WHERE tablename = 'executions'
                    ORDER BY indexname
                    """
                )
            )
        ).scalars().all()
        assert any("agent_request_id_requested_at" in name for name in exec_indexes)
        assert any("requester_id_requested_at" in name for name in exec_indexes)
        assert any("status_requested_at" in name for name in exec_indexes)

        # --- execution_steps ---
        step_fks = (
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
                      AND tc.table_name = 'execution_steps'
                    ORDER BY kcu.column_name
                    """
                )
            )
        ).all()
        step_fk_map = {row.column_name: row.foreign_table_name for row in step_fks}
        assert step_fk_map["execution_id"] == "executions"
        assert step_fk_map["mcp_tool_version_id"] == "mcp_tool_versions"
        assert step_fk_map["parent_step_id"] == "execution_steps"

        step_checks = (
            await session.execute(
                text(
                    """
                    SELECT pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conrelid = 'execution_steps'::regclass
                      AND contype = 'c'
                    """
                )
            )
        ).scalars().all()
        step_defs = " ".join(step_checks).lower()
        assert "pending" in step_defs
        assert "unknown_outcome" in step_defs
        assert "'tool'" in step_defs
        assert "attempt_count" in step_defs
        assert "jsonb_typeof(step_snapshot)" in step_defs
        assert "ck_execution_steps_result_inline_object" not in " ".join(step_checks)
        assert "result_inline is null or jsonb_typeof(result_inline)" not in step_defs

        uniques = (
            await session.execute(
                text(
                    """
                    SELECT tc.constraint_name
                    FROM information_schema.table_constraints tc
                    WHERE tc.table_name = 'execution_steps'
                      AND tc.constraint_type = 'UNIQUE'
                    """
                )
            )
        ).scalars().all()
        assert any("execution_id_step_key" in name for name in uniques)

        # --- api_idempotency_records ---
        idem_pks = (
            await session.execute(
                text(
                    """
                    SELECT kcu.column_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                    WHERE tc.table_name = 'api_idempotency_records'
                      AND tc.constraint_type = 'PRIMARY KEY'
                    ORDER BY kcu.ordinal_position
                    """
                )
            )
        ).scalars().all()
        assert idem_pks == [
            "principal_key",
            "operation_scope",
            "idempotency_key",
        ]

        idem_checks = (
            await session.execute(
                text(
                    """
                    SELECT pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conrelid = 'api_idempotency_records'::regclass
                      AND contype = 'c'
                    """
                )
            )
        ).scalars().all()
        idem_defs = " ".join(idem_checks).lower()
        assert "completed" in idem_defs
        assert "processing" in idem_defs
        assert "0-9a-f" in idem_defs
        assert "jsonb_typeof(response_body)" in idem_defs

        idem_indexes = (
            await session.execute(
                text(
                    """
                    SELECT indexname FROM pg_indexes
                    WHERE tablename = 'api_idempotency_records'
                    ORDER BY indexname
                    """
                )
            )
        ).scalars().all()
        assert any("resource" in name for name in idem_indexes)
