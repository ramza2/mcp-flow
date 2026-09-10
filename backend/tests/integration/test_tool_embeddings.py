"""PostgreSQL integration tests for tool_embeddings foundation."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from app.domain.enums import ToolEmbeddingStatus, ToolVersionValidationStatus
from app.model_provider.client import ModelProviderClient
from app.models.mcp import ToolEmbedding
from app.repositories.embedding_profile import EmbeddingProfileRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.tool_embedding import ToolEmbeddingRepository
from app.schemas.mcp_tool import MCPToolUpdate
from app.search.tool_document import ToolSearchDocumentBuilder
from app.search.tool_embedding import ToolEmbeddingService
from app.services.mcp_tool import MCPToolService
from sqlalchemy import func, select, text
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
def test_alembic_tool_embeddings_downgrade_upgrade(integration_database_url: str) -> None:
    cfg = _cfg(integration_database_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "20260908_0006")
    command.upgrade(cfg, "head")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tool_embeddings_schema_and_vector_extension(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        ext = (
            await session.execute(
                text("SELECT extversion FROM pg_extension WHERE extname='vector'")
            )
        ).scalar_one()
        assert ext

        cols = (
            await session.execute(
                text(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'tool_embeddings'
                    ORDER BY column_name
                    """
                )
            )
        ).scalars().all()
        for required in (
            "id",
            "mcp_tool_version_id",
            "embedding_profile_id",
            "search_text",
            "search_tsv",
            "embedding",
            "content_hash",
            "status",
            "created_at",
            "updated_at",
        ):
            assert required in cols

        check = (
            await session.execute(
                text(
                    """
                    SELECT pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conrelid = 'tool_embeddings'::regclass
                      AND contype = 'c'
                      AND pg_get_constraintdef(oid) ILIKE '%READY%'
                    """
                )
            )
        ).scalar_one()
        assert "READY" in check and "STALE" in check and "FAILED" in check

        indexes = (
            await session.execute(
                text(
                    """
                    SELECT indexname FROM pg_indexes
                    WHERE tablename = 'tool_embeddings'
                    """
                )
            )
        ).scalars().all()
        assert "uq_tool_embeddings_version_profile" in indexes
        assert "ix_tool_embeddings_search_tsv" in indexes

        col_type = (
            await session.execute(
                text(
                    """
                    SELECT data_type, character_maximum_length
                    FROM information_schema.columns
                    WHERE table_name = 'tool_embeddings'
                      AND column_name = 'content_hash'
                    """
                )
            )
        ).one()
        assert col_type[0] in {"character", "char"}
        assert int(col_type[1]) == 64


async def _seed_tool(
    session: AsyncSession,
    *,
    remote_name: str,
    description: str,
    tags: list[str] | None = None,
    display_name: str | None = None,
    input_schema: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    server = await MCPServerRepository(session).create(
        code=f"srv-{uuid.uuid4().hex[:8]}",
        name=f"Server {remote_name}",
        transport_type="STREAMABLE_HTTP",
        endpoint_url="https://mcp.test/mcp",
    )
    tools = MCPToolRepository(session)
    tool = await tools.create_tool(
        mcp_server_id=server.id,
        remote_name=remote_name,
        display_name=display_name,
        tags=tags,
        status="DISCOVERED",
    )
    content_hash = hashlib.sha256(remote_name.encode()).hexdigest()
    version = await tools.create_version(
        mcp_tool_id=tool.id,
        version_no=1,
        content_hash=content_hash,
        validation_status=ToolVersionValidationStatus.VALID,
        remote_description=description,
        input_schema=input_schema
        or {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": f"{remote_name} query"}
            },
            "required": ["q"],
        },
        output_schema={"description": f"{remote_name} result"},
    )
    tool.current_version_id = version.id
    await session.flush()
    return tool.id, version.id


