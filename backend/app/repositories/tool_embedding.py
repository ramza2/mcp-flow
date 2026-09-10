"""ToolEmbedding repository — FTS / exact cosine primitives (docs/05 §8.6)."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mcp import ToolEmbedding


@dataclass(frozen=True, slots=True)
class LexicalHit:
    tool_version_id: uuid.UUID
    score: float


@dataclass(frozen=True, slots=True)
class VectorHit:
    tool_version_id: uuid.UUID
    distance: float


class ToolEmbeddingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self,
        *,
        tool_version_id: uuid.UUID,
        embedding_profile_id: uuid.UUID,
    ) -> ToolEmbedding | None:
        result = await self._session.execute(
            select(ToolEmbedding).where(
                ToolEmbedding.mcp_tool_version_id == tool_version_id,
                ToolEmbedding.embedding_profile_id == embedding_profile_id,
            )
        )
        return result.scalar_one_or_none()

    async def upsert_ready_if_current(
        self,
        *,
        tool_version_id: uuid.UUID,
        embedding_profile_id: uuid.UUID,
        search_text: str,
        content_hash: str,
        embedding: list[float],
    ) -> ToolEmbedding | None:
        """Atomic READY upsert for (ToolVersion, Profile).

        Metadata / profile races are enforced by ToolEmbeddingService before calling
        this method (recompute live content_hash + profile lock_version). Concurrent
        workers collide on UNIQUE(version, profile) and converge to one row.
        """
        stmt = text(
            """
            INSERT INTO tool_embeddings (
              id, mcp_tool_version_id, embedding_profile_id,
              search_text, search_tsv, embedding, content_hash, status
            ) VALUES (
              :id, :tool_version_id, :profile_id,
              :search_text, to_tsvector('simple', :search_text),
              CAST(:embedding AS vector), :content_hash, 'READY'
            )
            ON CONFLICT (mcp_tool_version_id, embedding_profile_id)
            DO UPDATE SET
              search_text = EXCLUDED.search_text,
              search_tsv = EXCLUDED.search_tsv,
              embedding = EXCLUDED.embedding,
              content_hash = EXCLUDED.content_hash,
              status = 'READY',
              updated_at = now()
            RETURNING id
            """
        )
        await self._session.execute(
            stmt,
            {
                "id": uuid.uuid4(),
                "tool_version_id": tool_version_id,
                "profile_id": embedding_profile_id,
                "search_text": search_text,
                "embedding": str(embedding),
                "content_hash": content_hash,
            },
        )
        await self._session.flush()
        return await self.get(
            tool_version_id=tool_version_id,
            embedding_profile_id=embedding_profile_id,
        )

    async def upsert_failed(
        self,
        *,
        tool_version_id: uuid.UUID,
        embedding_profile_id: uuid.UUID,
        search_text: str,
        content_hash: str,
    ) -> ToolEmbedding:
        stmt = text(
            """
            INSERT INTO tool_embeddings (
              id, mcp_tool_version_id, embedding_profile_id,
              search_text, search_tsv, embedding, content_hash, status
            ) VALUES (
              :id, :tool_version_id, :profile_id,
              :search_text, to_tsvector('simple', :search_text),
              NULL, :content_hash, 'FAILED'
            )
            ON CONFLICT (mcp_tool_version_id, embedding_profile_id)
            DO UPDATE SET
              search_text = EXCLUDED.search_text,
              search_tsv = EXCLUDED.search_tsv,
              embedding = NULL,
              content_hash = EXCLUDED.content_hash,
              status = 'FAILED',
              updated_at = now()
            RETURNING id
            """
        )
        await self._session.execute(
            stmt,
            {
                "id": uuid.uuid4(),
                "tool_version_id": tool_version_id,
                "profile_id": embedding_profile_id,
                "search_text": search_text,
                "content_hash": content_hash,
            },
        )
        await self._session.flush()
        row = await self.get(
            tool_version_id=tool_version_id,
            embedding_profile_id=embedding_profile_id,
        )
        assert row is not None
        return row

    async def mark_stale_for_tool(self, tool_id: uuid.UUID) -> int:
        from sqlalchemy import update

        from app.domain.enums import ToolEmbeddingStatus
        from app.models.mcp import MCPToolVersion

        versions = await self._session.execute(
            select(MCPToolVersion.id).where(MCPToolVersion.mcp_tool_id == tool_id)
        )
        version_ids = [row[0] for row in versions.all()]
        if not version_ids:
            return 0
        result = await self._session.execute(
            update(ToolEmbedding)
            .where(
                ToolEmbedding.mcp_tool_version_id.in_(version_ids),
                ToolEmbedding.status != ToolEmbeddingStatus.STALE,
            )
            .values(status=ToolEmbeddingStatus.STALE)
        )
        return int(result.rowcount or 0)

    async def mark_stale_for_profile(self, profile_id: uuid.UUID) -> int:
        from sqlalchemy import update

        from app.domain.enums import ToolEmbeddingStatus

        result = await self._session.execute(
            update(ToolEmbedding)
            .where(
                ToolEmbedding.embedding_profile_id == profile_id,
                ToolEmbedding.status != ToolEmbeddingStatus.STALE,
            )
            .values(status=ToolEmbeddingStatus.STALE)
        )
        return int(result.rowcount or 0)

    async def upsert_stale(
        self,
        *,
        tool_version_id: uuid.UUID,
        embedding_profile_id: uuid.UUID,
        search_text: str,
        content_hash: str,
    ) -> ToolEmbedding:
        stmt = text(
            """
            INSERT INTO tool_embeddings (
              id, mcp_tool_version_id, embedding_profile_id,
              search_text, search_tsv, embedding, content_hash, status
            ) VALUES (
              :id, :tool_version_id, :profile_id,
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
            RETURNING id
            """
        )
        await self._session.execute(
            stmt,
            {
                "id": uuid.uuid4(),
                "tool_version_id": tool_version_id,
                "profile_id": embedding_profile_id,
                "search_text": search_text,
                "content_hash": content_hash,
            },
        )
        await self._session.flush()
        row = await self.get(
            tool_version_id=tool_version_id,
            embedding_profile_id=embedding_profile_id,
        )
        assert row is not None
        return row

    async def list_missing_or_stale_for_profile(
        self, profile_id: uuid.UUID
    ) -> list[uuid.UUID]:
        stmt = text(
            """
            SELECT t.current_version_id
            FROM mcp_tools AS t
            WHERE t.deleted_at IS NULL
              AND t.current_version_id IS NOT NULL
              AND (
                NOT EXISTS (
                  SELECT 1 FROM tool_embeddings te
                  WHERE te.mcp_tool_version_id = t.current_version_id
                    AND te.embedding_profile_id = :profile_id
                )
                OR EXISTS (
                  SELECT 1 FROM tool_embeddings te
                  WHERE te.mcp_tool_version_id = t.current_version_id
                    AND te.embedding_profile_id = :profile_id
                    AND te.status IN ('STALE', 'FAILED')
                )
              )
            """
        )
        result = await self._session.execute(stmt, {"profile_id": profile_id})
        return [row[0] for row in result.all()]

    async def lexical_search(
        self,
        *,
        query: str,
        profile_id: uuid.UUID,
        limit: int,
    ) -> list[LexicalHit]:
        q = query.strip()
        if not q or limit <= 0:
            return []
        stmt = text(
            """
            SELECT te.mcp_tool_version_id AS tool_version_id,
                   ts_rank_cd(te.search_tsv, plainto_tsquery('simple', :query)) AS score
            FROM tool_embeddings AS te
            WHERE te.embedding_profile_id = :profile_id
              AND te.status IN ('READY', 'FAILED')
              AND te.search_tsv @@ plainto_tsquery('simple', :query)
            ORDER BY score DESC, te.mcp_tool_version_id
            LIMIT :limit
            """
        )
        result = await self._session.execute(
            stmt, {"query": q, "profile_id": profile_id, "limit": limit}
        )
        return [
            LexicalHit(tool_version_id=row["tool_version_id"], score=float(row["score"]))
            for row in result.mappings()
        ]

    async def vector_search(
        self,
        *,
        query_vector: list[float],
        profile_id: uuid.UUID,
        expected_dimension: int,
        limit: int,
    ) -> list[VectorHit]:
        if limit <= 0:
            return []
        if len(query_vector) != expected_dimension:
            raise ValueError(
                f"query_vector dimension mismatch: expected {expected_dimension}, "
                f"got {len(query_vector)}"
            )
        for value in query_vector:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError("query_vector contains non-numeric values")
            number = float(value)
            if math.isnan(number) or math.isinf(number):
                raise ValueError("query_vector contains NaN or Infinity")

        stmt = text(
            """
            SELECT te.mcp_tool_version_id AS tool_version_id,
                   (te.embedding <=> CAST(:query_vec AS vector)) AS distance
            FROM tool_embeddings AS te
            WHERE te.embedding_profile_id = :profile_id
              AND te.status = 'READY'
              AND te.embedding IS NOT NULL
            ORDER BY te.embedding <=> CAST(:query_vec AS vector) ASC,
                     te.mcp_tool_version_id
            LIMIT :limit
            """
        )
        result = await self._session.execute(
            stmt,
            {
                "query_vec": str(query_vector),
                "profile_id": profile_id,
                "limit": limit,
            },
        )
        return [
            VectorHit(
                tool_version_id=row["tool_version_id"],
                distance=float(row["distance"]),
            )
            for row in result.mappings()
        ]
