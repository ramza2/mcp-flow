"""External MCP Discovery service (docs/02 FNC-DISC-001..003, docs/06 §19)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.discovery.contracts import ExternalMCPProviderCandidate
from app.discovery.provider import (
    EXTERNAL_DISCOVERY_PROVIDER_UNAVAILABLE,
    ExternalDiscoveryProviderError,
    ExternalMCPDiscoveryProvider,
    UnavailableExternalMCPProvider,
)
from app.domain.enums import (
    ExternalMCPCandidateReviewState,
    ExternalMCPReviewDecision,
    ExternalMCPSearchStatus,
    MCPAuthType,
    MCPServerStatus,
    MCPTransportType,
    UserStatus,
)
from app.models.external_discovery import (
    ExternalMCPCandidate,
    ExternalMCPSearch,
    ExternalMCPSource,
)
from app.models.mcp import MCPServer
from app.repositories.external_discovery import ExternalDiscoveryRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.user import UserRepository
from app.schemas.external_discovery import (
    ExternalMCPCandidateResponse,
    ExternalMCPCandidateSummary,
    ExternalMCPImportResponse,
    ExternalMCPReviewCreate,
    ExternalMCPReviewResponse,
    ExternalMCPSearchCreate,
    ExternalMCPSearchResponse,
    ExternalMCPSourceListResponse,
    ExternalMCPSourceResponse,
)
from app.schemas.mcp_server import MCPServerCreate
from app.services.authorization import AuthorizationResolver
from app.services.mcp_server import MCPServerService

_MCP_SERVER_READ = "mcp.server.read"
_MCP_SERVER_MANAGE = "mcp.server.manage"

_MAX_DESCRIPTION = 4000
_MAX_ERROR_MESSAGE = 500
_MAX_DISPLAY_URL = 2048
_MAX_EXTERNAL_KEY = 256
_MAX_NAME = 255
_MAX_VERSION = 128
_MAX_LICENSE = 128


class ExternalDiscoveryService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        provider: ExternalMCPDiscoveryProvider | None = None,
    ) -> None:
        self._session = session
        self._repo = ExternalDiscoveryRepository(session)
        self._users = UserRepository(session)
        self._authz = AuthorizationResolver(session)
        self._servers = MCPServerRepository(session)
        self._mcp_servers = MCPServerService(session)
        self._provider: ExternalMCPDiscoveryProvider = (
            provider if provider is not None else UnavailableExternalMCPProvider()
        )

    async def _assert_active(self, actor_user_id: uuid.UUID) -> None:
        user = await self._users.get(actor_user_id)
        if user is None or user.status != UserStatus.ACTIVE.value:
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Actor is not an ACTIVE user.",
                status_code=status.HTTP_403_FORBIDDEN,
            )

    async def _assert_read(self, actor_user_id: uuid.UUID) -> None:
        await self._assert_active(actor_user_id)
        if not await self._authz.has_permission(actor_user_id, _MCP_SERVER_READ):
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Missing mcp.server.read permission.",
                status_code=status.HTTP_403_FORBIDDEN,
            )

    async def _assert_manage(self, actor_user_id: uuid.UUID) -> None:
        await self._assert_active(actor_user_id)
        if not await self._authz.has_permission(actor_user_id, _MCP_SERVER_MANAGE):
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Missing mcp.server.manage permission.",
                status_code=status.HTTP_403_FORBIDDEN,
            )

    @staticmethod
    def _review_state_from_decision(
        decision: str | None,
    ) -> ExternalMCPCandidateReviewState:
        if decision == ExternalMCPReviewDecision.APPROVE.value:
            return ExternalMCPCandidateReviewState.APPROVED
        if decision == ExternalMCPReviewDecision.REJECT.value:
            return ExternalMCPCandidateReviewState.REJECTED
        return ExternalMCPCandidateReviewState.UNREVIEWED

    async def _effective_review_state(
        self, candidate_id: uuid.UUID
    ) -> ExternalMCPCandidateReviewState:
        latest = await self._repo.latest_review(candidate_id)
        if latest is None:
            return ExternalMCPCandidateReviewState.UNREVIEWED
        return self._review_state_from_decision(latest.decision)

    def _source_to_response(self, source: ExternalMCPSource) -> ExternalMCPSourceResponse:
        return ExternalMCPSourceResponse.model_validate(source)

    async def _candidate_summary(
        self, candidate: ExternalMCPCandidate
    ) -> ExternalMCPCandidateSummary:
        return ExternalMCPCandidateSummary(
            id=candidate.id,
            search_id=candidate.search_id,
            source_id=candidate.source_id,
            external_key=candidate.external_key,
            name=candidate.name,
            description=candidate.description,
            version=candidate.version,
            license=candidate.license,
            repository_url=candidate.repository_url,
            homepage_url=candidate.homepage_url,
            transport_type=candidate.transport_type,
            endpoint_url=candidate.endpoint_url,
            review_state=await self._effective_review_state(candidate.id),
            imported_mcp_server_id=candidate.imported_mcp_server_id,
            discovered_at=candidate.discovered_at,
        )

    async def _search_to_response(
        self, search: ExternalMCPSearch
    ) -> ExternalMCPSearchResponse:
        candidates = await self._repo.list_candidates_for_search(search.id)
        summaries = [await self._candidate_summary(c) for c in candidates]
        return ExternalMCPSearchResponse(
            id=search.id,
            source_id=search.source_id,
            query=search.query,
            status=ExternalMCPSearchStatus(search.status),
            requested_limit=search.requested_limit,
            candidate_count=search.candidate_count,
            error_code=search.error_code,
            error_message=search.error_message,
            requested_by=search.requested_by,
            started_at=search.started_at,
            finished_at=search.finished_at,
            candidates=summaries,
        )

    async def list_sources(
        self, actor_user_id: uuid.UUID
    ) -> ExternalMCPSourceListResponse:
        await self._assert_read(actor_user_id)
        sources = await self._repo.list_sources()
        return ExternalMCPSourceListResponse(
            items=[self._source_to_response(s) for s in sources]
        )

    @staticmethod
    def _bound_optional(value: str | None, *, max_len: int) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            return None
        return trimmed[:max_len]

    def _validate_provider_candidate(
        self, raw: ExternalMCPProviderCandidate
    ) -> dict[str, str | None]:
        """Validate/bound untrusted provider output. Raises AppError on reject."""

        external_key = (raw.external_key or "").strip()
        name = (raw.name or "").strip()
        if not external_key or len(external_key) > _MAX_EXTERNAL_KEY:
            raise AppError(
                code="VALIDATION_ERROR",
                message="Provider candidate external_key is invalid.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        if not name or len(name) > _MAX_NAME:
            raise AppError(
                code="VALIDATION_ERROR",
                message="Provider candidate name is invalid.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        return {
            "external_key": external_key,
            "name": name,
            "description": self._bound_optional(raw.description, max_len=_MAX_DESCRIPTION),
            "version": self._bound_optional(raw.version, max_len=_MAX_VERSION),
            "license": self._bound_optional(raw.license, max_len=_MAX_LICENSE),
            "repository_url": self._bound_optional(
                raw.repository_url, max_len=_MAX_DISPLAY_URL
            ),
            "homepage_url": self._bound_optional(
                raw.homepage_url, max_len=_MAX_DISPLAY_URL
            ),
            "transport_type": self._bound_optional(raw.transport_type, max_len=32),
            "endpoint_url": self._bound_optional(
                raw.endpoint_url, max_len=_MAX_DISPLAY_URL
            ),
        }

    async def create_search(
        self,
        actor_user_id: uuid.UUID,
        data: ExternalMCPSearchCreate,
    ) -> ExternalMCPSearchResponse:
        await self._assert_manage(actor_user_id)

        source = await self._repo.get_source(data.source_id)
        if source is None:
            raise AppError(
                code="NOT_FOUND",
                message="External MCP source not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if not source.enabled:
            raise AppError(
                code="EXTERNAL_DISCOVERY_SOURCE_DISABLED",
                message="External MCP source is disabled.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        query = data.q.strip()
        search = await self._repo.create_search(
            source_id=source.id,
            query=query,
            status=ExternalMCPSearchStatus.RUNNING.value,
            requested_limit=data.limit,
            requested_by=actor_user_id,
        )
        await self._session.commit()
        await self._session.refresh(search)

        try:
            provider_candidates = await self._provider.search(
                source, query, data.limit
            )
        except ExternalDiscoveryProviderError as exc:
            finished = await self._repo.finish_search(
                search,
                status=ExternalMCPSearchStatus.FAILED.value,
                candidate_count=0,
                finished_at=datetime.now(UTC),
                error_code=exc.code,
                error_message=(exc.message or "")[:_MAX_ERROR_MESSAGE],
            )
            await self._session.commit()
            return await self._search_to_response(finished)
        except Exception:
            finished = await self._repo.finish_search(
                search,
                status=ExternalMCPSearchStatus.FAILED.value,
                candidate_count=0,
                finished_at=datetime.now(UTC),
                error_code=EXTERNAL_DISCOVERY_PROVIDER_UNAVAILABLE,
                error_message="External MCP discovery provider failed.",
            )
            await self._session.commit()
            return await self._search_to_response(finished)

        if not isinstance(provider_candidates, list):
            finished = await self._repo.finish_search(
                search,
                status=ExternalMCPSearchStatus.FAILED.value,
                candidate_count=0,
                finished_at=datetime.now(UTC),
                error_code="EXTERNAL_DISCOVERY_PROVIDER_INVALID_RESULT",
                error_message="Provider returned an invalid candidate list.",
            )
            await self._session.commit()
            return await self._search_to_response(finished)

        try:
            validated: list[dict[str, str | None]] = []
            seen_keys: set[str] = set()
            for raw in provider_candidates[: data.limit]:
                if not isinstance(raw, ExternalMCPProviderCandidate):
                    raise AppError(
                        code="VALIDATION_ERROR",
                        message="Provider candidate type is invalid.",
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    )
                row = self._validate_provider_candidate(raw)
                key = str(row["external_key"])
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                validated.append(row)

            for row in validated:
                await self._repo.create_candidate(
                    search_id=search.id,
                    source_id=source.id,
                    external_key=str(row["external_key"]),
                    name=str(row["name"]),
                    description=row["description"],
                    version=row["version"],
                    license=row["license"],
                    repository_url=row["repository_url"],
                    homepage_url=row["homepage_url"],
                    transport_type=row["transport_type"],
                    endpoint_url=row["endpoint_url"],
                )

            finished = await self._repo.finish_search(
                search,
                status=ExternalMCPSearchStatus.SUCCEEDED.value,
                candidate_count=len(validated),
                finished_at=datetime.now(UTC),
            )
            await self._session.commit()
            return await self._search_to_response(finished)
        except AppError as exc:
            await self._session.rollback()
            search = await self._repo.get_search(search.id)
            assert search is not None
            finished = await self._repo.finish_search(
                search,
                status=ExternalMCPSearchStatus.FAILED.value,
                candidate_count=0,
                finished_at=datetime.now(UTC),
                error_code=exc.code,
                error_message=(exc.message or "")[:_MAX_ERROR_MESSAGE],
            )
            await self._session.commit()
            return await self._search_to_response(finished)

    async def get_search(
        self, actor_user_id: uuid.UUID, search_id: uuid.UUID
    ) -> ExternalMCPSearchResponse:
        await self._assert_read(actor_user_id)
        search = await self._repo.get_search(search_id)
        if search is None:
            raise AppError(
                code="NOT_FOUND",
                message="External MCP search not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return await self._search_to_response(search)

    async def get_candidate(
        self, actor_user_id: uuid.UUID, candidate_id: uuid.UUID
    ) -> ExternalMCPCandidateResponse:
        await self._assert_read(actor_user_id)
        candidate = await self._repo.get_candidate(candidate_id)
        if candidate is None:
            raise AppError(
                code="NOT_FOUND",
                message="External MCP candidate not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        summary = await self._candidate_summary(candidate)
        return ExternalMCPCandidateResponse.model_validate(summary.model_dump())

    async def create_review(
        self,
        actor_user_id: uuid.UUID,
        candidate_id: uuid.UUID,
        data: ExternalMCPReviewCreate,
    ) -> ExternalMCPReviewResponse:
        await self._assert_manage(actor_user_id)
        candidate = await self._repo.get_candidate(candidate_id)
        if candidate is None:
            raise AppError(
                code="NOT_FOUND",
                message="External MCP candidate not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        comment = data.comment.strip() if data.comment else None
        if comment == "":
            comment = None

        review = await self._repo.create_review(
            candidate_id=candidate.id,
            decision=str(data.decision),
            comment=comment,
            reviewed_by=actor_user_id,
        )
        await self._session.commit()
        await self._session.refresh(review)

        return ExternalMCPReviewResponse(
            id=review.id,
            candidate_id=review.candidate_id,
            decision=ExternalMCPReviewDecision(review.decision),
            comment=review.comment,
            reviewed_by=review.reviewed_by,
            reviewed_at=review.reviewed_at,
            review_state=self._review_state_from_decision(review.decision),
        )

    async def import_candidate(
        self,
        actor_user_id: uuid.UUID,
        candidate_id: uuid.UUID,
    ) -> ExternalMCPImportResponse:
        await self._assert_manage(actor_user_id)

        candidate = await self._repo.lock_candidate_for_update(candidate_id)
        if candidate is None:
            raise AppError(
                code="NOT_FOUND",
                message="External MCP candidate not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        if candidate.imported_mcp_server_id is not None:
            server = await self._servers.get(candidate.imported_mcp_server_id)
            if server is None:
                raise AppError(
                    code="NOT_FOUND",
                    message="Imported MCP server not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            return ExternalMCPImportResponse(
                candidate_id=candidate.id,
                mcp_server_id=server.id,
                created=False,
                server_status=MCPServerStatus(server.status),
                transport_type=MCPTransportType(server.transport_type),
            )

        latest = await self._repo.latest_review(candidate.id)
        if latest is None or latest.decision != ExternalMCPReviewDecision.APPROVE.value:
            raise AppError(
                code="EXTERNAL_DISCOVERY_IMPORT_NOT_APPROVED",
                message="Candidate must have latest review APPROVE before import.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        transport_raw = (candidate.transport_type or "").strip()
        if transport_raw == MCPTransportType.STDIO.value:
            raise AppError(
                code="EXTERNAL_DISCOVERY_STDIO_IMPORT_UNSUPPORTED",
                message=(
                    "STDIO import from external candidates is unsupported; "
                    "map to an operator-approved stdio_manifest_id in a later slice."
                ),
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        if transport_raw not in {
            MCPTransportType.STREAMABLE_HTTP.value,
            MCPTransportType.LEGACY_HTTP_SSE.value,
        }:
            raise AppError(
                code="VALIDATION_ERROR",
                message="Import requires STREAMABLE_HTTP or LEGACY_HTTP_SSE transport.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        endpoint = (candidate.endpoint_url or "").strip()
        if not endpoint:
            raise AppError(
                code="VALIDATION_ERROR",
                message="Import requires a candidate endpoint_url.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        create_data = MCPServerCreate(
            name=candidate.name[:255],
            description=candidate.description,
            transport_type=MCPTransportType(transport_raw),
            endpoint_url=endpoint,
            stdio_manifest_id=None,
            auth_type=MCPAuthType.NONE,
            auth_secret_id=None,
        )
        # Reuses MCPServerService validation (URL/SSRF boundary) without auto
        # connection-test, discovery, or activation.
        server: MCPServer = await self._mcp_servers.create_draft_uncommitted(
            create_data,
            created_by=actor_user_id,
        )
        await self._repo.set_imported_server(candidate, server.id)
        await self._session.commit()
        await self._session.refresh(server)

        assert server.status == MCPServerStatus.DRAFT.value
        return ExternalMCPImportResponse(
            candidate_id=candidate.id,
            mcp_server_id=server.id,
            created=True,
            server_status=MCPServerStatus.DRAFT,
            transport_type=MCPTransportType(server.transport_type),
        )
