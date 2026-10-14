"""Alembic round-trip and PostgreSQL schema checks for Tool Factory (0028)."""

from __future__ import annotations

import json
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
    command.downgrade(cfg, "20261008_0028")
    command.upgrade(cfg, "20261013_0032")
    command.downgrade(cfg, "20261013_0030")
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
            "tool_factory_candidates",
            "tool_factory_builds",
            "tool_factory_generated_packages",
            "tool_factory_test_results",
            "tool_factory_test_runs",
            "tool_factory_reviews",
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
        for required in (
            "source_bucket",
            "source_object_key",
            "source_content_type",
            "source_size_bytes",
            "queued_at",
            "worker_id",
            "lease_token",
            "lease_expires_at",
            "lock_version",
        ):
            assert required in colset
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
            "ix_tool_factory_test_runs_generated_package_id",
            "ix_tool_factory_test_runs_status_lease_expires_at",
            "uq_tool_factory_test_runs_package_policies",
            "ix_tool_factory_reviews_generated_package_id",
            "uq_tool_factory_reviews_test_run_id",
        ):
            assert required in indexes

        test_run_checks = (
            await session.execute(
                text(
                    """
                    SELECT conname, pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conrelid = 'tool_factory_test_runs'::regclass
                      AND contype = 'c'
                    """
                )
            )
        ).all()
        test_run_check_map = {row[0]: row[1] for row in test_run_checks}
        status_def = _check_constraint_definition(
            test_run_check_map, "ck_tool_factory_test_runs_status"
        )
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
        result_def = _check_constraint_definition(
            test_run_check_map, "ck_tool_factory_test_runs_result"
        )
        assert "PASS" in result_def
        assert "FAIL" in result_def


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
                  :sha, 2, CAST(:payload AS jsonb)
                )
                """
            ),
            {
                "id": art_id,
                "job_id": job_id,
                "sha": "c" * 64,
                "payload": '{"ok": true}',
            },
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
                      :sha, 2, CAST(:payload AS jsonb)
                    )
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "job_id": job_id,
                    "sha": "d" * 64,
                    "payload": '{"ok": false}',
                },
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
                  :id, :job_id, 'smoke', CAST(:passed AS boolean), :art, 10
                )
                """
            ),
            {
                "id": uuid.uuid4(),
                "job_id": job_id,
                "art": art_id,
                "passed": True,
            },
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
                      :id, :job_id, 'missing', CAST(:passed AS boolean), :art
                    )
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "job_id": job_id,
                    "art": uuid.uuid4(),
                    "passed": False,
                },
            )
            await session.flush()
        await session.rollback()

        # Candidate unique (job_id, operation_key).
        cand_id = uuid.uuid4()
        await session.execute(
            text(
                """
                INSERT INTO tool_factory_candidates (
                  id, job_id, analysis_artifact_id, source_sha256,
                  analyzer_version, operation_key, method, path,
                  tags, parameters
                ) VALUES (
                  :id, :job_id, :art, :sha, 'openapi-analyzer-v1',
                  'GET /pets', 'GET', '/pets',
                  CAST('[]' AS jsonb), CAST('[]' AS jsonb)
                )
                """
            ),
            {
                "id": cand_id,
                "job_id": job_id,
                "art": art_id,
                "sha": "a" * 64,
            },
        )
        await session.commit()

        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    """
                    INSERT INTO tool_factory_candidates (
                      id, job_id, analysis_artifact_id, source_sha256,
                      analyzer_version, operation_key, method, path,
                      tags, parameters
                    ) VALUES (
                      :id, :job_id, :art, :sha, 'openapi-analyzer-v1',
                      'GET /pets', 'GET', '/pets',
                      CAST('[]' AS jsonb), CAST('[]' AS jsonb)
                    )
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "job_id": job_id,
                    "art": art_id,
                    "sha": "a" * 64,
                },
            )
            await session.flush()
        await session.rollback()

        # Build candidate_count >= 1.
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    """
                    INSERT INTO tool_factory_builds (
                      id, job_id, analysis_artifact_id, source_sha256,
                      analyzer_version, generator_version, candidate_ids,
                      candidate_count, candidates_snapshot,
                      candidates_snapshot_hash, build_input_hash, requested_by
                    ) VALUES (
                      :id, :job_id, :art, :sha, 'openapi-analyzer-v1',
                      'factory-build-foundation-v1', CAST('[]' AS jsonb),
                      0, CAST('[]' AS jsonb), :h1, :h2, :uid
                    )
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "job_id": job_id,
                    "art": art_id,
                    "sha": "a" * 64,
                    "h1": "b" * 64,
                    "h2": "c" * 64,
                    "uid": user_id,
                },
            )
            await session.flush()
        await session.rollback()

        build_id = uuid.uuid4()
        await session.execute(
            text(
                """
                INSERT INTO tool_factory_builds (
                  id, job_id, analysis_artifact_id, source_sha256,
                  analyzer_version, generator_version, candidate_ids,
                  candidate_count, candidates_snapshot,
                  candidates_snapshot_hash, build_input_hash, requested_by
                ) VALUES (
                  :id, :job_id, :art, :sha, 'openapi-analyzer-v1',
                  'factory-build-foundation-v1', CAST(:cids AS jsonb),
                  1, CAST(:snap AS jsonb), :h1, :h2, :uid
                )
                """
            ),
            {
                "id": build_id,
                "job_id": job_id,
                "art": art_id,
                "sha": "a" * 64,
                "cids": json.dumps([str(cand_id)]),
                "snap": json.dumps([{"candidate_id": str(cand_id)}]),
                "h1": "b" * 64,
                "h2": "c" * 64,
                "uid": user_id,
            },
        )
        await session.commit()

        # Generated package tool_count >= 1.
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    """
                    INSERT INTO tool_factory_generated_packages (
                      id, build_id, job_id, analysis_artifact_id, source_sha256,
                      analyzer_version, build_input_hash, build_generator_version,
                      generator_version, package_schema_version,
                      package_content_sha256, package_size_bytes, tool_count,
                      object_bucket, object_key, content_type, requested_by
                    ) VALUES (
                      :id, :bid, :job_id, :art, :sha, 'openapi-analyzer-v1',
                      :h2, 'factory-build-foundation-v1',
                      'factory-package-generator-v1', 'factory-generated-package-v1',
                      :h1, 0, 0, 'bucket', 'key', 'application/json', :uid
                    )
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "bid": build_id,
                    "job_id": job_id,
                    "art": art_id,
                    "sha": "a" * 64,
                    "h1": "b" * 64,
                    "h2": "c" * 64,
                    "uid": user_id,
                },
            )
            await session.flush()
        await session.rollback()
