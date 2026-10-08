"""Persistence-only repository for External MCP Discovery."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.external_discovery import (
    ExternalMCPCandidate,
    ExternalMCPReview,
    ExternalMCPSearch,
    ExternalMCPSource,
)


class ExternalDiscoveryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_sources(self) -> list[ExternalMCPSource]:
        stmt = select(ExternalMCPSource).order_by(ExternalMCPSource.code.asc())
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_source(self, source_id: uuid.UUID) -> ExternalMCPSource | None:
        return await self._session.get(ExternalMCPSource, source_id)

    async def create_source(
        self,
        *,
        code: str,
        name: str,
        source_type: str,
        provider_key: str,
        base_url: str | None = None,
        enabled: bool = True,
    ) -> ExternalMCPSource:
        """Test/bootstrap helper — no public source CRUD API in this slice."""
        source = ExternalMCPSource(
            code=code,
            name=name,
            source_type=source_type,
            provider_key=provider_key,
            base_url=base_url,
            enabled=enabled,
        )
        self._session.add(source)
        await self._session.flush()
        await self._session.refresh(source)
        return source

    async def create_search(
        self,
        *,
        source_id: uuid.UUID,
        query: str,
        status: str,
        requested_limit: int,
        requested_by: uuid.UUID,
        candidate_count: int = 0,
    ) -> ExternalMCPSearch:
        search = ExternalMCPSearch(
            source_id=source_id,
            query=query,
            status=status,
            requested_limit=requested_limit,
            candidate_count=candidate_count,
            requested_by=requested_by,
        )
        self._session.add(search)
        await self._session.flush()
        await self._session.refresh(search)
        return search

    async def get_search(self, search_id: uuid.UUID) -> ExternalMCPSearch | None:
        return await self._session.get(ExternalMCPSearch, search_id)

    async def finish_search(
        self,
        search: ExternalMCPSearch,
        *,
        status: str,
        candidate_count: int,
        finished_at: datetime,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> ExternalMCPSearch:
        search.status = status
        search.candidate_count = candidate_count
        search.finished_at = finished_at
        search.error_code = error_code
        search.error_message = error_message
        await self._session.flush()
        await self._session.refresh(search)
        return search

    async def create_candidate(
        self,
        *,
        search_id: uuid.UUID,
        source_id: uuid.UUID,
        external_key: str,
        name: str,
        description: str | None = None,
        version: str | None = None,
        license: str | None = None,
        repository_url: str | None = None,
        homepage_url: str | None = None,
        transport_type: str | None = None,
        endpoint_url: str | None = None,
    ) -> ExternalMCPCandidate:
        candidate = ExternalMCPCandidate(
            search_id=search_id,
            source_id=source_id,
            external_key=external_key,
            name=name,
            description=description,
            version=version,
            license=license,
            repository_url=repository_url,
            homepage_url=homepage_url,
            transport_type=transport_type,
            endpoint_url=endpoint_url,
        )
        self._session.add(candidate)
        await self._session.flush()
        await self._session.refresh(candidate)
        return candidate

    async def list_candidates_for_search(
        self, search_id: uuid.UUID
    ) -> list[ExternalMCPCandidate]:
        stmt = (
            select(ExternalMCPCandidate)
            .where(ExternalMCPCandidate.search_id == search_id)
            .order_by(ExternalMCPCandidate.discovered_at.asc(), ExternalMCPCandidate.id.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_candidate(
        self, candidate_id: uuid.UUID
    ) -> ExternalMCPCandidate | None:
        return await self._session.get(ExternalMCPCandidate, candidate_id)

    async def lock_candidate_for_update(
        self, candidate_id: uuid.UUID
    ) -> ExternalMCPCandidate | None:
        stmt = (
            select(ExternalMCPCandidate)
            .where(ExternalMCPCandidate.id == candidate_id)
            .with_for_update()
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def set_imported_server(
        self,
        candidate: ExternalMCPCandidate,
        mcp_server_id: uuid.UUID,
    ) -> ExternalMCPCandidate:
        candidate.imported_mcp_server_id = mcp_server_id
        await self._session.flush()
        await self._session.refresh(candidate)
        return candidate

    async def create_review(
        self,
        *,
        candidate_id: uuid.UUID,
        decision: str,
        comment: str | None,
        reviewed_by: uuid.UUID,
        reviewed_at: datetime | None = None,
    ) -> ExternalMCPReview:
        # Explicit UTC timestamp (microseconds) so same-second re-reviews on
        # SQLite still order correctly via reviewed_at DESC.
        review = ExternalMCPReview(
            candidate_id=candidate_id,
            decision=decision,
            comment=comment,
            reviewed_by=reviewed_by,
            reviewed_at=reviewed_at or datetime.now(UTC),
        )
        self._session.add(review)
        await self._session.flush()
        await self._session.refresh(review)
        return review

    async def latest_review(
        self, candidate_id: uuid.UUID
    ) -> ExternalMCPReview | None:
        stmt = (
            select(ExternalMCPReview)
            .where(ExternalMCPReview.candidate_id == candidate_id)
            .order_by(
                ExternalMCPReview.reviewed_at.desc(),
                ExternalMCPReview.id.desc(),
            )
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_reviews(
        self, candidate_id: uuid.UUID
    ) -> list[ExternalMCPReview]:
        stmt = (
            select(ExternalMCPReview)
            .where(ExternalMCPReview.candidate_id == candidate_id)
            .order_by(
                ExternalMCPReview.reviewed_at.desc(),
                ExternalMCPReview.id.desc(),
            )
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
