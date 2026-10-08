"""Alembic round-trip and PostgreSQL schema checks for Tool Factory (0028)."""

from __future__ import annotations

import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
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
def test_alembic_factory_downgrade_upgrade(integration_database_url: str) -> None:
    cfg = _cfg(integration_database_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "20261008_0027")
    command.upgrade(cfg, "20261008_0028")
    command.downgrade(cfg, "20261008_0027")
    command.upgrade(cfg, "head")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_factory_schema_constraints(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        for table in (
            "tool_factory_jobs",
            "tool_factory_artifacts",
            "tool_factory_test_results",
        ):
            exists = (
                await session.execute(
                    text("SELECT to_regclass(:t)"), {"t": f"public.{table}"}
                )
            ).scalar_one()
            assert exists is not None

        cols = (
            await session.execute(
                text(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'tool_factory_jobs'
                    """
                )
            )
        ).scalars().all()
        colset = {c.lower() for c in cols}
        assert "source_sha256" in colset
        for forbidden in ("source_body", "source_content", "raw_source", "content"):
            assert forbidden not in colset

        checks = (
            await session.execute(
                text(
                    """
                    SELECT conname, pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conrelid = 'tool_factory_jobs'::regclass
                      AND contype = 'c'
                    """
                )
            )
        ).all()
        check_map = {row[0]: row[1] for row in checks}
        status_def = _check_constraint_definition(check_map, "ck_tool_factory_jobs_status")
        for value in (
            "PENDING",
            "QUEUED",
            "RUNNING",
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
            "TIMED_OUT",
        ):
            assert value in status_def

        indexes = (
            await session.execute(
                text(
                    """
                    SELECT indexname FROM pg_indexes
                    WHERE schemaname = 'public'
                      AND tablename LIKE 'tool_factory_%'
                    """
                )
            )
        ).scalars().all()
        for required in (
            "ix_tool_factory_jobs_requested_by_created_at",
            "ix_tool_factory_jobs_status_created_at",
            "ix_tool_factory_jobs_created_at",
            "ix_tool_factory_artifacts_job_id",
            "ix_tool_factory_test_results_job_id",
            "uq_tool_factory_artifacts_job_artifact_type",
        ):
            assert required in indexes


async def _seed_user_job(
    session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO users (id, username, display_name, email, status, lock_version)
            VALUES (:id, :username, 'Fac', :email, 'ACTIVE', 1)
            """
        ),
        {
            "id": user_id,
            "username": f"fac-mig-{uuid.uuid4().hex[:8]}",
            "email": f"fac-mig-{uuid.uuid4().hex[:8]}@example.com",
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO tool_factory_jobs (
              id, job_type, status, source_name, source_sha256,
              analyzer_version, requested_by
            ) VALUES (
              :id, 'OPENAPI_ANALYZE', 'SUCCEEDED', 'demo.json',
              :sha, 'openapi-analyzer-v1', :uid
            )
            """
        ),
        {"id": job_id, "sha": "a" * 64, "uid": user_id},
    )
    await session.commit()
    return user_id, job_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_factory_job_status_and_artifact_unique(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        user_id, job_id = await _seed_user_job(session)

        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    """
                    INSERT INTO tool_factory_jobs (
                      id, job_type, status, source_name, source_sha256,
                      analyzer_version, requested_by
                    ) VALUES (
                      :id, 'OPENAPI_ANALYZE', 'DONE', 'x.json',
                      :sha, 'openapi-analyzer-v1', :uid
                    )
                    """
                ),
                {"id": uuid.uuid4(), "sha": "b" * 64, "uid": user_id},
            )
            await session.flush()
        await session.rollback()

        art_id = uuid.uuid4()
        await session.execute(
            text(
                """
                INSERT INTO tool_factory_artifacts (
                  id, job_id, artifact_type, content_type,
                  content_sha256, size_bytes, inline_payload
                ) VALUES (
                  :id, :job_id, 'OPENAPI_ANALYSIS', 'application/json',
                  :sha, 2, '{"ok":true}'::jsonb
                )
                """
            ),
            {"id": art_id, "job_id": job_id, "sha": "c" * 64},
        )
        await session.commit()

        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    """
                    INSERT INTO tool_factory_artifacts (
                      id, job_id, artifact_type, content_type,
                      content_sha256, size_bytes, inline_payload
                    ) VALUES (
                      :id, :job_id, 'OPENAPI_ANALYSIS', 'application/json',
                      :sha, 2, '{"ok":false}'::jsonb
                    )
                    """
                ),
                {"id": uuid.uuid4(), "job_id": job_id, "sha": "d" * 64},
            )
            await session.flush()
        await session.rollback()

        # test_results evidence FK accepts artifact id.
        await session.execute(
            text(
                """
                INSERT INTO tool_factory_test_results (
                  id, job_id, test_name, passed, evidence_artifact_id, duration_ms
                ) VALUES (
                  :id, :job_id, 'smoke', true, :art, 10
                )
                """
            ),
            {"id": uuid.uuid4(), "job_id": job_id, "art": art_id},
        )
        await session.commit()

        # Missing evidence artifact rejected.
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    """
                    INSERT INTO tool_factory_test_results (
                      id, job_id, test_name, passed, evidence_artifact_id
                    ) VALUES (
                      :id, :job_id, 'missing', false, :art
                    )
                    """
                ),
                {"id": uuid.uuid4(), "job_id": job_id, "art": uuid.uuid4()},
            )
            await session.flush()
        await session.rollback()