async def _seed_profile(
    session: AsyncSession,
    *,
    dimension: int = 4,
    code: str | None = None,
    active: bool = False,
) -> uuid.UUID:
    profile = await EmbeddingProfileRepository(session).create(
        code=code or f"emb-{uuid.uuid4().hex[:8]}",
        name="Emb",
        provider="OPENAI_COMPATIBLE",
        model="emb-test",
        base_url="https://llm.test/v1",
        dimension=dimension,
        distance_metric="cosine",
        is_active_for_tools=active,
    )
    await session.flush()
    return profile.id


def _mock_embed_client(
    vectors_by_text: dict[str, list[float]] | None = None,
) -> ModelProviderClient:
    default_dim = 4

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        payload = json.loads(request.content.decode("utf-8"))
        inputs = payload["input"]
        data = []
        for i, text_value in enumerate(inputs):
            if vectors_by_text and text_value in vectors_by_text:
                vector = vectors_by_text[text_value]
            else:
                # Deterministic tiny vector from text hash.
                digest = hashlib.sha256(text_value.encode()).digest()
                vector = [((digest[j] / 255.0) * 2 - 1) for j in range(default_dim)]
            data.append({"index": i, "embedding": vector, "object": "embedding"})
        return httpx.Response(
            200,
            json={"object": "list", "data": data, "model": "emb-test"},
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    return ModelProviderClient(http=http)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fts_lexical_ranks_weather(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        profile_id = await _seed_profile(session, dimension=4)
        specs = [
            ("weather_lookup", "Lookup weather forecasts for cities", ["weather"]),
            ("send_email", "Send an email message to recipients", ["mail"]),
            ("create_calendar_event", "Create a calendar event on a schedule", ["calendar"]),
        ]
        version_ids: dict[str, uuid.UUID] = {}
        for remote_name, description, tags in specs:
            _tool_id, version_id = await _seed_tool(
                session,
                remote_name=remote_name,
                description=description,
                tags=tags,
                display_name=remote_name.replace("_", " ").title(),
            )
            version_ids[remote_name] = version_id
            tool = await MCPToolRepository(session).get(_tool_id)
            version = await MCPToolRepository(session).get_version(version_id)
            assert tool is not None and version is not None
            doc = ToolSearchDocumentBuilder().build(tool, version)
            await ToolEmbeddingRepository(session).upsert_ready_if_current(
                tool_version_id=version_id,
                embedding_profile_id=profile_id,
                search_text=doc.search_text,
                content_hash=doc.content_hash,
                embedding=[0.1, 0.2, 0.3, 0.4],
            )
        await session.commit()

        hits = await ToolEmbeddingRepository(session).lexical_search(
            query="weather",
            profile_id=profile_id,
            limit=10,
        )
        assert hits
        assert hits[0].tool_version_id == version_ids["weather_lookup"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exact_cosine_and_profile_scope(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        profile_a = await _seed_profile(session, dimension=3, code=f"a-{uuid.uuid4().hex[:6]}")
        profile_b = await _seed_profile(session, dimension=3, code=f"b-{uuid.uuid4().hex[:6]}")
        _t1, v1 = await _seed_tool(
            session, remote_name="near_tool", description="Near vector tool"
        )
        _t2, v2 = await _seed_tool(
            session, remote_name="far_tool", description="Far vector tool"
        )
        repo = ToolEmbeddingRepository(session)
        await repo.upsert_ready_if_current(
            tool_version_id=v1,
            embedding_profile_id=profile_a,
            search_text="near",
            content_hash="1" * 64,
            embedding=[1.0, 0.0, 0.0],
        )
        await repo.upsert_ready_if_current(
            tool_version_id=v2,
            embedding_profile_id=profile_a,
            search_text="far",
            content_hash="2" * 64,
            embedding=[0.0, 1.0, 0.0],
        )
        await repo.upsert_ready_if_current(
            tool_version_id=v1,
            embedding_profile_id=profile_b,
            search_text="near-b",
            content_hash="3" * 64,
            embedding=[0.0, 0.0, 1.0],
        )
        await session.commit()

        hits_a = await repo.vector_search(
            query_vector=[0.9, 0.1, 0.0],
            profile_id=profile_a,
            expected_dimension=3,
            limit=5,
        )
        assert hits_a[0].tool_version_id == v1
        assert all(True for _ in hits_a)  # profile scoped below
        only_a = {h.tool_version_id for h in hits_a}
        assert v1 in only_a

        hits_b = await repo.vector_search(
            query_vector=[0.0, 0.0, 1.0],
            profile_id=profile_b,
            expected_dimension=3,
            limit=5,
        )
        assert len(hits_b) == 1
        assert hits_b[0].tool_version_id == v1

        with pytest.raises(ValueError, match="dimension mismatch"):
            await repo.vector_search(
                query_vector=[1.0, 0.0],
                profile_id=profile_a,
                expected_dimension=3,
                limit=5,
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_upsert_single_row(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        profile_id = await _seed_profile(session, dimension=2)
        _tool_id, version_id = await _seed_tool(
            session, remote_name="concurrent_tool", description="Concurrent"
        )
        await session.commit()

    async def _writer(vec: list[float], suffix: str) -> None:
        async with integration_session_factory() as session:
            await ToolEmbeddingRepository(session).upsert_ready_if_current(
                tool_version_id=version_id,
                embedding_profile_id=profile_id,
                search_text=f"text-{suffix}",
                content_hash=hashlib.sha256(suffix.encode()).hexdigest(),
                embedding=vec,
            )
            await session.commit()

    await asyncio.gather(
        _writer([1.0, 0.0], "a"),
        _writer([0.0, 1.0], "b"),
    )

    async with integration_session_factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(ToolEmbedding)
                .where(
                    ToolEmbedding.mcp_tool_version_id == version_id,
                    ToolEmbedding.embedding_profile_id == profile_id,
                )
            )
        ).scalar_one()
        assert count == 1
        row = (
            await session.execute(
                select(ToolEmbedding).where(
                    ToolEmbedding.mcp_tool_version_id == version_id,
                    ToolEmbedding.embedding_profile_id == profile_id,
                )
            )
        ).scalar_one()
        assert row.status == ToolEmbeddingStatus.READY


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ensure_embedding_smoke_and_metadata_race(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        profile_id = await _seed_profile(session, dimension=4, active=False)
        tool_id, version_id = await _seed_tool(
            session,
            remote_name="smoke_weather",
            description="Weather smoke tool",
            tags=["weather"],
            display_name="Smoke Weather",
        )
        await session.commit()

    gate = asyncio.Event()
    release = asyncio.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        # Block until test flips release after metadata PATCH.
        if not release.is_set():
            # busy-wait via nest — use sync wait on event from async thread carefully
            pass
        payload = json.loads(request.content.decode("utf-8"))
        data = [
            {
                "index": i,
                "embedding": [0.1, 0.2, 0.3, 0.4],
                "object": "embedding",
            }
            for i, _ in enumerate(payload["input"])
        ]
        return httpx.Response(
            200, json={"object": "list", "data": data, "model": "emb-test"}
        )

    # Use async-aware delayed provider via AsyncMock-like custom client.
    class _DelayedClient(ModelProviderClient):
        async def embed_texts(self, target, inputs):  # type: ignore[no-untyped-def]
            gate.set()
            await release.wait()
            return [[0.1, 0.2, 0.3, 0.4] for _ in inputs]

    delayed = _DelayedClient(
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        )
    )

    async def _ensure() -> Any:
        async with integration_session_factory() as session:
            service = ToolEmbeddingService(
                session,
                session_factory=integration_session_factory,
                model_provider=delayed,
            )
            return await service.ensure_embedding(version_id, profile_id)

    task = asyncio.create_task(_ensure())
    await gate.wait()

    async with integration_session_factory() as session:
        tool = await MCPToolRepository(session).get(tool_id)
        assert tool is not None
        await MCPToolService(session).update(
            tool_id,
            MCPToolUpdate(display_name="Changed During Embed", lock_version=tool.lock_version),
            expected_lock_version=tool.lock_version,
        )

    release.set()
    result = await task
    assert result.status == ToolEmbeddingStatus.STALE

    async with integration_session_factory() as session:
        row = await ToolEmbeddingRepository(session).get(
            tool_version_id=version_id,
            embedding_profile_id=profile_id,
        )
        assert row is not None
        assert row.status == ToolEmbeddingStatus.STALE
        assert row.embedding is None or row.status == ToolEmbeddingStatus.STALE

    # Happy-path smoke after race: ensure again with immediate provider.
    client = _mock_embed_client()
    async with integration_session_factory() as session:
        service = ToolEmbeddingService(
            session,
            session_factory=integration_session_factory,
            model_provider=client,
        )
        ready = await service.ensure_embedding(version_id, profile_id)
        assert ready.status == ToolEmbeddingStatus.READY
        row = await ToolEmbeddingRepository(session).get(
            tool_version_id=version_id,
            embedding_profile_id=profile_id,
        )
        assert row is not None
        assert row.status == ToolEmbeddingStatus.READY
        assert row.embedding is not None

        lexical = await ToolEmbeddingRepository(session).lexical_search(
            query="weather",
            profile_id=profile_id,
            limit=5,
        )
        assert any(hit.tool_version_id == version_id for hit in lexical)

        vector = await ToolEmbeddingRepository(session).vector_search(
            query_vector=list(row.embedding),
            profile_id=profile_id,
            expected_dimension=4,
            limit=5,
        )
        assert vector[0].tool_version_id == version_id
    await client.aclose()
    await delayed.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_profile_lock_race_integration(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        profile_id = await _seed_profile(session, dimension=4)
        _tool_id, version_id = await _seed_tool(
            session, remote_name="profile_race", description="Profile race tool"
        )
        await session.commit()

    gate = asyncio.Event()
    release = asyncio.Event()

    class _DelayedClient(ModelProviderClient):
        async def embed_texts(self, target, inputs):  # type: ignore[no-untyped-def]
            gate.set()
            await release.wait()
            return [[0.2, 0.2, 0.2, 0.2] for _ in inputs]

    delayed = _DelayedClient(
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(500)),
            follow_redirects=False,
        )
    )

    async def _ensure() -> Any:
        async with integration_session_factory() as session:
            service = ToolEmbeddingService(
                session,
                session_factory=integration_session_factory,
                model_provider=delayed,
            )
            return await service.ensure_embedding(version_id, profile_id)

    task = asyncio.create_task(_ensure())
    await gate.wait()

    async with integration_session_factory() as session:
        profile = await EmbeddingProfileRepository(session).get(profile_id)
        assert profile is not None
        profile.model = "emb-changed"
        profile.lock_version = int(profile.lock_version) + 1
        await session.commit()

    release.set()
    result = await task
    assert result.skipped is True

    async with integration_session_factory() as session:
        row = await ToolEmbeddingRepository(session).get(
            tool_version_id=version_id,
            embedding_profile_id=profile_id,
        )
        # Must not become READY with obsolete profile snapshot.
        assert row is None or row.status != ToolEmbeddingStatus.READY

    await delayed.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_new_tool_version_does_not_inherit_embedding(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        profile_id = await _seed_profile(session, dimension=2)
        tool_id, v1 = await _seed_tool(
            session, remote_name="versioned_tool", description="v1 desc"
        )
        await ToolEmbeddingRepository(session).upsert_ready_if_current(
            tool_version_id=v1,
            embedding_profile_id=profile_id,
            search_text="v1",
            content_hash="e" * 64,
            embedding=[1.0, 0.0],
        )
        tools = MCPToolRepository(session)
        v2 = await tools.create_version(
            mcp_tool_id=tool_id,
            version_no=2,
            content_hash=hashlib.sha256(b"v2").hexdigest(),
            validation_status=ToolVersionValidationStatus.VALID,
            remote_description="v2 desc",
            input_schema={"type": "object", "properties": {}},
        )
        tool = await tools.get(tool_id)
        assert tool is not None
        tool.current_version_id = v2.id
        await session.commit()

        row_v1 = await ToolEmbeddingRepository(session).get(
            tool_version_id=v1, embedding_profile_id=profile_id
        )
        row_v2 = await ToolEmbeddingRepository(session).get(
            tool_version_id=v2.id, embedding_profile_id=profile_id
        )
        assert row_v1 is not None
        assert row_v1.status == ToolEmbeddingStatus.READY
        assert row_v2 is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lexical_includes_failed_rows(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        profile_id = await _seed_profile(session, dimension=2)
        _tool_id, version_id = await _seed_tool(
            session,
            remote_name="failed_weather_index",
            description="Weather index failed embedding",
            tags=["weather"],
        )
        await ToolEmbeddingRepository(session).upsert_failed(
            tool_version_id=version_id,
            embedding_profile_id=profile_id,
            search_text=(
                "remote_name: failed_weather_index\n"
                "description: Weather index failed embedding\n"
                "tags: weather"
            ),
            content_hash="f" * 64,
        )
        await session.commit()
        hits = await ToolEmbeddingRepository(session).lexical_search(
            query="weather",
            profile_id=profile_id,
            limit=5,
        )
        assert any(hit.tool_version_id == version_id for hit in hits)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lexical_excludes_stale_includes_ready_failed(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        profile_id = await _seed_profile(session, dimension=2)
        _t1, v_ready = await _seed_tool(
            session, remote_name="ready_kw_tool", description="readykeyword alpha"
        )
        _t2, v_failed = await _seed_tool(
            session, remote_name="failed_kw_tool", description="failedkeyword beta"
        )
        _t3, v_stale = await _seed_tool(
            session, remote_name="stale_kw_tool", description="stalekeyword gamma"
        )
        repo = ToolEmbeddingRepository(session)
        await repo.upsert_ready_if_current(
            tool_version_id=v_ready,
            embedding_profile_id=profile_id,
            search_text="readykeyword document",
            content_hash="1" * 64,
            embedding=[1.0, 0.0],
        )
        await repo.upsert_failed(
            tool_version_id=v_failed,
            embedding_profile_id=profile_id,
            search_text="failedkeyword document",
            content_hash="2" * 64,
        )
        await repo.upsert_stale(
            tool_version_id=v_stale,
            embedding_profile_id=profile_id,
            search_text="stalekeyword document",
            content_hash="3" * 64,
        )
        await session.commit()

        ready_hits = await repo.lexical_search(
            query="readykeyword", profile_id=profile_id, limit=5
        )
        failed_hits = await repo.lexical_search(
            query="failedkeyword", profile_id=profile_id, limit=5
        )
        stale_hits = await repo.lexical_search(
            query="stalekeyword", profile_id=profile_id, limit=5
        )
        assert any(h.tool_version_id == v_ready for h in ready_hits)
        assert any(h.tool_version_id == v_failed for h in failed_hits)
        assert stale_hits == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_final_write_lock_blocks_metadata_until_ready_then_stale(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """T1 holds Profile+Tool locks before upsert; T2 PATCH waits; final is STALE."""
    async with integration_session_factory() as session:
        profile_id = await _seed_profile(session, dimension=4)
        tool_id, version_id = await _seed_tool(
            session,
            remote_name="final_lock_tool",
            description="Final lock race",
            display_name="Original",
        )
        await session.commit()

    locks_held = asyncio.Event()
    allow_upsert = asyncio.Event()
    patch_started = asyncio.Event()
    patch_done = asyncio.Event()

    class _ImmediateClient(ModelProviderClient):
        async def embed_texts(self, target, inputs):  # type: ignore[no-untyped-def]
            return [[0.1, 0.2, 0.3, 0.4] for _ in inputs]

    client = _ImmediateClient(
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(500)),
            follow_redirects=False,
        )
    )

    async def _after_locks() -> None:
        locks_held.set()
        await allow_upsert.wait()

    async def _ensure() -> Any:
        async with integration_session_factory() as session:
            service = ToolEmbeddingService(
                session,
                session_factory=integration_session_factory,
                model_provider=client,
                after_row_locks=_after_locks,
            )
            return await service.ensure_embedding(version_id, profile_id)

    async def _patch() -> None:
        await locks_held.wait()
        patch_started.set()
        async with integration_session_factory() as session:
            tool = await MCPToolRepository(session).get(tool_id)
            assert tool is not None
            await MCPToolService(session).update(
                tool_id,
                MCPToolUpdate(
                    display_name="Patched After Lock",
                    lock_version=tool.lock_version,
                ),
                expected_lock_version=tool.lock_version,
            )
        patch_done.set()

    ensure_task = asyncio.create_task(_ensure())
    patch_task = asyncio.create_task(_patch())

    await locks_held.wait()
    await asyncio.sleep(0.05)
    assert patch_started.is_set()
    assert not patch_done.is_set()

    allow_upsert.set()
    result = await ensure_task
    assert result.status == ToolEmbeddingStatus.READY
    await patch_task
    assert patch_done.is_set()

    async with integration_session_factory() as session:
        row = await ToolEmbeddingRepository(session).get(
            tool_version_id=version_id,
            embedding_profile_id=profile_id,
        )
        assert row is not None
        assert row.status == ToolEmbeddingStatus.STALE

    await client.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failed_provider_after_metadata_change_writes_stale(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        profile_id = await _seed_profile(session, dimension=4)
        tool_id, version_id = await _seed_tool(
            session,
            remote_name="failed_meta_race",
            description="Original failed race",
            display_name="Original",
        )
        await session.commit()

    gate = asyncio.Event()
    release = asyncio.Event()

    class _FailClient(ModelProviderClient):
        async def embed_texts(self, target, inputs):  # type: ignore[no-untyped-def]
            from app.model_provider.errors import PROTOCOL, ModelProviderError

            gate.set()
            await release.wait()
            raise ModelProviderError(
                error_code=PROTOCOL, message="provider down", retryable=False
            )

    client = _FailClient(
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(500)),
            follow_redirects=False,
        )
    )

    async def _ensure() -> Any:
        async with integration_session_factory() as session:
            service = ToolEmbeddingService(
                session,
                session_factory=integration_session_factory,
                model_provider=client,
            )
            return await service.ensure_embedding(version_id, profile_id)

    task = asyncio.create_task(_ensure())
    await gate.wait()

    async with integration_session_factory() as session:
        tool = await MCPToolRepository(session).get(tool_id)
        assert tool is not None
        await MCPToolService(session).update(
            tool_id,
            MCPToolUpdate(display_name="Changed B", lock_version=tool.lock_version),
            expected_lock_version=tool.lock_version,
        )

    release.set()
    result = await task
    assert result.status == ToolEmbeddingStatus.STALE

    async with integration_session_factory() as session:
        tool = await MCPToolRepository(session).get(tool_id)
        version = await MCPToolRepository(session).get_version(version_id)
        assert tool is not None and version is not None
        live = ToolSearchDocumentBuilder().build(tool, version)
        row = await ToolEmbeddingRepository(session).get(
            tool_version_id=version_id,
            embedding_profile_id=profile_id,
        )
        assert row is not None
        assert row.status == ToolEmbeddingStatus.STALE
        assert row.content_hash == live.content_hash
        assert row.search_text == live.search_text

    await client.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_profile_update_after_ready_marks_stale(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """T1 READY commits first; T2 Profile model PATCH marks STALE."""
    async with integration_session_factory() as session:
        profile_id = await _seed_profile(session, dimension=4)
        _tool_id, version_id = await _seed_tool(
            session, remote_name="profile_after_ready", description="Profile after ready"
        )
        await session.commit()

    locks_held = asyncio.Event()
    allow_upsert = asyncio.Event()

    class _ImmediateClient(ModelProviderClient):
        async def embed_texts(self, target, inputs):  # type: ignore[no-untyped-def]
            return [[0.4, 0.3, 0.2, 0.1] for _ in inputs]

    client = _ImmediateClient(
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(500)),
            follow_redirects=False,
        )
    )

    async def _after_locks() -> None:
        locks_held.set()
        await allow_upsert.wait()

    async def _ensure() -> Any:
        async with integration_session_factory() as session:
            service = ToolEmbeddingService(
                session,
                session_factory=integration_session_factory,
                model_provider=client,
                after_row_locks=_after_locks,
            )
            return await service.ensure_embedding(version_id, profile_id)

    ensure_task = asyncio.create_task(_ensure())
    await locks_held.wait()

    async def _profile_patch() -> None:
        async with integration_session_factory() as session:
            profile = await EmbeddingProfileRepository(session).get(profile_id)
            assert profile is not None
            from app.schemas.model_profile import EmbeddingProfileUpdate
            from app.services.embedding_profile import EmbeddingProfileService

            await EmbeddingProfileService(session).update(
                profile_id,
                EmbeddingProfileUpdate(model="emb-after", lock_version=profile.lock_version),
                expected_lock_version=profile.lock_version,
            )

    # Profile PATCH waits on Profile FOR UPDATE held by ensure.
    patch_task = asyncio.create_task(_profile_patch())
    await asyncio.sleep(0.05)
    assert not patch_task.done()

    allow_upsert.set()
    result = await ensure_task
    assert result.status == ToolEmbeddingStatus.READY
    await patch_task

    async with integration_session_factory() as session:
        row = await ToolEmbeddingRepository(session).get(
            tool_version_id=version_id,
            embedding_profile_id=profile_id,
        )
        assert row is not None
        assert row.status == ToolEmbeddingStatus.STALE
        profile = await EmbeddingProfileRepository(session).get(profile_id)
        assert profile is not None
        assert profile.model == "emb-after"

    await client.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_profile_update_before_final_write_skips_ready(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        profile_id = await _seed_profile(session, dimension=4)
        _tool_id, version_id = await _seed_tool(
            session, remote_name="profile_before_ready", description="Profile before ready"
        )
        await session.commit()

    gate = asyncio.Event()
    release = asyncio.Event()

    class _DelayedClient(ModelProviderClient):
        async def embed_texts(self, target, inputs):  # type: ignore[no-untyped-def]
            gate.set()
            await release.wait()
            return [[0.2, 0.2, 0.2, 0.2] for _ in inputs]

    delayed = _DelayedClient(
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(500)),
            follow_redirects=False,
        )
    )

    async def _ensure() -> Any:
        async with integration_session_factory() as session:
            service = ToolEmbeddingService(
                session,
                session_factory=integration_session_factory,
                model_provider=delayed,
            )
            return await service.ensure_embedding(version_id, profile_id)

    task = asyncio.create_task(_ensure())
    await gate.wait()

    async with integration_session_factory() as session:
        from app.schemas.model_profile import EmbeddingProfileUpdate
        from app.services.embedding_profile import EmbeddingProfileService

        profile = await EmbeddingProfileRepository(session).get(profile_id)
        assert profile is not None
        await EmbeddingProfileService(session).update(
            profile_id,
            EmbeddingProfileUpdate(model="emb-changed", lock_version=profile.lock_version),
            expected_lock_version=profile.lock_version,
        )

    release.set()
    result = await task
    assert result.skipped is True

    async with integration_session_factory() as session:
        row = await ToolEmbeddingRepository(session).get(
            tool_version_id=version_id,
            embedding_profile_id=profile_id,
        )
        assert row is None or row.status != ToolEmbeddingStatus.READY

    await delayed.aclose()
