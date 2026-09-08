"""ToolEmbeddingService — ensure / rebuild search index (docs/05 §8.6).

No public HTTP API. No Retrieval / RRF / authorization filters.
Outbound embedding HTTP never runs under an open SELECT FOR UPDATE.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from fastapi import status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError
from app.domain.enums import ToolEmbeddingStatus
from app.model_provider.client import EmbeddingConnectionTarget, ModelProviderClient
from app.model_provider.errors import ModelProviderError
from app.models.mcp import MCPTool, MCPToolVersion
from app.models.model_profile import EmbeddingProfile
from app.repositories.embedding_profile import EmbeddingProfileRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.tool_embedding import ToolEmbeddingRepository
from app.search.tool_document import ToolSearchDocumentBuilder

logger = logging.getLogger(__name__)


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


class ToolEmbeddingService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        model_provider: ModelProviderClient | None = None,
        document_builder: ToolSearchDocumentBuilder | None = None,
    ) -> None:
        self._session = session
        self._session_factory = session_factory
        self._provider = model_provider
        self._builder = document_builder or ToolSearchDocumentBuilder()
        self._embeddings = ToolEmbeddingRepository(session)
        self._tools = MCPToolRepository(session)
        self._profiles = EmbeddingProfileRepository(session)

    async def _session_scope(self) -> AsyncSession:
        return self._session

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
        search_text = document.search_text
        content_hash = document.content_hash
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
                [search_text],
            )
            vector = vectors[0]
        except ModelProviderError as exc:
            provider_error = exc.error_code
            logger.info(
                "Tool embedding failed version=%s profile=%s code=%s",
                tool_version_id,
                embedding_profile_id,
                exc.error_code,
            )
        finally:
            if owns_client:
                await client.aclose()

        if vector is None:
            assert provider_error is not None
            async with self._write_session() as session:
                row = await ToolEmbeddingRepository(session).upsert_failed(
                    tool_version_id=tool_version_id,
                    embedding_profile_id=embedding_profile_id,
                    search_text=search_text,
                    content_hash=content_hash,
                )
                await session.commit()
                return EnsureEmbeddingResult(
                    tool_version_id=tool_version_id,
                    embedding_profile_id=embedding_profile_id,
                    status=row.status,
                    content_hash=row.content_hash,
                )

        async with self._write_session() as session:
            return await self._persist_ready(
                session,
                tool_version_id=tool_version_id,
                tool_id=tool_id,
                snapshot=snapshot,
                search_text=search_text,
                content_hash=content_hash,
                embedding=vector,
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

    async def _persist_ready(
        self,
        session: AsyncSession,
        *,
        tool_version_id: uuid.UUID,
        tool_id: uuid.UUID,
        snapshot: _ProfileSnapshot,
        search_text: str,
        content_hash: str,
        embedding: list[float],
    ) -> EnsureEmbeddingResult:
        profiles = EmbeddingProfileRepository(session)
        embeddings = ToolEmbeddingRepository(session)

        profile = await profiles.get(snapshot.id)
        if profile is None or int(profile.lock_version) != snapshot.lock_version:
            logger.info(
                "Skipping READY upsert; embedding profile changed version=%s profile=%s",
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
                content_hash=content_hash,
                skipped=True,
            )

        version, tool = await self._load_version_and_tool(session, tool_version_id)
        live_doc = self._builder.build(tool, version)
        if live_doc.content_hash != content_hash:
            # Metadata changed during provider call — do not store obsolete READY.
            await session.execute(
                text(
                    """
                    INSERT INTO tool_embeddings (
                      id, mcp_tool_version_id, embedding_profile_id,
                      search_text, search_tsv, embedding, content_hash, status
                    ) VALUES (
                      :id, :vid, :pid,
                      :search_text, to_tsvector('simple', :search_text),
                      NULL, :content_hash, 'STALE'
                    )
                    ON CONFLICT (mcp_tool_version_id, embedding_profile_id)
                    DO UPDATE SET
                      search_text = EXCLUDED.search_text,
                      search_tsv = EXCLUDED.search_tsv,
                      content_hash = EXCLUDED.content_hash,
                      status = 'STALE',
                      updated_at = now()
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "vid": tool_version_id,
                    "pid": snapshot.id,
                    "search_text": live_doc.search_text,
                    "content_hash": live_doc.content_hash,
                },
            )
            await session.commit()
            return EnsureEmbeddingResult(
                tool_version_id=tool_version_id,
                embedding_profile_id=snapshot.id,
                status=ToolEmbeddingStatus.STALE,
                content_hash=live_doc.content_hash,
                skipped=True,
            )

        row = await embeddings.upsert_ready_if_current(
            tool_version_id=tool_version_id,
            embedding_profile_id=snapshot.id,
            search_text=search_text,
            content_hash=content_hash,
            embedding=embedding,
        )
        await session.commit()
        assert row is not None
        return EnsureEmbeddingResult(
            tool_version_id=tool_version_id,
            embedding_profile_id=snapshot.id,
            status=ToolEmbeddingStatus.READY,
            content_hash=content_hash,
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
