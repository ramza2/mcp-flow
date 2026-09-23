"""Alembic round-trip and PostgreSQL schema checks for mcp_input_requests (0019)."""

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


def _check_constraint_definition(check_map: dict[str, str], logical_name: str) -> str:
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
def test_alembic_mcp_input_requests_downgrade_upgrade(
    integration_database_url: str,
) -> None:
    cfg = _cfg(integration_database_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "20260922_0018")
    command.upgrade(cfg, "20260923_0019")
    command.downgrade(cfg, "20260922_0018")
    command.upgrade(cfg, "head")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mcp_input_requests_schema_constraints(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        columns = (
            await session.execute(
                text(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema='public' AND table_name='mcp_input_requests'
                    ORDER BY column_name
                    """
                )
            )
        ).scalars().all()
        for required in (
            "id",
            "execution_id",
            "step_execution_id",
            "step_attempt_id",
            "protocol_era",
            "input_requests",
            "request_state",
            "round_no",
            "status",
            "response_payload",
            "requested_at",
            "expires_at",
            "answered_at",
            "answered_by",
        ):
            assert required in columns

        checks = (
            await session.execute(
                text(
                    """
                    SELECT conname, pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conrelid = 'mcp_input_requests'::regclass
                      AND contype = 'c'
                    """
                )
            )
        ).all()
        check_map = {row[0]: row[1] for row in checks}
        status_def = _check_constraint_definition(
            check_map, "ck_mcp_input_requests_status"
        )
        for status in ("OPEN", "ANSWERED", "REJECTED", "EXPIRED", "UNSUPPORTED"):
            assert status in status_def
        era_def = _check_constraint_definition(
            check_map, "ck_mcp_input_requests_protocol_era"
        )
        assert "CURRENT" in era_def
        assert "LEGACY" in era_def
        round_def = _check_constraint_definition(
            check_map, "ck_mcp_input_requests_round_no"
        )
        assert "round_no" in round_def.lower()
        input_def = _check_constraint_definition(
            check_map, "ck_mcp_input_requests_input_requests_object"
        )
        assert "jsonb_typeof" in input_def.lower()
        resp_def = _check_constraint_definition(
            check_map, "ck_mcp_input_requests_response_payload_object"
        )
        assert "response_payload" in resp_def.lower()

        fks = (
            await session.execute(
                text(
                    """
                    SELECT tc.constraint_name, kcu.column_name, ccu.table_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                    JOIN information_schema.constraint_column_usage ccu
                      ON ccu.constraint_name = tc.constraint_name
                     AND ccu.table_schema = tc.table_schema
                    WHERE tc.table_name = 'mcp_input_requests'
                      AND tc.constraint_type = 'FOREIGN KEY'
                    ORDER BY kcu.column_name
                    """
                )
            )
        ).all()
        fk_by_col = {row[1]: row[2] for row in fks}
        assert fk_by_col["execution_id"] == "executions"
        assert fk_by_col["step_execution_id"] == "execution_steps"
        assert fk_by_col["step_attempt_id"] == "step_attempts"
        assert fk_by_col["answered_by"] == "users"

        indexes = (
            await session.execute(
                text(
                    """
                    SELECT indexname
                    FROM pg_indexes
                    WHERE tablename = 'mcp_input_requests'
                    """
                )
            )
        ).scalars().all()
        for required_ix in (
            "ix_mcp_input_requests_execution_id",
            "ix_mcp_input_requests_step_execution_id",
            "ix_mcp_input_requests_status",
            "ix_mcp_input_requests_expires_at",
            "ix_mcp_input_requests_open_expires_at",
        ):
            assert required_ix in indexes
