"""Alembic round-trip and PostgreSQL schema checks for External MCP Discovery (0026)."""

from __future__ import annotations

import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
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
def test_alembic_external_discovery_downgrade_upgrade(
    integration_database_url: str,
) -> None:
    cfg = _cfg(integration_database_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "20261007_0025")
    command.upgrade(cfg, "20261007_0026")
    command.downgrade(cfg, "20261007_0025")
    command.upgrade(cfg, "head")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_external_discovery_schema_constraints(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        for table in (
            "external_mcp_sources",
            "external_mcp_searches",
            "external_mcp_candidates",
            "external_mcp_reviews",
        ):
            exists = (
                await session.execute(
                    text("SELECT to_regclass(:t)"), {"t": f"public.{table}"}
                )
            ).scalar_one()
            assert exists is not None

        source_checks = (
            await session.execute(
                text(
                    """
                    SELECT conname, pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conrelid = 'external_mcp_sources'::regclass
                      AND contype = 'c'
                    """
                )
            )
        ).all()
        source_map = {row[0]: row[1] for row in source_checks}
        source_type_def = _check_constraint_definition(
            source_map, "ck_external_mcp_sources_source_type"
        )
        assert "REGISTRY" in source_type_def
        assert "ALLOWLIST_URL" in source_type_def

        search_checks = (
            await session.execute(
                text(
                    """
                    SELECT conname, pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conrelid = 'external_mcp_searches'::regclass
                      AND contype = 'c'
                    """
                )
            )
        ).all()
        search_map = {row[0]: row[1] for row in search_checks}
        status_def = _check_constraint_definition(
            search_map, "ck_external_mcp_searches_status"
        )
        for status in ("RUNNING", "SUCCEEDED", "FAILED"):
            assert status in status_def

        review_checks = (
            await session.execute(
                text(
                    """
                    SELECT conname, pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conrelid = 'external_mcp_reviews'::regclass
                      AND contype = 'c'
                    """
                )
            )
        ).all()
        review_map = {row[0]: row[1] for row in review_checks}
        decision_def = _check_constraint_definition(
            review_map, "ck_external_mcp_reviews_decision"
        )
        assert "APPROVE" in decision_def
        assert "REJECT" in decision_def

        indexes = (
            await session.execute(
                text(
                    """
                    SELECT indexname FROM pg_indexes
                    WHERE schemaname = 'public'
                      AND tablename LIKE 'external_mcp_%'
                    """
                )
            )
        ).scalars().all()
        for required in (
            "ix_external_mcp_searches_source_started",
            "ix_external_mcp_searches_requested_by_started",
            "ix_external_mcp_candidates_search_id",
            "ix_external_mcp_candidates_source_external_key",
            "ix_external_mcp_candidates_imported_mcp_server_id",
            "ix_external_mcp_reviews_candidate_reviewed",
            "uq_external_mcp_candidates_search_external_key",
        ):
            assert required in indexes


async def _seed_user_source_search(
    session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    source_id = uuid.uuid4()
    search_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO users (id, username, display_name, email, status, lock_version)
            VALUES (:id, :username, 'Disc', :email, 'ACTIVE', 1)
            """
        ),
        {
            "id": user_id,
            "username": f"disc-mig-{uuid.uuid4().hex[:8]}",
            "email": f"disc-mig-{uuid.uuid4().hex[:8]}@example.com",
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO external_mcp_sources
              (id, code, name, source_type, provider_key, enabled)
            VALUES (:id, :code, 'Src', 'REGISTRY', 'test.fake', true)
            """
        ),
        {"id": source_id, "code": f"src-{uuid.uuid4().hex[:8]}"},
    )
    await session.execute(
        text(
            """
            INSERT INTO external_mcp_searches
              (id, source_id, query, status, requested_limit, candidate_count, requested_by)
            VALUES (:id, :source_id, 'q', 'SUCCEEDED', 10, 0, :user_id)
            """
        ),
        {"id": search_id, "source_id": source_id, "user_id": user_id},
    )
    await session.commit()
    return user_id, source_id, search_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_external_discovery_unique_and_invalid_status(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        _user_id, source_id, search_id = await _seed_user_source_search(session)

        with pytest.raises((IntegrityError, DBAPIError)):
            await session.execute(
                text(
                    """
                    INSERT INTO external_mcp_searches
                      (id, source_id, query, status, requested_limit,
                       candidate_count, requested_by)
                    VALUES (:id, :source_id, 'q', 'WAITING', 10, 0,
                      (SELECT id FROM users LIMIT 1))
                    """
                ),
                {"id": uuid.uuid4(), "source_id": source_id},
            )
        await session.rollback()

        await session.execute(
            text(
                """
                INSERT INTO external_mcp_candidates
                  (id, search_id, source_id, external_key, name)
                VALUES (:id, :search_id, :source_id, 'same-key', 'A')
                """
            ),
            {
                "id": uuid.uuid4(),
                "search_id": search_id,
                "source_id": source_id,
            },
        )
        await session.commit()

        with pytest.raises((IntegrityError, DBAPIError)):
            await session.execute(
                text(
                    """
                    INSERT INTO external_mcp_candidates
                      (id, search_id, source_id, external_key, name)
                    VALUES (:id, :search_id, :source_id, 'same-key', 'B')
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "search_id": search_id,
                    "source_id": source_id,
                },
            )
        await session.rollback()
