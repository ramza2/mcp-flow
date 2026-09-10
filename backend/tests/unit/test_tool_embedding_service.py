"""Service-level unit tests for ToolEmbeddingService races and verification neutrality."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.domain.enums import ToolEmbeddingStatus
from app.model_provider.errors import PROTOCOL, ModelProviderError
from app.models.mcp import MCPTool, MCPToolVersion
from app.models.model_profile import EmbeddingProfile
from app.repositories import embedding_profile as ep_mod
from app.repositories import mcp_tool as mt_mod
from app.repositories import tool_embedding as te_mod
from app.search.tool_document import ToolSearchDocumentBuilder
from app.search.tool_embedding import ToolEmbeddingService


def _tool(**overrides: Any) -> MCPTool:
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "mcp_server_id": uuid.uuid4(),
        "remote_name": "send_email",
        "display_name": "Send Email",
        "description_override": None,
        "tags": ["mail"],
        "status": "INACTIVE",
        "first_seen_at": now,
        "last_seen_at": now,
        "created_at": now,
        "updated_at": now,
        "lock_version": 1,
    }
    values.update(overrides)
    return MCPTool(**values)


def _version(tool_id: uuid.UUID, **overrides: Any) -> MCPToolVersion:
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "mcp_tool_id": tool_id,
        "version_no": 1,
        "remote_description": "Send an email",
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": None,
        "content_hash": "b" * 64,
        "validation_status": "VALID",
        "discovered_at": now,
        "created_at": now,
    }
    values.update(overrides)
    return MCPToolVersion(**values)


def _profile(**overrides: Any) -> EmbeddingProfile:
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "code": f"emb-{uuid.uuid4().hex[:8]}",
        "name": "Test Emb",
        "provider": "OPENAI_COMPATIBLE",
        "model": "emb",
        "base_url": "https://llm.test/v1",
        "dimension": 4,
        "distance_metric": "cosine",
        "credential_secret_id": None,
        "is_active_for_tools": True,
        "created_at": now,
        "updated_at": now,
        "lock_version": 1,
    }
    values.update(overrides)
    return EmbeddingProfile(**values)


class _SessionFactory:
    def __init__(self, session: Any) -> None:
        self._session = session

    def __call__(self) -> _SessionFactory:
        return self

    async def __aenter__(self) -> Any:
        return self._session

    async def __aexit__(self, *args: object) -> None:
        return None


def _patch_persist_repos(
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile: EmbeddingProfile,
    tool: MCPTool,
    version: MCPToolVersion,
    upsert_ready: AsyncMock | None = None,
    upsert_failed: AsyncMock | None = None,
    upsert_stale: AsyncMock | None = None,
    get_existing: Any = None,
) -> None:
    def _ep_init(self: Any, sess: Any) -> None:
        self._session = sess
        self.lock_for_update = AsyncMock(return_value=profile)
        self.get = AsyncMock(return_value=profile)

    def _mt_init(self: Any, sess: Any) -> None:
        self._session = sess
        self.lock_for_update = AsyncMock(return_value=tool)
        self.get = AsyncMock(return_value=tool)

    def _te_init(self: Any, sess: Any) -> None:
        self._session = sess
        self.get = AsyncMock(return_value=get_existing)
        self.upsert_ready_if_current = upsert_ready or AsyncMock(
            return_value=type(
                "Row",
                (),
                {
                    "status": ToolEmbeddingStatus.READY,
                    "content_hash": "x" * 64,
                },
            )()
        )
        self.upsert_failed = upsert_failed or AsyncMock(
            return_value=type(
                "Row",
                (),
                {
                    "status": ToolEmbeddingStatus.FAILED,
                    "content_hash": "x" * 64,
                },
            )()
        )
        self.upsert_stale = upsert_stale or AsyncMock(
            return_value=type(
                "Row",
                (),
                {
                    "status": ToolEmbeddingStatus.STALE,
                    "content_hash": "y" * 64,
                },
            )()
        )

    monkeypatch.setattr(ep_mod.EmbeddingProfileRepository, "__init__", _ep_init)
    monkeypatch.setattr(mt_mod.MCPToolRepository, "__init__", _mt_init)
    monkeypatch.setattr(te_mod.ToolEmbeddingRepository, "__init__", _te_init)

    async def _fake_execute(*_a: Any, **_k: Any) -> Any:
        return type("R", (), {"scalar_one_or_none": lambda self: version})()

    return _fake_execute


@pytest.mark.asyncio
async def test_ensure_skips_provider_when_ready_same_hash() -> None:
    tool = _tool()
    version = _version(tool.id)
    profile = _profile()
    doc = ToolSearchDocumentBuilder().build(tool, version)

    session = AsyncMock()
    session.commit = AsyncMock()
    service = ToolEmbeddingService(session, model_provider=AsyncMock())
    service._load_version_and_tool = AsyncMock(return_value=(version, tool))  # type: ignore[method-assign]
    service._profiles.get = AsyncMock(return_value=profile)  # type: ignore[method-assign]
    service._embeddings.get = AsyncMock(  # type: ignore[method-assign]
        return_value=type(
            "Row",
            (),
            {
                "status": ToolEmbeddingStatus.READY,
                "content_hash": doc.content_hash,
                "embedding": [0.1, 0.2, 0.3, 0.4],
            },
        )()
    )
    service._provider.embed_texts = AsyncMock(side_effect=AssertionError("should skip"))

    result = await service.ensure_embedding(version.id, profile.id)
    assert result.skipped is True
    assert result.status == ToolEmbeddingStatus.READY
    service._provider.embed_texts.assert_not_called()


@pytest.mark.asyncio
async def test_metadata_race_refuses_obsolete_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _tool(display_name="Original")
    version = _version(tool.id)
    profile = _profile()
    builder = ToolSearchDocumentBuilder()
    original_doc = builder.build(tool, version)

    provider = AsyncMock()
    gate = asyncio.Event()

    async def _embed(_target: Any, _inputs: list[str]) -> list[list[float]]:
        await gate.wait()
        return [[0.25, 0.25, 0.25, 0.25]]

    provider.embed_texts = AsyncMock(side_effect=_embed)
    provider.aclose = AsyncMock()

    session = AsyncMock()
    session.commit = AsyncMock()
    write_session = AsyncMock()
    write_session.commit = AsyncMock()

    patched = _tool(
        id=tool.id,
        mcp_server_id=tool.mcp_server_id,
        display_name="Patched",
        tags=tool.tags,
    )
    upsert_stale = AsyncMock(
        return_value=type(
            "Row",
            (),
            {
                "status": ToolEmbeddingStatus.STALE,
                "content_hash": builder.build(patched, version).content_hash,
            },
        )()
    )
    fake_execute = _patch_persist_repos(
        monkeypatch,
        profile=profile,
        tool=patched,
        version=version,
        upsert_stale=upsert_stale,
    )
    write_session.execute = AsyncMock(side_effect=fake_execute)

    service = ToolEmbeddingService(
        session,
        session_factory=_SessionFactory(write_session),  # type: ignore[arg-type]
        model_provider=provider,
        document_builder=builder,
    )
    service._load_version_and_tool = AsyncMock(return_value=(version, tool))  # type: ignore[method-assign]
    service._profiles.get = AsyncMock(return_value=profile)  # type: ignore[method-assign]
    service._embeddings.get = AsyncMock(return_value=None)  # type: ignore[method-assign]

    task = asyncio.create_task(service.ensure_embedding(version.id, profile.id))
    await asyncio.sleep(0)
    gate.set()
    result = await task
    assert result.status == ToolEmbeddingStatus.STALE
    assert result.content_hash != original_doc.content_hash
    assert result.skipped is True
    upsert_stale.assert_awaited()


@pytest.mark.asyncio
async def test_profile_lock_race_skips_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _tool()
    version = _version(tool.id)
    profile = _profile(lock_version=1)

    provider = AsyncMock()
    provider.embed_texts = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4]])
    provider.aclose = AsyncMock()

    session = AsyncMock()
    session.commit = AsyncMock()
    write_session = AsyncMock()
    write_session.commit = AsyncMock()
    write_session.execute = AsyncMock()

    service = ToolEmbeddingService(
        session,
        session_factory=_SessionFactory(write_session),  # type: ignore[arg-type]
        model_provider=provider,
    )
    service._load_version_and_tool = AsyncMock(return_value=(version, tool))  # type: ignore[method-assign]
    service._profiles.get = AsyncMock(return_value=profile)  # type: ignore[method-assign]
    service._embeddings.get = AsyncMock(return_value=None)  # type: ignore[method-assign]

    def _ep_init(self: Any, sess: Any) -> None:
        self._session = sess
        # Changed lock_version — refuse READY.
        self.lock_for_update = AsyncMock(return_value=None)

    def _te_init(self: Any, sess: Any) -> None:
        self._session = sess
        self.get = AsyncMock(return_value=None)

    monkeypatch.setattr(ep_mod.EmbeddingProfileRepository, "__init__", _ep_init)
    monkeypatch.setattr(te_mod.ToolEmbeddingRepository, "__init__", _te_init)

    result = await service.ensure_embedding(version.id, profile.id)
    assert result.skipped is True
    assert result.status == ToolEmbeddingStatus.STALE


@pytest.mark.asyncio
async def test_ensure_does_not_require_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _tool(status="MISSING")
    version = _version(tool.id, validation_status="INVALID")
    profile = _profile()
    doc = ToolSearchDocumentBuilder().build(tool, version)

    provider = AsyncMock()
    provider.embed_texts = AsyncMock(return_value=[[0.5, 0.5, 0.5, 0.5]])
    provider.aclose = AsyncMock()

    session = AsyncMock()
    session.commit = AsyncMock()
    write_session = AsyncMock()
    write_session.commit = AsyncMock()
    fake_execute = _patch_persist_repos(
        monkeypatch,
        profile=profile,
        tool=tool,
        version=version,
        upsert_ready=AsyncMock(
            return_value=type(
                "Row",
                (),
                {"status": ToolEmbeddingStatus.READY, "content_hash": doc.content_hash},
            )()
        ),
    )
    write_session.execute = AsyncMock(side_effect=fake_execute)

    service = ToolEmbeddingService(
        session,
        session_factory=_SessionFactory(write_session),  # type: ignore[arg-type]
        model_provider=provider,
    )
    service._load_version_and_tool = AsyncMock(return_value=(version, tool))  # type: ignore[method-assign]
    service._profiles.get = AsyncMock(return_value=profile)  # type: ignore[method-assign]
    service._embeddings.get = AsyncMock(return_value=None)  # type: ignore[method-assign]

    result = await service.ensure_embedding(version.id, profile.id)
    assert result.status == ToolEmbeddingStatus.READY
    provider.embed_texts.assert_awaited()


@pytest.mark.asyncio
async def test_provider_failure_upserts_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _tool()
    version = _version(tool.id)
    profile = _profile()
    doc = ToolSearchDocumentBuilder().build(tool, version)
    provider = AsyncMock()
    provider.embed_texts = AsyncMock(
        side_effect=ModelProviderError(error_code=PROTOCOL, message="bad", retryable=False)
    )
    provider.aclose = AsyncMock()

    session = AsyncMock()
    session.commit = AsyncMock()
    write_session = AsyncMock()
    write_session.commit = AsyncMock()
    upsert_failed = AsyncMock(
        return_value=type(
            "Row",
            (),
            {"status": ToolEmbeddingStatus.FAILED, "content_hash": doc.content_hash},
        )()
    )
    fake_execute = _patch_persist_repos(
        monkeypatch,
        profile=profile,
        tool=tool,
        version=version,
        upsert_failed=upsert_failed,
    )
    write_session.execute = AsyncMock(side_effect=fake_execute)

    service = ToolEmbeddingService(
        session,
        session_factory=_SessionFactory(write_session),  # type: ignore[arg-type]
        model_provider=provider,
    )
    service._load_version_and_tool = AsyncMock(return_value=(version, tool))  # type: ignore[method-assign]
    service._profiles.get = AsyncMock(return_value=profile)  # type: ignore[method-assign]
    service._embeddings.get = AsyncMock(return_value=None)  # type: ignore[method-assign]

    result = await service.ensure_embedding(version.id, profile.id)
    assert result.status == ToolEmbeddingStatus.FAILED
    upsert_failed.assert_awaited()


@pytest.mark.asyncio
async def test_failed_path_metadata_race_writes_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _tool(display_name="A")
    version = _version(tool.id)
    profile = _profile()
    builder = ToolSearchDocumentBuilder()
    patched = _tool(
        id=tool.id,
        mcp_server_id=tool.mcp_server_id,
        display_name="B",
        tags=tool.tags,
    )
    live = builder.build(patched, version)

    provider = AsyncMock()
    provider.embed_texts = AsyncMock(
        side_effect=ModelProviderError(error_code=PROTOCOL, message="boom", retryable=False)
    )
    provider.aclose = AsyncMock()

    session = AsyncMock()
    session.commit = AsyncMock()
    write_session = AsyncMock()
    write_session.commit = AsyncMock()
    upsert_stale = AsyncMock(
        return_value=type(
            "Row",
            (),
            {"status": ToolEmbeddingStatus.STALE, "content_hash": live.content_hash},
        )()
    )
    fake_execute = _patch_persist_repos(
        monkeypatch,
        profile=profile,
        tool=patched,
        version=version,
        upsert_stale=upsert_stale,
    )
    write_session.execute = AsyncMock(side_effect=fake_execute)

    service = ToolEmbeddingService(
        session,
        session_factory=_SessionFactory(write_session),  # type: ignore[arg-type]
        model_provider=provider,
        document_builder=builder,
    )
    service._load_version_and_tool = AsyncMock(return_value=(version, tool))  # type: ignore[method-assign]
    service._profiles.get = AsyncMock(return_value=profile)  # type: ignore[method-assign]
    service._embeddings.get = AsyncMock(return_value=None)  # type: ignore[method-assign]

    result = await service.ensure_embedding(version.id, profile.id)
    assert result.status == ToolEmbeddingStatus.STALE
    assert result.content_hash == live.content_hash
    upsert_stale.assert_awaited()
