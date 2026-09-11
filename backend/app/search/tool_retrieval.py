"""Authorized hybrid Tool retrieval service (docs/04 §6).

Internal only — no public Search HTTP API.

Pipeline:
  query text
  → active EmbeddingProfile snapshot
  → commit (no DB lock across outbound HTTP)
  → query embedding
  → authorized hybrid SQL (Hard Filter → lexical 40 → vector 40 → RRF → top 20)
  → safe ToolCandidateDescriptor (+ internal context)
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import RiskClass
from app.model_provider.client import EmbeddingConnectionTarget, ModelProviderClient
from app.model_provider.errors import DIMENSION_MISMATCH, PROTOCOL, ModelProviderError
from app.models.model_profile import EmbeddingProfile
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.embedding_profile import EmbeddingProfileRepository
from app.repositories.tool_retrieval import (
    MERGED_CANDIDATE_LIMIT,
    ToolRetrievalHit,
    ToolRetrievalRepository,
    ToolRetrievalSqlResult,
)
from app.search.tool_document import normalize_search_tags

logger = logging.getLogger(__name__)

_WS_RE = re.compile(r"\s+")
_RISK_VALUES = {item.value for item in RiskClass}


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
class ToolCandidateDescriptor:
    """Safe LLM-facing candidate (docs/04 §6.3). No secrets/endpoints/raw schemas."""

    tool_version_id: uuid.UUID
    name: str
    description: str | None
    tags: tuple[str, ...]
    required_inputs: tuple[str, ...]
    optional_inputs: tuple[str, ...]
    output_summary: str | None
    risk_class: str
    retrieval_score: float


@dataclass(frozen=True, slots=True)
class RetrievedToolCandidate:
    """Descriptor plus internal selector/plan context (not LLM-facing)."""

    mcp_tool_id: uuid.UUID
    descriptor: ToolCandidateDescriptor
    lexical_rank: int | None
    vector_rank: int | None
    rrf_raw: float
    agent_requires_confirmation: bool
    parameter_constraints: Any
    policy_present: bool
    tool_policy_id: uuid.UUID | None
    tool_policy_lock_version: int | None
    policy_requires_confirmation: bool | None
    policy_requires_approval: bool | None
    approval_policy_id: uuid.UUID | None
    allow_auto_select: bool | None


@dataclass(frozen=True, slots=True)
class ToolRetrievalResult:
    candidates: tuple[RetrievedToolCandidate, ...]
    embedding_profile_id: uuid.UUID
    profile_retried: bool


def _norm_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = _WS_RE.sub(" ", value).strip()
    return normalized or None


def split_input_fields(input_schema: Any) -> tuple[list[str], list[str]]:
    """Return sorted required/optional property names; empty on malformed schema."""
    if not isinstance(input_schema, dict):
        return [], []
    properties = input_schema.get("properties")
    if not isinstance(properties, dict):
        return [], []
    required_raw = input_schema.get("required")
    required_set: set[str] = set()
    if isinstance(required_raw, list):
        for item in required_raw:
            if isinstance(item, str) and item:
                required_set.add(item)
    required: list[str] = []
    optional: list[str] = []
    for name in sorted(
        str(key) for key in properties.keys() if isinstance(key, str) and key
    ):
        if name in required_set:
            required.append(name)
        else:
            optional.append(name)
    return required, optional


def summarize_output_schema(output_schema: Any) -> str | None:
    """Prefer output description; else sorted property names. Never raw schema JSON."""
    if not isinstance(output_schema, dict):
        return None
    description = _norm_text(output_schema.get("description"))
    if description is not None:
        return description
    properties = output_schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return None
    names = sorted(
        str(key) for key in properties.keys() if isinstance(key, str) and key
    )
    if not names:
        return None
    return ", ".join(names)


def build_candidate_description(
    *,
    description_override: str | None,
    remote_description: str | None,
) -> str | None:
    override = _norm_text(description_override)
    if override is not None:
        return override
    return _norm_text(remote_description)


def build_tool_candidate_descriptor(hit: ToolRetrievalHit) -> ToolCandidateDescriptor:
    required, optional = split_input_fields(hit.input_schema)
    tags = tuple(normalize_search_tags(hit.tags))
    risk = hit.risk_class if hit.risk_class in _RISK_VALUES else RiskClass.UNKNOWN.value
    return ToolCandidateDescriptor(
        tool_version_id=hit.tool_version_id,
        name=hit.remote_name,
        description=build_candidate_description(
            description_override=hit.description_override,
            remote_description=hit.remote_description,
        ),
        tags=tags,
        required_inputs=tuple(required),
        optional_inputs=tuple(optional),
        output_summary=summarize_output_schema(hit.output_schema),
        risk_class=risk,
        retrieval_score=float(hit.retrieval_score),
    )


def map_retrieval_hit(hit: ToolRetrievalHit) -> RetrievedToolCandidate:
    return RetrievedToolCandidate(
        mcp_tool_id=hit.mcp_tool_id,
        descriptor=build_tool_candidate_descriptor(hit),
        lexical_rank=hit.lexical_rank,
        vector_rank=hit.vector_rank,
        rrf_raw=hit.rrf_raw,
        agent_requires_confirmation=hit.agent_requires_confirmation,
        parameter_constraints=hit.parameter_constraints,
        policy_present=hit.policy_present,
        tool_policy_id=hit.tool_policy_id,
        tool_policy_lock_version=hit.tool_policy_lock_version,
        policy_requires_confirmation=hit.policy_requires_confirmation,
        policy_requires_approval=hit.policy_requires_approval,
        approval_policy_id=hit.approval_policy_id,
        allow_auto_select=hit.allow_auto_select,
    )


class ToolRetrievalService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        model_provider: ModelProviderClient | None = None,
    ) -> None:
        self._session = session
        self._provider = model_provider
        self._profiles = EmbeddingProfileRepository(session)
        self._agent_versions = AgentVersionRepository(session)
        self._retrieval = ToolRetrievalRepository(session)

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

    async def _load_active_profile_snapshot(self) -> _ProfileSnapshot:
        profile = await self._profiles.get_active_for_tools()
        if profile is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="No active embedding profile for tools.",
                status_code=status.HTTP_409_CONFLICT,
            )
        return self._snapshot_profile(profile)

    async def _embed_query(
        self,
        client: ModelProviderClient,
        snapshot: _ProfileSnapshot,
        query_text: str,
    ) -> list[float]:
        vectors = await client.embed_texts(
            EmbeddingConnectionTarget(
                provider=snapshot.provider,
                model=snapshot.model,
                base_url=snapshot.base_url,
                dimension=snapshot.dimension,
                credential_secret_id=snapshot.credential_secret_id,
            ),
            [query_text],
        )
        if not vectors or len(vectors) != 1:
            raise ModelProviderError(
                error_code=PROTOCOL,
                message="Query embedding provider returned an unexpected payload.",
                retryable=False,
            )
        vector = vectors[0]
        if len(vector) != snapshot.dimension:
            raise ModelProviderError(
                error_code=DIMENSION_MISMATCH,
                message=(
                    f"Embedding dimension mismatch: expected {snapshot.dimension}, "
                    f"got {len(vector)}."
                ),
                retryable=False,
            )
        return [float(x) for x in vector]

    async def _run_retrieval_sql(
        self,
        *,
        user_id: uuid.UUID,
        agent_version_id: uuid.UUID,
        query_text: str,
        query_vector: list[float],
        snapshot: _ProfileSnapshot,
    ) -> ToolRetrievalSqlResult:
        return await self._retrieval.authorized_hybrid_search(
            user_id=user_id,
            agent_version_id=agent_version_id,
            query_text=query_text,
            query_vector=query_vector,
            profile_id=snapshot.id,
            profile_lock_version=snapshot.lock_version,
            expected_dimension=snapshot.dimension,
            merged_limit=MERGED_CANDIDATE_LIMIT,
        )

    async def retrieve(
        self,
        *,
        user_id: uuid.UUID,
        agent_version_id: uuid.UUID,
        query_text: str,
    ) -> ToolRetrievalResult:
        normalized_query = query_text.strip()
        if not normalized_query:
            raise AppError(
                code="VALIDATION_ERROR",
                message="query_text must not be blank.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        agent_version = await self._agent_versions.get(agent_version_id)
        if agent_version is None:
            raise AppError(
                code="NOT_FOUND",
                message="Agent version not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        client = self._provider or ModelProviderClient()
        owns_client = self._provider is None
        try:
            snapshot = await self._load_active_profile_snapshot()
            # Do not hold a DB transaction/lock across outbound embedding HTTP.
            await self._session.commit()

            profile_retried = False
            query_vector = await self._embed_query(
                client, snapshot, normalized_query
            )
            sql_result = await self._run_retrieval_sql(
                user_id=user_id,
                agent_version_id=agent_version_id,
                query_text=normalized_query,
                query_vector=query_vector,
                snapshot=snapshot,
            )

            if not sql_result.profile_current:
                # Active profile raced during embedding — retry once with a fresh snapshot.
                profile_retried = True
                snapshot = await self._load_active_profile_snapshot()
                await self._session.commit()
                query_vector = await self._embed_query(
                    client, snapshot, normalized_query
                )
                sql_result = await self._run_retrieval_sql(
                    user_id=user_id,
                    agent_version_id=agent_version_id,
                    query_text=normalized_query,
                    query_vector=query_vector,
                    snapshot=snapshot,
                )
                if not sql_result.profile_current:
                    raise AppError(
                        code="RESOURCE_CONFLICT",
                        message=(
                            "Active embedding profile changed during retrieval; retry."
                        ),
                        status_code=status.HTTP_409_CONFLICT,
                    )

            candidates = tuple(map_retrieval_hit(hit) for hit in sql_result.hits)
            logger.info(
                "tool_retrieval complete user=%s agent_version=%s query_len=%s "
                "candidate_count=%s profile=%s retried=%s",
                user_id,
                agent_version_id,
                len(normalized_query),
                len(candidates),
                snapshot.id,
                profile_retried,
            )
            return ToolRetrievalResult(
                candidates=candidates,
                embedding_profile_id=snapshot.id,
                profile_retried=profile_retried,
            )
        finally:
            if owns_client:
                await client.aclose()
