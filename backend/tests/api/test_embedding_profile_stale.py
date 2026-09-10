"""API regression: EmbeddingProfile semantic PATCH marks tool_embeddings STALE."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from app.domain.enums import ToolEmbeddingStatus
from app.models.mcp import ToolEmbedding
from app.models.model_profile import EmbeddingProfile
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.api.test_mcp_tools_lifecycle import _discover_tools

EMB_API = "/api/v1/model-profiles/embeddings"
API_TOOLS = "/api/v1/mcp/tools"


@pytest.fixture
async def db_client(authenticated_db_client):
    return authenticated_db_client


async def _seed_ready_embedding(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    version_id: uuid.UUID,
    profile: EmbeddingProfile,
) -> uuid.UUID:
    async with session_factory() as session:
        session.add(profile)
        session.add(
            ToolEmbedding(
                id=uuid.uuid4(),
                mcp_tool_version_id=version_id,
                embedding_profile_id=profile.id,
                search_text="remote_name: echo",
                search_tsv="echo",
                embedding=None,
                content_hash="c" * 64,
                status=ToolEmbeddingStatus.READY,
            )
        )
        await session.commit()
        return profile.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch_body",
    [
        {"model": "emb-changed"},
        {"base_url": "https://llm.other.test/v1"},
        {"credential_secret_id": str(uuid.uuid4())},
    ],
)
async def test_embedding_profile_semantic_patch_marks_stale(
    db_client: AsyncClient,
    override_mcp_client,
    db_session_factory: async_sessionmaker[AsyncSession],
    patch_body: dict,
) -> None:
    _server, tool = await _discover_tools(db_client, override_mcp_client)
    version_id = uuid.UUID(tool["current_version_id"])

    created = await db_client.post(
        EMB_API,
        json={
            "name": f"Stale Profile {uuid.uuid4().hex[:6]}",
            "provider": "OPENAI_COMPATIBLE",
            "model": "emb-original",
            "base_url": "https://llm.test/v1",
            "dimension": 8,
            "distance_metric": "cosine",
        },
    )
    assert created.status_code == 201, created.text
    profile_body = created.json()
    profile_id = uuid.UUID(profile_body["id"])

    async with db_session_factory() as session:
        session.add(
            ToolEmbedding(
                id=uuid.uuid4(),
                mcp_tool_version_id=version_id,
                embedding_profile_id=profile_id,
                search_text="remote_name: echo",
                search_tsv="echo",
                embedding=None,
                content_hash="c" * 64,
                status=ToolEmbeddingStatus.READY,
            )
        )
        await session.commit()

    patched = await db_client.patch(
        f"{EMB_API}/{profile_id}",
        headers={"If-Match": str(profile_body["lock_version"])},
        json={**patch_body, "lock_version": profile_body["lock_version"]},
    )
    assert patched.status_code == 200, patched.text

    async with db_session_factory() as session:
        row = (
            await session.execute(
                select(ToolEmbedding).where(
                    ToolEmbedding.mcp_tool_version_id == version_id,
                    ToolEmbedding.embedding_profile_id == profile_id,
                )
            )
        ).scalar_one()
        assert row.status == ToolEmbeddingStatus.STALE


@pytest.mark.asyncio
async def test_inactive_dimension_patch_marks_stale(
    db_client: AsyncClient,
    override_mcp_client,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _server, tool = await _discover_tools(db_client, override_mcp_client)
    version_id = uuid.UUID(tool["current_version_id"])
    created = await db_client.post(
        EMB_API,
        json={
            "name": f"Dim Profile {uuid.uuid4().hex[:6]}",
            "provider": "OPENAI_COMPATIBLE",
            "model": "emb",
            "base_url": "https://llm.test/v1",
            "dimension": 8,
            "distance_metric": "cosine",
        },
    )
    assert created.status_code == 201
    profile_body = created.json()
    profile_id = uuid.UUID(profile_body["id"])

    async with db_session_factory() as session:
        session.add(
            ToolEmbedding(
                id=uuid.uuid4(),
                mcp_tool_version_id=version_id,
                embedding_profile_id=profile_id,
                search_text="x",
                search_tsv="x",
                embedding=None,
                content_hash="d" * 64,
                status=ToolEmbeddingStatus.READY,
            )
        )
        await session.commit()

    patched = await db_client.patch(
        f"{EMB_API}/{profile_id}",
        headers={"If-Match": str(profile_body["lock_version"])},
        json={"dimension": 16, "lock_version": profile_body["lock_version"]},
    )
    assert patched.status_code == 200, patched.text

    async with db_session_factory() as session:
        row = (
            await session.execute(
                select(ToolEmbedding).where(
                    ToolEmbedding.embedding_profile_id == profile_id
                )
            )
        ).scalar_one()
        assert row.status == ToolEmbeddingStatus.STALE


@pytest.mark.asyncio
async def test_profile_name_only_keeps_ready(
    db_client: AsyncClient,
    override_mcp_client,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _server, tool = await _discover_tools(db_client, override_mcp_client)
    version_id = uuid.UUID(tool["current_version_id"])
    created = await db_client.post(
        EMB_API,
        json={
            "name": f"Name Profile {uuid.uuid4().hex[:6]}",
            "provider": "OPENAI_COMPATIBLE",
            "model": "emb",
            "base_url": "https://llm.test/v1",
            "dimension": 8,
            "distance_metric": "cosine",
        },
    )
    profile_body = created.json()
    profile_id = uuid.UUID(profile_body["id"])

    async with db_session_factory() as session:
        session.add(
            ToolEmbedding(
                id=uuid.uuid4(),
                mcp_tool_version_id=version_id,
                embedding_profile_id=profile_id,
                search_text="x",
                search_tsv="x",
                embedding=None,
                content_hash="e" * 64,
                status=ToolEmbeddingStatus.READY,
            )
        )
        await session.commit()

    patched = await db_client.patch(
        f"{EMB_API}/{profile_id}",
        headers={"If-Match": str(profile_body["lock_version"])},
        json={"name": "Renamed Only", "lock_version": profile_body["lock_version"]},
    )
    assert patched.status_code == 200

    async with db_session_factory() as session:
        row = (
            await session.execute(
                select(ToolEmbedding).where(
                    ToolEmbedding.embedding_profile_id == profile_id
                )
            )
        ).scalar_one()
        assert row.status == ToolEmbeddingStatus.READY


@pytest.mark.asyncio
async def test_stale_if_match_leaves_embeddings_unchanged(
    db_client: AsyncClient,
    override_mcp_client,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _server, tool = await _discover_tools(db_client, override_mcp_client)
    version_id = uuid.UUID(tool["current_version_id"])
    created = await db_client.post(
        EMB_API,
        json={
            "name": f"Conflict Profile {uuid.uuid4().hex[:6]}",
            "provider": "OPENAI_COMPATIBLE",
            "model": "emb",
            "base_url": "https://llm.test/v1",
            "dimension": 8,
            "distance_metric": "cosine",
        },
    )
    profile_body = created.json()
    profile_id = uuid.UUID(profile_body["id"])

    async with db_session_factory() as session:
        session.add(
            ToolEmbedding(
                id=uuid.uuid4(),
                mcp_tool_version_id=version_id,
                embedding_profile_id=profile_id,
                search_text="x",
                search_tsv="x",
                embedding=None,
                content_hash="f" * 64,
                status=ToolEmbeddingStatus.READY,
            )
        )
        await session.commit()

    conflict = await db_client.patch(
        f"{EMB_API}/{profile_id}",
        headers={"If-Match": "999"},
        json={"model": "should-not-apply", "lock_version": 999},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "RESOURCE_VERSION_CONFLICT"

    detail = await db_client.get(f"{EMB_API}/{profile_id}")
    assert detail.json()["model"] == "emb"
    assert detail.json()["lock_version"] == profile_body["lock_version"]

    async with db_session_factory() as session:
        row = (
            await session.execute(
                select(ToolEmbedding).where(
                    ToolEmbedding.embedding_profile_id == profile_id
                )
            )
        ).scalar_one()
        assert row.status == ToolEmbeddingStatus.READY


@pytest.mark.asyncio
async def test_credential_clear_marks_stale(
    db_client: AsyncClient,
    override_mcp_client,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _server, tool = await _discover_tools(db_client, override_mcp_client)
    version_id = uuid.UUID(tool["current_version_id"])
    secret_id = str(uuid.uuid4())
    created = await db_client.post(
        EMB_API,
        json={
            "name": f"Cred Profile {uuid.uuid4().hex[:6]}",
            "provider": "OPENAI_COMPATIBLE",
            "model": "emb",
            "base_url": "https://llm.test/v1",
            "dimension": 8,
            "distance_metric": "cosine",
            "credential_secret_id": secret_id,
        },
    )
    assert created.status_code == 201, created.text
    profile_body = created.json()
    profile_id = uuid.UUID(profile_body["id"])

    async with db_session_factory() as session:
        session.add(
            ToolEmbedding(
                id=uuid.uuid4(),
                mcp_tool_version_id=version_id,
                embedding_profile_id=profile_id,
                search_text="x",
                search_tsv="x",
                embedding=None,
                content_hash="a" * 64,
                status=ToolEmbeddingStatus.READY,
            )
        )
        await session.commit()

    patched = await db_client.patch(
        f"{EMB_API}/{profile_id}",
        headers={"If-Match": str(profile_body["lock_version"])},
        json={"credential_secret_id": None, "lock_version": profile_body["lock_version"]},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["credential_secret_id"] is None

    async with db_session_factory() as session:
        row = (
            await session.execute(
                select(ToolEmbedding).where(
                    ToolEmbedding.embedding_profile_id == profile_id
                )
            )
        ).scalar_one()
        assert row.status == ToolEmbeddingStatus.STALE


@pytest.mark.asyncio
async def test_tool_tag_casefold_noop_keeps_ready(
    db_client: AsyncClient,
    override_mcp_client,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _server, tool = await _discover_tools(db_client, override_mcp_client)
    tool_id = uuid.UUID(tool["id"])
    version_id = uuid.UUID(tool["current_version_id"])

    first = await db_client.patch(
        f"{API_TOOLS}/{tool_id}",
        headers={"If-Match": str(tool["lock_version"])},
        json={"tags": ["Weather", "Ops"]},
    )
    assert first.status_code == 200
    lock = first.json()["lock_version"]

    profile = EmbeddingProfile(
        id=uuid.uuid4(),
        code=f"emb-{uuid.uuid4().hex[:8]}",
        name="Tag Noop",
        provider="OPENAI_COMPATIBLE",
        model="emb",
        base_url="https://llm.test/v1",
        dimension=8,
        distance_metric="cosine",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        lock_version=1,
    )
    await _seed_ready_embedding(db_session_factory, version_id=version_id, profile=profile)

    second = await db_client.patch(
        f"{API_TOOLS}/{tool_id}",
        headers={"If-Match": str(lock)},
        json={"tags": ["weather", "Weather", "Ops"]},
    )
    assert second.status_code == 200

    async with db_session_factory() as session:
        row = (
            await session.execute(
                select(ToolEmbedding).where(
                    ToolEmbedding.embedding_profile_id == profile.id
                )
            )
        ).scalar_one()
        assert row.status == ToolEmbeddingStatus.READY
