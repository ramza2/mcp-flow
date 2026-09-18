"""Alembic round-trip and PostgreSQL schema checks for MCP Tool Runner foundation."""

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
def test_alembic_mcp_tool_runner_downgrade_upgrade(integration_database_url: str) -> None:
    cfg = _cfg(integration_database_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "20260917_0015")
    command.upgrade(cfg, "head")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_secret_records_schema_constraints(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        columns = (
            (
                await session.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema='public' AND table_name='secret_records'
                        ORDER BY column_name
                        """
                    )
                )
            )
            .scalars()
            .all()
        )
        for required in (
            "id",
            "name",
            "secret_kind",
            "ciphertext",
            "nonce",
            "key_version",
            "fingerprint",
            "status",
            "expires_at",
            "rotated_at",
            "created_at",
            "updated_at",
        ):
            assert required in columns

        # No plaintext column must ever exist on secret_records.
        for forbidden in ("plaintext", "value", "password", "secret_value", "raw_value"):
            assert forbidden not in columns

        checks = (
            (
                await session.execute(
                    text(
                        """
                        SELECT conname, pg_get_constraintdef(oid)
                        FROM pg_constraint
                        WHERE conrelid = 'secret_records'::regclass
                          AND contype = 'c'
                        """
                    )
                )
            )
            .all()
        )
        check_map = {row[0]: row[1] for row in checks}
        kind_def = _check_constraint_definition(check_map, "ck_secret_records_secret_kind")
        for kind in ("API_KEY", "OAUTH_TOKEN_SET", "BASIC_AUTH", "CUSTOM"):
            assert kind in kind_def
        status_def = _check_constraint_definition(check_map, "ck_secret_records_status")
        for status in ("ACTIVE", "EXPIRED", "REVOKED"):
            assert status in status_def
        key_version_def = _check_constraint_definition(
            check_map, "ck_secret_records_key_version"
        )
        assert "key_version" in key_version_def.lower()

        uniques = (
            (
                await session.execute(
                    text(
                        """
                        SELECT tc.constraint_name
                        FROM information_schema.table_constraints tc
                        WHERE tc.table_name = 'secret_records'
                          AND tc.constraint_type = 'UNIQUE'
                        """
                    )
                )
            )
            .scalars()
            .all()
        )
        assert any("name" in name for name in uniques)

        indexes = (
            (
                await session.execute(
                    text(
                        """
                        SELECT indexname FROM pg_indexes
                        WHERE tablename = 'secret_records'
                        ORDER BY indexname
                        """
                    )
                )
            )
            .scalars()
            .all()
        )
        assert any("status" in name for name in indexes)
        assert any("fingerprint" in name for name in indexes)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tool_calls_schema_constraints(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        columns = (
            (
                await session.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema='public' AND table_name='tool_calls'
                        ORDER BY column_name
                        """
                    )
                )
            )
            .scalars()
            .all()
        )
        for required in (
            "id",
            "step_attempt_id",
            "mcp_server_id",
            "mcp_tool_version_id",
            "protocol_era",
            "protocol_version",
            "transport_type",
            "remote_request_id",
            "request_meta",
            "response_meta",
            "normalized_status",
            "request_bytes",
            "response_bytes",
            "started_at",
            "first_byte_at",
            "finished_at",
        ):
            assert required in columns

        checks = (
            (
                await session.execute(
                    text(
                        """
                        SELECT conname, pg_get_constraintdef(oid)
                        FROM pg_constraint
                        WHERE conrelid = 'tool_calls'::regclass
                          AND contype = 'c'
                        """
                    )
                )
            )
            .all()
        )
        check_map = {row[0]: row[1] for row in checks}
        status_def = _check_constraint_definition(
            check_map, "ck_tool_calls_normalized_status"
        )
        for status in (
            "STARTED",
            "SUCCEEDED",
            "FAILED",
            "TIMED_OUT",
            "CANCELLED",
            "UNKNOWN_OUTCOME",
        ):
            assert status in status_def
        request_meta_def = _check_constraint_definition(
            check_map, "ck_tool_calls_request_meta_object"
        )
        assert "jsonb_typeof" in request_meta_def
        response_meta_def = _check_constraint_definition(
            check_map, "ck_tool_calls_response_meta_object"
        )
        assert "jsonb_typeof" in response_meta_def

        fks = (
            await session.execute(
                text(
                    """
                    SELECT kcu.column_name, ccu.table_name, rc.delete_rule
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                    JOIN information_schema.constraint_column_usage ccu
                      ON ccu.constraint_name = tc.constraint_name
                     AND ccu.table_schema = tc.table_schema
                    JOIN information_schema.referential_constraints rc
                      ON rc.constraint_name = tc.constraint_name
                     AND rc.constraint_schema = tc.table_schema
                    WHERE tc.table_name = 'tool_calls'
                      AND tc.constraint_type = 'FOREIGN KEY'
                    """
                )
            )
        ).all()
        fk_map = {row[0]: (row[1], row[2]) for row in fks}
        assert fk_map["step_attempt_id"] == ("step_attempts", "CASCADE")
        assert fk_map["mcp_server_id"] == ("mcp_servers", "RESTRICT")
        assert fk_map["mcp_tool_version_id"] == ("mcp_tool_versions", "RESTRICT")

        uniques = (
            (
                await session.execute(
                    text(
                        """
                        SELECT tc.constraint_name
                        FROM information_schema.table_constraints tc
                        WHERE tc.table_name = 'tool_calls'
                          AND tc.constraint_type = 'UNIQUE'
                        """
                    )
                )
            )
            .scalars()
            .all()
        )
        assert any(
            "step_attempt_id" in name and "remote_request_id" in name for name in uniques
        )

        indexes = (
            (
                await session.execute(
                    text(
                        """
                        SELECT indexname FROM pg_indexes
                        WHERE tablename = 'tool_calls'
                        ORDER BY indexname
                        """
                    )
                )
            )
            .scalars()
            .all()
        )
        assert any("step_attempt_id" in name for name in indexes)
        assert any("mcp_server_id" in name for name in indexes)
        assert any("mcp_tool_version_id" in name for name in indexes)
        assert any("remote_request_id" in name for name in indexes)
