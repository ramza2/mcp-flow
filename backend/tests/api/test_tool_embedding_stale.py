"""API regression: Tool metadata PATCH marks embeddings STALE (SQLite-safe)."""

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
from tests.fixtures.test_mcp_server import TestMCPScenario

API_TOOLS = "/api/v1/mcp/tools"


@pytest.fixture
async def db_client(authenticated_db_client):
    """Protected API tests use a real Session + CSRF (no auth bypass)."""
    return authenticated_db_client


@pytest.mark.asyncio
async def test_tool_patch_marks_embeddings_stale(
    db_client: AsyncClient,
    override_mcp_client,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _server, tool = await _discover_tools(
        db_client, override_mcp_client, scenario=TestMCPScenario.HEALTHY
    )
    tool_id = uuid.UUID(tool["id"])
    version_id = uuid.UUID(tool["current_version_id"])

    async with db_session_factory() as session:
        profile = EmbeddingProfile(
            id=uuid.uuid4(),
            code=f"emb-{uuid.uuid4().hex[:8]}",
            name="Index Profile",
            provider="OPENAI_COMPATIBLE",
            model="emb",
            base_url="https://llm.test/v1",
            dimension=8,
            distance_metric="cosine",
            is_active_for_tools=False,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            lock_version=1,
        )
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
        profile_id = profile.id

    patched = await db_client.patch(
        f"{API_TOOLS}/{tool_id}",
        headers={"If-Match": str(tool["lock_version"])},
        json={"display_name": "Echo Friendly Name", "lock_version": tool["lock_version"]},
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
async def test_tool_patch_noop_does_not_stale(
    db_client: AsyncClient,
    override_mcp_client,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _server, tool = await _discover_tools(db_client, override_mcp_client)
    tool_id = uuid.UUID(tool["id"])
    version_id = uuid.UUID(tool["current_version_id"])

    # First set display_name.
    first = await db_client.patch(
        f"{API_TOOLS}/{tool_id}",
        headers={"If-Match": str(tool["lock_version"])},
        json={"display_name": "Stable Name"},
    )
    assert first.status_code == 200
    lock = first.json()["lock_version"]

    async with db_session_factory() as session:
        profile = EmbeddingProfile(
            id=uuid.uuid4(),
            code=f"emb-{uuid.uuid4().hex[:8]}",
            name="Index Profile 2",
            provider="OPENAI_COMPATIBLE",
            model="emb",
            base_url="https://llm.test/v1",
            dimension=8,
            distance_metric="cosine",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            lock_version=1,
        )
        session.add(profile)
        session.add(
            ToolEmbedding(
                id=uuid.uuid4(),
                mcp_tool_version_id=version_id,
                embedding_profile_id=profile.id,
                search_text="remote_name: echo",
                search_tsv="echo",
                embedding=None,
                content_hash="d" * 64,
                status=ToolEmbeddingStatus.READY,
            )
        )
        await session.commit()
        profile_id = profile.id

    # Semantic no-op (same display_name after normalize).
    second = await db_client.patch(
        f"{API_TOOLS}/{tool_id}",
        headers={"If-Match": str(lock)},
        json={"display_name": "  Stable Name  "},
    )
    assert second.status_code == 200

    async with db_session_factory() as session:
        row = (
            await session.execute(
                select(ToolEmbedding).where(
                    ToolEmbedding.mcp_tool_version_id == version_id,
                    ToolEmbedding.embedding_profile_id == profile_id,
                )
            )
        ).scalar_one()
        assert row.status == ToolEmbeddingStatus.READY
