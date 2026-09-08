"""PostgreSQL integration tests for Model Profiles foundation."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from app.core.errors import AppError
from app.repositories.embedding_profile import EmbeddingProfileRepository
from app.repositories.llm_profile import LLMProfileRepository
from app.schemas.model_profile import EmbeddingProfileCreate, EmbeddingProfileUpdate
from app.services.embedding_profile import EmbeddingProfileService
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.integration
def test_alembic_model_profiles_downgrade_upgrade(integration_database_url: str) -> None:
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    cfg = Config(os.path.join(backend_dir, "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", integration_database_url)
    os.environ["MCPFLOW_DATABASE_URL"] = integration_database_url
    from app.core.config import get_settings

    get_settings.cache_clear()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "20260907_0003")
    command.upgrade(cfg, "head")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_model_profile_db_constraints(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    code = f"llm-{uuid.uuid4().hex[:8]}"
    async with integration_session_factory() as session:
        repo = LLMProfileRepository(session)
        await repo.create(
            code=code,
            name="A",
            provider="OPENAI_COMPATIBLE",
            model="m",
            base_url="https://llm.test/v1",
        )
        await session.commit()

    async with integration_session_factory() as session:
        with pytest.raises(IntegrityError):
            await LLMProfileRepository(session).create(
                code=code,
                name="B",
                provider="OPENAI_COMPATIBLE",
                model="m",
                base_url="https://llm.test/v1",
            )
            await session.commit()
        await session.rollback()

    async with integration_session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO llm_profiles "
                    "(id, code, name, provider, model, base_url, lock_version) "
                    "VALUES (:id, :code, 'Bad', 'P', 'M', 'https://x', 0)"
                ),
                {"id": uuid.uuid4(), "code": f"lv-{uuid.uuid4().hex[:6]}"},
            )
            await session.commit()
        await session.rollback()

    emb_code = f"emb-{uuid.uuid4().hex[:8]}"
    async with integration_session_factory() as session:
        await EmbeddingProfileRepository(session).create(
            code=emb_code,
            name="E",
            provider="OPENAI_COMPATIBLE",
            model="e",
            base_url="https://emb.test/v1",
            dimension=8,
            distance_metric="cosine",
        )
        await session.commit()

    async with integration_session_factory() as session:
        with pytest.raises(IntegrityError):
            await EmbeddingProfileRepository(session).create(
                code=emb_code,
                name="E2",
                provider="OPENAI_COMPATIBLE",
                model="e",
                base_url="https://emb.test/v1",
                dimension=8,
                distance_metric="cosine",
            )
            await session.commit()
        await session.rollback()

    async with integration_session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO embedding_profiles "
                    "(id, code, name, provider, model, base_url, dimension, "
                    "distance_metric, is_active_for_tools, lock_version) "
                    "VALUES (:id, :code, 'Bad', 'P', 'M', 'https://x', 0, 'cosine', false, 1)"
                ),
                {"id": uuid.uuid4(), "code": f"dim-{uuid.uuid4().hex[:6]}"},
            )
            await session.commit()
        await session.rollback()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_activate_for_tools(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        service = EmbeddingProfileService(session)
        a = await service.create(
            EmbeddingProfileCreate(
                name="Act A",
                provider="OPENAI_COMPATIBLE",
                model="e",
                base_url="https://emb.test/v1",
                dimension=8,
                distance_metric="cosine",
            )
        )
        b = await service.create(
            EmbeddingProfileCreate(
                name="Act B",
                provider="OPENAI_COMPATIBLE",
                model="e",
                base_url="https://emb.test/v1",
                dimension=8,
                distance_metric="cosine",
            )
        )
        a_id, b_id = a.id, b.id
        a_lock, b_lock = a.lock_version, b.lock_version

    async def _activate(profile_id: uuid.UUID, lock_version: int) -> str:
        async with integration_session_factory() as session:
            try:
                result = await EmbeddingProfileService(session).activate_for_tools(
                    profile_id, expected_lock_version=lock_version
                )
                return f"OK:{result.id}"
            except AppError as exc:
                return f"ERR:{exc.code}"
            except Exception as exc:  # noqa: BLE001
                return f"ERR:{type(exc).__name__}"

    results = await asyncio.gather(
        _activate(a_id, a_lock),
        _activate(b_id, b_lock),
    )
    oks = [r for r in results if r.startswith("OK:")]
    errs = [r for r in results if r.startswith("ERR:")]
    assert len(oks) >= 1
    # Either both serialize cleanly (one wins) or loser gets RESOURCE_CONFLICT.
    for err in errs:
        assert "RESOURCE_CONFLICT" in err or "RESOURCE_VERSION_CONFLICT" in err

    async with integration_session_factory() as session:
        count = await EmbeddingProfileRepository(session).count_active_for_tools()
        assert count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_idempotent_activate_releases_row_lock(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Idempotent activate must commit so a subsequent session can PATCH promptly."""

    async with integration_session_factory() as session:
        service = EmbeddingProfileService(session)
        profile = await service.create(
            EmbeddingProfileCreate(
                name="Lock Release",
                provider="OPENAI_COMPATIBLE",
                model="e",
                base_url="https://emb.test/v1",
                dimension=8,
                distance_metric="cosine",
            )
        )
        activated = await service.activate_for_tools(
            profile.id, expected_lock_version=int(profile.lock_version)
        )
        profile_id = activated.id
        lock_version = int(activated.lock_version)
        assert activated.is_active_for_tools is True

    activate_returned = asyncio.Event()
    patch_finished = asyncio.Event()
    outcomes: dict[str, object] = {}

    async def session_a_idempotent_activate() -> None:
        async with integration_session_factory() as session:
            result = await EmbeddingProfileService(session).activate_for_tools(
                profile_id, expected_lock_version=lock_version
            )
            outcomes["activate_lock"] = int(result.lock_version)
            outcomes["activate_active"] = bool(result.is_active_for_tools)
            activate_returned.set()
            # Keep the session open after method return to detect lingering locks.
            await patch_finished.wait()

    async def session_b_patch_after_release() -> None:
        await activate_returned.wait()

        async def _patch() -> int:
            async with integration_session_factory() as session:
                updated = await EmbeddingProfileService(session).update(
                    profile_id,
                    EmbeddingProfileUpdate(
                        name="After Idempotent", lock_version=lock_version
                    ),
                    expected_lock_version=lock_version,
                )
                return int(updated.lock_version)

        new_lock = await asyncio.wait_for(_patch(), timeout=2.0)
        outcomes["patch_lock"] = new_lock
        patch_finished.set()

    await asyncio.gather(session_a_idempotent_activate(), session_b_patch_after_release())
    assert outcomes["activate_active"] is True
    assert outcomes["activate_lock"] == lock_version
    assert outcomes["patch_lock"] == lock_version + 1
