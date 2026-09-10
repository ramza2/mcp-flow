"""ToolEmbeddingService — ensure / rebuild search index (docs/05 §8.6).

No public HTTP API. No Retrieval / RRF / authorization filters.
Outbound embedding HTTP never runs under an open SELECT FOR UPDATE.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError
from app.domain.enums import ToolEmbeddingStatus
from app.model_provider.client import EmbeddingConnectionTarget, ModelProviderClient
from app.model_provider.errors import DIMENSION_MISMATCH, PROTOCOL, ModelProviderError
from app.models.mcp import MCPTool, MCPToolVersion
from app.models.model_profile import EmbeddingProfile
from app.repositories.embedding_profile import EmbeddingProfileRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.tool_embedding import ToolEmbeddingRepository
from app.search.tool_document import ToolSearchDocument, ToolSearchDocumentBuilder

logger = logging.getLogger(__name__)

AfterLocksHook = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _ProfileSnapshot:
    id: uuid.UUID
    lock_version: int
    provider: str
    model: str
    base_url: str
    dimension: int
    credential_secret_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class EnsureEmbeddingResult:
    tool_version_id: uuid.UUID
    embedding_profile_id: uuid.UUID
    status: str
    content_hash: str
    skipped: bool = False


def _validate_embedding_vector(
    vector: list[float], *, expected_dimension: int
) -> list[float]:
    """Service-boundary defense for docs/05 dimension / numeric contract."""
    if not isinstance(vector, list) or len(vector) == 0:
        raise ModelProviderError(
            error_code=PROTOCOL,
            message="Embedding vector is missing or empty.",
            retryable=False,
        )
    parsed: list[float] = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ModelProviderError(
                error_code=PROTOCOL,
                message="Embedding vector contains non-numeric values.",
                retryable=False,
            )
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            raise ModelProviderError(
                error_code=PROTOCOL,
                message="Embedding vector contains NaN or Infinity.",
                retryable=False,
            )
        parsed.append(number)
    if len(parsed) != expected_dimension:
        raise ModelProviderError(
            error_code=DIMENSION_MISMATCH,
            message=(
                f"Embedding dimension mismatch: expected {expected_dimension}, "
                f"got {len(parsed)}."
            ),
            retryable=False,
        )
    return parsed


class ToolEmbeddingService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        model_provider: ModelProviderClient | None = None,
        document_builder: ToolSearchDocumentBuilder | None = None,
        after_row_locks: AfterLocksHook | None = None,
    ) -> None:
        self._session = session
        self._session_factory = session_factory
        self._provider = model_provider
        self._builder = document_builder or ToolSearchDocumentBuilder()
        self._after_row_locks = after_row_locks
        self._embeddings = ToolEmbeddingRepository(session)
        self._tools = MCPToolRepository(session)
        self._profiles = EmbeddingProfileRepository(session)

    async def _load_version_and_tool(
        self, session: AsyncSession, tool_version_id: uuid.UUID
    ) -> tuple[MCPToolVersion, MCPTool]:
        result = await session.execute(
            select(MCPToolVersion).where(MCPToolVersion.id == tool_version_id)
        )
        version = result.scalar_one_or_none()
        if version is None:
            raise AppError(
                code="NOT_FOUND",
                message="MCP tool version not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        tool = await MCPToolRepository(session).get(version.mcp_tool_id)
        if tool is None or tool.deleted_at is not None:
            raise AppError(
                code="NOT_FOUND",
                message="MCP tool not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return version, tool

    def _snapshot_profile(self, profile: EmbeddingProfile) -> _ProfileSnapshot:
        return _ProfileSnapshot(
            id=profile.id,
            lock_version=int(profile.lock_version),
            provider=profile.provider,
            model=profile.model,
            base_url=profile.base_url,
            dimension=int(profile.dimension),
            credential_secret_id=profile.credential_secret_id,
        )

    def _write_session(self):
        if self._session_factory is not None:
            return self._session_factory()

        class _Reuse:
            def __init__(self, session: AsyncSession) -> None:
                self._session = session

            async def __aenter__(self) -> AsyncSession:
                return self._session

            async def __aexit__(self, *args: object) -> None:
                return None

        return _Reuse(self._session)

    async def ensure_embedding(
        self,
        tool_version_id: uuid.UUID,
        embedding_profile_id: uuid.UUID,
    ) -> EnsureEmbeddingResult:
        version, tool = await self._load_version_and_tool(self._session, tool_version_id)
        profile = await self._profiles.get(embedding_profile_id)
        if profile is None:
            raise AppError(
                code="NOT_FOUND",
                message="Embedding profile not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        document = self._builder.build(tool, version)
        existing = await self._embeddings.get(
            tool_version_id=tool_version_id,
            embedding_profile_id=embedding_profile_id,
        )
        if (
            existing is not None
            and existing.status == ToolEmbeddingStatus.READY
            and existing.content_hash == document.content_hash
            and existing.embedding is not None
        ):
            return EnsureEmbeddingResult(
                tool_version_id=tool_version_id,
                embedding_profile_id=embedding_profile_id,
                status=ToolEmbeddingStatus.READY,
                content_hash=document.content_hash,
                skipped=True,
            )

        snapshot = self._snapshot_profile(profile)
        original_document = document
        tool_id = tool.id

        # Release any read transaction state before outbound HTTP.
        await self._session.commit()

        client = self._provider or ModelProviderClient()
        owns_client = self._provider is None
        vector: list[float] | None = None
        provider_error: str | None = None
        try:
            vectors = await client.embed_texts(
                EmbeddingConnectionTarget(
                    provider=snapshot.provider,
                    model=snapshot.model,
                    base_url=snapshot.base_url,
                    dimension=snapshot.dimension,
                    credential_secret_id=snapshot.credential_secret_id,
                ),
                [original_document.search_text],
            )
            if len(vectors) != 1:
                raise ModelProviderError(
                    error_code=PROTOCOL,
                    message=(
                        f"Embedding result count mismatch: expected 1, got {len(vectors)}."
                    ),
                    retryable=False,
                )
            vector = _validate_embedding_vector(
                vectors[0], expected_dimension=snapshot.dimension
            )
        except ModelProviderError as exc:
            provider_error = exc.error_code
            vector = None
            logger.info(
                "Tool embedding failed version=%s profile=%s code=%s",
                tool_version_id,
                embedding_profile_id,
                exc.error_code,
            )
        finally:
            if owns_client:
                await client.aclose()

        async with self._write_session() as session:
            return await self._persist_provider_outcome(
                session,
                tool_version_id=tool_version_id,
                tool_id=tool_id,
                snapshot=snapshot,
                original_document=original_document,
                vector=vector,
                provider_error=provider_error,
            )

    async def _persist_provider_outcome(
        self,
        session: AsyncSession,
        *,
        tool_version_id: uuid.UUID,
        tool_id: uuid.UUID,
        snapshot: _ProfileSnapshot,
        original_document: ToolSearchDocument,
        vector: list[float] | None,
        provider_error: str | None,
    ) -> EnsureEmbeddingResult:
        """Short write transaction: Profile + Tool FOR UPDATE, then READY/FAILED/STALE.

        Lock order is always EmbeddingProfile → MCPTool (then immutable ToolVersion read).
        """
        profiles = EmbeddingProfileRepository(session)
        tools = MCPToolRepository(session)
        embeddings = ToolEmbeddingRepository(session)

        # 1. EmbeddingProfile FOR UPDATE
        profile = await profiles.lock_for_update(snapshot.id)
        if profile is None or int(profile.lock_version) != snapshot.lock_version:
            logger.info(
                "Skipping provider outcome; embedding profile changed "
                "version=%s profile=%s",
                tool_version_id,
                snapshot.id,
            )
            existing = await embeddings.get(
                tool_version_id=tool_version_id,
                embedding_profile_id=snapshot.id,
            )
            await session.commit()
            return EnsureEmbeddingResult(
                tool_version_id=tool_version_id,
                embedding_profile_id=snapshot.id,
                status=(
                    existing.status if existing is not None else ToolEmbeddingStatus.STALE
                ),
                content_hash=original_document.content_hash,
                skipped=True,
            )

        # 2. MCPTool FOR UPDATE
        tool = await tools.lock_for_update(tool_id)
        if tool is None or tool.deleted_at is not None:
            await session.commit()
            raise AppError(
                code="NOT_FOUND",
                message="MCP tool not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        # 3. Immutable ToolVersion read
        version_result = await session.execute(
            select(MCPToolVersion).where(MCPToolVersion.id == tool_version_id)
        )
        version = version_result.scalar_one_or_none()
        if version is None:
            await session.commit()
            raise AppError(
                code="NOT_FOUND",
                message="MCP tool version not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        # Test hook: holds locks while another transaction attempts PATCH.
        if self._after_row_locks is not None:
            await self._after_row_locks()

        # 4. Recompute live search document under locks
        live_doc = self._builder.build(tool, version)
        if live_doc.content_hash != original_document.content_hash:
            row = await embeddings.upsert_stale(
                tool_version_id=tool_version_id,
                embedding_profile_id=snapshot.id,
                search_text=live_doc.search_text,
                content_hash=live_doc.content_hash,
            )
            await session.commit()
            return EnsureEmbeddingResult(
                tool_version_id=tool_version_id,
                embedding_profile_id=snapshot.id,
                status=ToolEmbeddingStatus.STALE,
                content_hash=row.content_hash,
                skipped=True,
            )

        # 5. Persist READY or FAILED for the still-current document
        if vector is not None:
            row = await embeddings.upsert_ready_if_current(
                tool_version_id=tool_version_id,
                embedding_profile_id=snapshot.id,
                search_text=original_document.search_text,
                content_hash=original_document.content_hash,
                embedding=vector,
            )
            await session.commit()
            assert row is not None
            return EnsureEmbeddingResult(
                tool_version_id=tool_version_id,
                embedding_profile_id=snapshot.id,
                status=ToolEmbeddingStatus.READY,
                content_hash=original_document.content_hash,
            )

        assert provider_error is not None
        row = await embeddings.upsert_failed(
            tool_version_id=tool_version_id,
            embedding_profile_id=snapshot.id,
            search_text=original_document.search_text,
            content_hash=original_document.content_hash,
        )
        await session.commit()
        return EnsureEmbeddingResult(
            tool_version_id=tool_version_id,
            embedding_profile_id=snapshot.id,
            status=ToolEmbeddingStatus.FAILED,
            content_hash=row.content_hash,
        )

    async def rebuild_profile(self, profile_id: uuid.UUID) -> list[EnsureEmbeddingResult]:
        profile = await self._profiles.get(profile_id)
        if profile is None:
            raise AppError(
                code="NOT_FOUND",
                message="Embedding profile not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        version_ids = await self._embeddings.list_missing_or_stale_for_profile(profile_id)
        await self._session.commit()
        results: list[EnsureEmbeddingResult] = []
        for version_id in version_ids:
            results.append(await self.ensure_embedding(version_id, profile_id))
        return results

    async def rebuild_active_profile(self) -> list[EnsureEmbeddingResult]:
        result = await self._session.execute(
            select(EmbeddingProfile).where(EmbeddingProfile.is_active_for_tools.is_(True))
        )
        profile = result.scalar_one_or_none()
        if profile is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="No active embedding profile for tools.",
                status_code=status.HTTP_409_CONFLICT,
            )
        profile_id = profile.id
        return await self.rebuild_profile(profile_id)
