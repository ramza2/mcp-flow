"""Unit tests: ToolEmbedding ensure Outbox scheduling on activate / profile switch."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.domain.enums import MCPToolStatus, ToolEmbeddingStatus, ToolVersionValidationStatus
from app.execution.queue import (
    OutboxRelayService,
    validate_tool_embedding_ensure_event,
)
from app.models.mcp import ToolEmbedding
from app.models.outbox import OutboxEvent
from app.repositories.embedding_profile import EmbeddingProfileRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.outbox import OutboxRepository
from app.search.tool_embedding_recovery import ToolEmbeddingRecoveryService
from app.search.tool_embedding_schedule import (
    enqueue_rebuild_for_profile,
    enqueue_tool_embedding_ensure,
)
from app.schemas.mcp_tool import MCPToolUpdate
from app.services.embedding_profile import EmbeddingProfileService
from app.services.mcp_tool import MCPToolService
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def _add_embedding(
    session: AsyncSession,
    *,
    version_id: uuid.UUID,
    profile_id: uuid.UUID,
    status: str,
    content_hash: str,
) -> None:
    session.add(
        ToolEmbedding(
            id=uuid.uuid4(),
            mcp_tool_version_id=version_id,
            embedding_profile_id=profile_id,
            search_text="remote_name: pets",
            search_tsv="pets",
            embedding=None,
            content_hash=content_hash,
            status=status,
        )
    )


async def _seed_tool(
    session: AsyncSession,
    *,
    status: str = MCPToolStatus.DISCOVERED,
) -> tuple[uuid.UUID, uuid.UUID, int]:
    server = await MCPServerRepository(session).create(
        code=f"srv-{uuid.uuid4().hex[:8]}",
        name="Emb Auto Index Server",
        transport_type="STREAMABLE_HTTP",
        endpoint_url="https://mcp.test/mcp",
    )
    tools = MCPToolRepository(session)
    tool = await tools.create_tool(
        mcp_server_id=server.id,
        remote_name=f"pets.get.{uuid.uuid4().hex[:6]}",
        display_name="GET /pets",
        tags=["pets"],
        status=status,
    )
    version = await tools.create_version(
        mcp_tool_id=tool.id,
        version_no=1,
        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        validation_status=ToolVersionValidationStatus.VALID,
        remote_description="List pets",
        input_schema={"type": "object", "properties": {}},
        output_schema=None,
    )
    tool.current_version_id = version.id
    await session.flush()
    return tool.id, version.id, int(tool.lock_version)


async def _seed_active_profile(session: AsyncSession) -> uuid.UUID:
    profile = await EmbeddingProfileRepository(session).create(
        code=f"emb-{uuid.uuid4().hex[:8]}",
        name="Active Emb",
        provider="OPENAI_COMPATIBLE",
        model="emb",
        base_url="https://llm.test/v1",
        dimension=4,
        distance_metric="cosine",
        is_active_for_tools=True,
    )
    await session.flush()
    return profile.id


class RecordingPublisher:
    def __init__(self) -> None:
        self.embedding_calls: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = []

    def publish_execution(self, **_k: Any) -> None:
        raise AssertionError("execution unexpected")

    def publish_approval_resume(self, **_k: Any) -> None:
        raise AssertionError("approval unexpected")

    def publish_mrtr_resume(self, **_k: Any) -> None:
        raise AssertionError("mrtr unexpected")

    def publish_factory_job(self, **_k: Any) -> None:
        raise AssertionError("factory unexpected")

    def publish_factory_test_run(self, **_k: Any) -> None:
        raise AssertionError("factory test unexpected")

    def publish_tool_embedding_ensure(
        self,
        *,
        tool_version_id: uuid.UUID,
        embedding_profile_id: uuid.UUID,
        outbox_event_id: uuid.UUID,
    ) -> None:
        self.embedding_calls.append(
            (tool_version_id, embedding_profile_id, outbox_event_id)
        )


@pytest.mark.asyncio
async def test_activate_enqueues_ensure_for_active_profile(
    db_session: AsyncSession,
) -> None:
    tool_id, version_id, lock = await _seed_tool(db_session)
    profile_id = await _seed_active_profile(db_session)
    await db_session.commit()

    updated = await MCPToolService(db_session).activate(
        tool_id, expected_lock_version=lock
    )
    assert updated.status == MCPToolStatus.ACTIVE

    outbox = (
        await db_session.execute(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "TOOL_EMBEDDING_ENSURE",
                OutboxEvent.aggregate_id == version_id,
            )
        )
    ).scalar_one()
    assert outbox.published_at is None
    assert outbox.payload == {
        "tool_version_id": str(version_id),
        "embedding_profile_id": str(profile_id),
    }
    validate_tool_embedding_ensure_event(outbox)


@pytest.mark.asyncio
async def test_activate_skips_outbox_without_active_profile(
    db_session: AsyncSession,
) -> None:
    tool_id, version_id, lock = await _seed_tool(db_session)
    await db_session.commit()

    await MCPToolService(db_session).activate(tool_id, expected_lock_version=lock)

    count = (
        await db_session.execute(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "TOOL_EMBEDDING_ENSURE",
                OutboxEvent.aggregate_id == version_id,
            )
        )
    ).scalars().all()
    assert count == []


@pytest.mark.asyncio
async def test_duplicate_unpublished_ensure_is_not_recreated(
    db_session: AsyncSession,
) -> None:
    _tool_id, version_id, _lock = await _seed_tool(db_session)
    profile_id = await _seed_active_profile(db_session)
    await db_session.commit()

    first = await enqueue_tool_embedding_ensure(
        db_session,
        tool_version_id=version_id,
        embedding_profile_id=profile_id,
    )
    second = await enqueue_tool_embedding_ensure(
        db_session,
        tool_version_id=version_id,
        embedding_profile_id=profile_id,
    )
    await db_session.commit()
    assert first is not None
    assert second is not None
    assert first.id == second.id

    rows = (
        await db_session.execute(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "TOOL_EMBEDDING_ENSURE",
                OutboxEvent.aggregate_id == version_id,
            )
        )
    ).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_profile_activate_enqueues_missing_and_stale(
    db_session: AsyncSession,
) -> None:
    tool_id, version_id, lock = await _seed_tool(
        db_session, status=MCPToolStatus.INACTIVE
    )
    # Activate tool first without a profile (no outbox).
    await MCPToolService(db_session).activate(tool_id, expected_lock_version=lock)

    profile = await EmbeddingProfileRepository(db_session).create(
        code=f"emb-{uuid.uuid4().hex[:8]}",
        name="Soon Active",
        provider="OPENAI_COMPATIBLE",
        model="emb",
        base_url="https://llm.test/v1",
        dimension=4,
        distance_metric="cosine",
        is_active_for_tools=False,
    )
    await db_session.flush()
    _add_embedding(
        db_session,
        version_id=version_id,
        profile_id=profile.id,
        status=ToolEmbeddingStatus.STALE,
        content_hash="a" * 64,
    )
    await db_session.commit()

    activated = await EmbeddingProfileService(db_session).activate_for_tools(
        profile.id, expected_lock_version=int(profile.lock_version)
    )
    assert activated.is_active_for_tools is True

    outbox = (
        await db_session.execute(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "TOOL_EMBEDDING_ENSURE",
                OutboxEvent.aggregate_id == version_id,
            )
        )
    ).scalar_one()
    assert outbox.payload["embedding_profile_id"] == str(profile.id)


@pytest.mark.asyncio
async def test_outbox_relay_publishes_tool_embedding_ensure(
    db_session: AsyncSession,
) -> None:
    _tool_id, version_id, _lock = await _seed_tool(db_session)
    profile_id = await _seed_active_profile(db_session)
    row = await enqueue_tool_embedding_ensure(
        db_session,
        tool_version_id=version_id,
        embedding_profile_id=profile_id,
    )
    await db_session.commit()
    assert row is not None

    publisher = RecordingPublisher()
    result = await OutboxRelayService(db_session).publish_batch(
        publisher=publisher, limit=10
    )
    await db_session.commit()

    assert result.published >= 1
    assert (version_id, profile_id, row.id) in publisher.embedding_calls
    refreshed = await OutboxRepository(db_session).get(row.id)
    assert refreshed is not None
    assert refreshed.published_at is not None


@pytest.mark.asyncio
async def test_recovery_skips_unpublished_and_fresh_failed(
    db_session: AsyncSession,
) -> None:
    _tool_id, version_id, _lock = await _seed_tool(db_session)
    profile_id = await _seed_active_profile(db_session)
    pending = await enqueue_tool_embedding_ensure(
        db_session,
        tool_version_id=version_id,
        embedding_profile_id=profile_id,
    )
    await db_session.commit()
    assert pending is not None

    # Unpublished twin must be skipped by recovery.
    recovered_profile, recoverable = await ToolEmbeddingRecoveryService(
        db_session
    ).list_recoverable_version_ids(limit=50, min_age_seconds=0)
    assert recovered_profile == profile_id
    assert version_id not in recoverable

    # Mark published so recovery can see the still-MISSING index.
    pending.published_at = datetime.now(UTC)
    await db_session.commit()

    recovered_profile, recoverable = await ToolEmbeddingRecoveryService(
        db_session
    ).list_recoverable_version_ids(limit=50, min_age_seconds=0)
    assert version_id in recoverable

    # Fresh FAILED is filtered by min_age_seconds.
    _add_embedding(
        db_session,
        version_id=version_id,
        profile_id=profile_id,
        status=ToolEmbeddingStatus.FAILED,
        content_hash="b" * 64,
    )
    await db_session.commit()

    _profile, recoverable_fresh = await ToolEmbeddingRecoveryService(
        db_session
    ).list_recoverable_version_ids(limit=50, min_age_seconds=3600)
    assert version_id not in recoverable_fresh

    # Age the FAILED row so recovery becomes eligible again.
    row = (
        await db_session.execute(
            select(ToolEmbedding).where(
                ToolEmbedding.mcp_tool_version_id == version_id,
                ToolEmbedding.embedding_profile_id == profile_id,
            )
        )
    ).scalar_one()
    row.updated_at = datetime.now(UTC) - timedelta(hours=2)
    await db_session.commit()

    _profile, recoverable_aged = await ToolEmbeddingRecoveryService(
        db_session
    ).list_recoverable_version_ids(limit=50, min_age_seconds=3600)
    assert version_id in recoverable_aged


@pytest.mark.asyncio
async def test_rebuild_enqueues_only_missing_or_stale(
    db_session: AsyncSession,
) -> None:
    _t1, ready_version, _ = await _seed_tool(db_session)
    _t2, stale_version, _ = await _seed_tool(db_session)
    _t3, missing_version, _ = await _seed_tool(db_session)
    profile_id = await _seed_active_profile(db_session)

    _add_embedding(
        db_session,
        version_id=ready_version,
        profile_id=profile_id,
        status=ToolEmbeddingStatus.READY,
        content_hash="c" * 64,
    )
    _add_embedding(
        db_session,
        version_id=stale_version,
        profile_id=profile_id,
        status=ToolEmbeddingStatus.STALE,
        content_hash="d" * 64,
    )
    await db_session.commit()

    enqueued = await enqueue_rebuild_for_profile(db_session, profile_id)
    await db_session.commit()
    assert enqueued == 2

    version_ids = {
        row.aggregate_id
        for row in (
            await db_session.execute(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == "TOOL_EMBEDDING_ENSURE"
                )
            )
        ).scalars().all()
    }
    assert ready_version not in version_ids
    assert stale_version in version_ids
    assert missing_version in version_ids


@pytest.mark.asyncio
async def test_metadata_patch_enqueues_ensure_when_active(
    db_session: AsyncSession,
) -> None:
    tool_id, version_id, lock = await _seed_tool(
        db_session, status=MCPToolStatus.INACTIVE
    )
    profile_id = await _seed_active_profile(db_session)
    activated = await MCPToolService(db_session).activate(
        tool_id, expected_lock_version=lock
    )
    await db_session.commit()

    # Clear outbox from activate so we only observe the patch enqueue.
    for row in (
        await db_session.execute(
            select(OutboxEvent).where(OutboxEvent.event_type == "TOOL_EMBEDDING_ENSURE")
        )
    ).scalars().all():
        await db_session.delete(row)
    await db_session.commit()

    await MCPToolService(db_session).update(
        tool_id,
        MCPToolUpdate(
            display_name="GET pets renamed",
            lock_version=int(activated.lock_version),
        ),
        expected_lock_version=int(activated.lock_version),
    )

    outbox = (
        await db_session.execute(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "TOOL_EMBEDDING_ENSURE",
                OutboxEvent.aggregate_id == version_id,
            )
        )
    ).scalar_one()
    assert outbox.payload["embedding_profile_id"] == str(profile_id)
