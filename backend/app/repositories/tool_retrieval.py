"""Authorized hybrid Tool retrieval — single-statement Hard Filter + RRF.

Hard Filter (auth helpers, Agent ALLOW grant, lifecycle, current-version ownership)
runs BEFORE lexical/vector ranking. Post-hoc N+1 authorize_resource filtering is
forbidden.

Authorization predicates are the same SQLAlchemy expressions used by
AuthorizationRepository.get_resource_authorization_snapshot().
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Select,
    and_,
    bindparam,
    exists,
    func,
    literal_column,
    select,
    true,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import over

from app.domain.enums import (
    AgentToolGrantEffect,
    MCPServerStatus,
    MCPToolStatus,
    ResourceGrantResourceType,
    RiskClass,
    ToolEmbeddingStatus,
    ToolVersionValidationStatus,
)
from app.models.agent import AgentToolGrant
from app.models.mcp import MCPServer, MCPTool, MCPToolPolicy, MCPToolVersion, ToolEmbedding
from app.models.model_profile import EmbeddingProfile
from app.repositories.authorization import (
    build_effective_permission_exists,
    build_effective_resource_grant_exists,
)

TOOL_EXECUTE_PERMISSION = "mcp.tool.execute"

LEXICAL_CANDIDATE_LIMIT = 40
VECTOR_CANDIDATE_LIMIT = 40
RRF_K = 60
MERGED_CANDIDATE_LIMIT = 20

RRF_THEORETICAL_MAX = 2.0 / (RRF_K + 1)


@dataclass(frozen=True, slots=True)
class ToolRetrievalHit:
    """One authorized RRF-merged candidate with internal selector context."""

    mcp_tool_id: uuid.UUID
    tool_version_id: uuid.UUID
    remote_name: str
    description_override: str | None
    remote_description: str | None
    tags: Any
    input_schema: Any
    output_schema: Any
    lexical_rank: int | None
    vector_rank: int | None
    rrf_raw: float
    retrieval_score: float
    agent_requires_confirmation: bool
    parameter_constraints: Any
    policy_present: bool
    tool_policy_id: uuid.UUID | None
    tool_policy_lock_version: int | None
    risk_class: str
    policy_requires_confirmation: bool | None
    policy_requires_approval: bool | None
    approval_policy_id: uuid.UUID | None
    allow_auto_select: bool | None


@dataclass(frozen=True, slots=True)
class ToolRetrievalSqlResult:
    """Result of one authorized retrieval statement."""

    profile_current: bool
    hits: list[ToolRetrievalHit]


def normalize_rrf_score(rrf_raw: float) -> float:
    """Map RRF raw score into [0, 1] using theoretical max 2/(k+1)."""
    if rrf_raw <= 0:
        return 0.0
    return min(1.0, float(rrf_raw) / RRF_THEORETICAL_MAX)


def rrf_contribution(rank: int | None, *, k: int = RRF_K) -> float:
    if rank is None:
        return 0.0
    if rank < 1:
        raise ValueError("RRF rank must be 1-based")
    return 1.0 / (k + rank)


def _validate_limits(
    *,
    lexical_limit: int,
    vector_limit: int,
    merged_limit: int,
    rrf_k: int,
) -> None:
    if lexical_limit < 1 or vector_limit < 1 or merged_limit < 1:
        raise ValueError("lexical_limit, vector_limit, and merged_limit must be >= 1")
    if rrf_k <= 0:
        raise ValueError("rrf_k must be > 0")


def _build_authorized_retrieval_statement(
    *,
    user_id: uuid.UUID,
    agent_version_id: uuid.UUID,
    query_text: str,
    query_vector: list[float],
    profile_id: uuid.UUID,
    profile_lock_version: int,
    lexical_limit: int,
    vector_limit: int,
    merged_limit: int,
    rrf_k: int,
) -> Select[Any]:
    """Compose one SELECT: profile gate + Hard Filter + lexical/vector + RRF."""

    permission_exists = build_effective_permission_exists(
        user_id, TOOL_EXECUTE_PERMISSION
    )
    resource_grant_exists = build_effective_resource_grant_exists(
        user_id,
        resource_type=ResourceGrantResourceType.MCP_TOOL.value,
        resource_id=MCPTool.id,
    )

    profile_current = exists(
        select(1).where(
            EmbeddingProfile.id == profile_id,
            EmbeddingProfile.is_active_for_tools.is_(True),
            EmbeddingProfile.lock_version == profile_lock_version,
        )
    )
    profile_gate = select(profile_current.label("profile_current")).cte("profile_gate")

    eligible_tools = (
        select(
            MCPTool.id.label("mcp_tool_id"),
            MCPTool.current_version_id.label("tool_version_id"),
            MCPTool.remote_name.label("remote_name"),
            MCPTool.description_override.label("description_override"),
            MCPTool.tags.label("tags"),
            MCPToolVersion.remote_description.label("remote_description"),
            MCPToolVersion.input_schema.label("input_schema"),
            MCPToolVersion.output_schema.label("output_schema"),
            AgentToolGrant.requires_confirmation.label(
                "agent_requires_confirmation"
            ),
            AgentToolGrant.parameter_constraints.label("parameter_constraints"),
            MCPToolPolicy.id.label("tool_policy_id"),
            MCPToolPolicy.lock_version.label("tool_policy_lock_version"),
            MCPToolPolicy.risk_class.label("policy_risk_class"),
            MCPToolPolicy.requires_confirmation.label(
                "policy_requires_confirmation"
            ),
            MCPToolPolicy.requires_approval.label("policy_requires_approval"),
            MCPToolPolicy.approval_policy_id.label("approval_policy_id"),
            MCPToolPolicy.allow_auto_select.label("allow_auto_select"),
        )
        .select_from(MCPTool)
        .join(profile_gate, true())
        .join(MCPServer, MCPServer.id == MCPTool.mcp_server_id)
        .join(
            MCPToolVersion,
            and_(
                MCPToolVersion.id == MCPTool.current_version_id,
                MCPToolVersion.mcp_tool_id == MCPTool.id,
            ),
        )
        .join(
            AgentToolGrant,
            and_(
                AgentToolGrant.agent_version_id == agent_version_id,
                AgentToolGrant.mcp_tool_id == MCPTool.id,
                AgentToolGrant.effect == AgentToolGrantEffect.ALLOW.value,
            ),
        )
        .outerjoin(MCPToolPolicy, MCPToolPolicy.mcp_tool_id == MCPTool.id)
        .where(
            permission_exists,
            profile_gate.c.profile_current.is_(True),
            resource_grant_exists,
            MCPTool.deleted_at.is_(None),
            MCPTool.status == MCPToolStatus.ACTIVE.value,
            MCPTool.current_version_id.is_not(None),
            MCPServer.deleted_at.is_(None),
            MCPServer.status == MCPServerStatus.ACTIVE.value,
            MCPToolVersion.validation_status
            == ToolVersionValidationStatus.VALID.value,
        )
    ).cte("eligible_tools")

    tsquery = func.plainto_tsquery(literal_column("'simple'"), query_text)
    lexical_rank = over(
        func.row_number(),
        order_by=(
            func.ts_rank_cd(ToolEmbedding.search_tsv, tsquery).desc(),
            eligible_tools.c.tool_version_id.asc(),
        ),
    ).label("lexical_rank")

    lexical_ranked = (
        select(eligible_tools, lexical_rank)
        .select_from(
            eligible_tools.join(
                ToolEmbedding,
                and_(
                    ToolEmbedding.mcp_tool_version_id
                    == eligible_tools.c.tool_version_id,
                    ToolEmbedding.embedding_profile_id == profile_id,
                    ToolEmbedding.status.in_(
                        (
                            ToolEmbeddingStatus.READY.value,
                            ToolEmbeddingStatus.FAILED.value,
                        )
                    ),
                    ToolEmbedding.search_tsv.op("@@")(tsquery),
                ),
            )
        )
    ).cte("lexical_ranked")

    lexical_top = (
        select(lexical_ranked).where(
            lexical_ranked.c.lexical_rank <= lexical_limit
        )
    ).cte("lexical_top")

    query_vec = bindparam("query_vector", value=list(map(float, query_vector)), type_=Vector())
    vector_rank = over(
        func.row_number(),
        order_by=(
            ToolEmbedding.embedding.op("<=>")(query_vec).asc(),
            eligible_tools.c.tool_version_id.asc(),
        ),
    ).label("vector_rank")

    vector_ranked = (
        select(eligible_tools, vector_rank)
        .select_from(
            eligible_tools.join(
                ToolEmbedding,
                and_(
                    ToolEmbedding.mcp_tool_version_id
                    == eligible_tools.c.tool_version_id,
                    ToolEmbedding.embedding_profile_id == profile_id,
                    ToolEmbedding.status == ToolEmbeddingStatus.READY.value,
                    ToolEmbedding.embedding.is_not(None),
                ),
            )
        )
    ).cte("vector_ranked")

    vector_top = (
        select(vector_ranked).where(vector_ranked.c.vector_rank <= vector_limit)
    ).cte("vector_top")

    lex = lexical_top
    vec = vector_top
    rrf_raw = (
        func.coalesce(1.0 / (rrf_k + lex.c.lexical_rank), 0.0)
        + func.coalesce(1.0 / (rrf_k + vec.c.vector_rank), 0.0)
    ).label("rrf_raw")

    merged = (
        select(
            func.coalesce(lex.c.mcp_tool_id, vec.c.mcp_tool_id).label("mcp_tool_id"),
            func.coalesce(lex.c.tool_version_id, vec.c.tool_version_id).label(
                "tool_version_id"
            ),
            func.coalesce(lex.c.remote_name, vec.c.remote_name).label("remote_name"),
            func.coalesce(
                lex.c.description_override, vec.c.description_override
            ).label("description_override"),
            func.coalesce(
                lex.c.remote_description, vec.c.remote_description
            ).label("remote_description"),
            func.coalesce(lex.c.tags, vec.c.tags).label("tags"),
            func.coalesce(lex.c.input_schema, vec.c.input_schema).label("input_schema"),
            func.coalesce(lex.c.output_schema, vec.c.output_schema).label(
                "output_schema"
            ),
            lex.c.lexical_rank,
            vec.c.vector_rank,
            rrf_raw,
            func.coalesce(
                lex.c.agent_requires_confirmation, vec.c.agent_requires_confirmation
            ).label("agent_requires_confirmation"),
            func.coalesce(
                lex.c.parameter_constraints, vec.c.parameter_constraints
            ).label("parameter_constraints"),
            func.coalesce(lex.c.tool_policy_id, vec.c.tool_policy_id).label(
                "tool_policy_id"
            ),
            func.coalesce(
                lex.c.tool_policy_lock_version, vec.c.tool_policy_lock_version
            ).label("tool_policy_lock_version"),
            func.coalesce(lex.c.policy_risk_class, vec.c.policy_risk_class).label(
                "policy_risk_class"
            ),
            func.coalesce(
                lex.c.policy_requires_confirmation, vec.c.policy_requires_confirmation
            ).label("policy_requires_confirmation"),
            func.coalesce(
                lex.c.policy_requires_approval, vec.c.policy_requires_approval
            ).label("policy_requires_approval"),
            func.coalesce(lex.c.approval_policy_id, vec.c.approval_policy_id).label(
                "approval_policy_id"
            ),
            func.coalesce(lex.c.allow_auto_select, vec.c.allow_auto_select).label(
                "allow_auto_select"
            ),
        )
        .select_from(
            lex.join(
                vec,
                lex.c.tool_version_id == vec.c.tool_version_id,
                full=True,
            )
        )
    ).cte("merged")

    merge_rank = over(
        func.row_number(),
        order_by=(merged.c.rrf_raw.desc(), merged.c.tool_version_id.asc()),
    ).label("merge_rank")

    ranked = select(merged, merge_rank).cte("ranked")

    return (
        select(
            profile_gate.c.profile_current,
            ranked.c.mcp_tool_id,
            ranked.c.tool_version_id,
            ranked.c.remote_name,
            ranked.c.description_override,
            ranked.c.remote_description,
            ranked.c.tags,
            ranked.c.input_schema,
            ranked.c.output_schema,
            ranked.c.lexical_rank,
            ranked.c.vector_rank,
            ranked.c.rrf_raw,
            ranked.c.agent_requires_confirmation,
            ranked.c.parameter_constraints,
            ranked.c.tool_policy_id,
            ranked.c.tool_policy_lock_version,
            ranked.c.policy_risk_class,
            ranked.c.policy_requires_confirmation,
            ranked.c.policy_requires_approval,
            ranked.c.approval_policy_id,
            ranked.c.allow_auto_select,
        )
        .select_from(
            profile_gate.outerjoin(
                ranked, ranked.c.merge_rank <= merged_limit
            )
        )
        .order_by(
            ranked.c.rrf_raw.desc().nulls_last(),
            ranked.c.tool_version_id.asc().nulls_last(),
        )
    )


class ToolRetrievalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def authorized_hybrid_search(
        self,
        *,
        user_id: uuid.UUID,
        agent_version_id: uuid.UUID,
        query_text: str,
        query_vector: list[float],
        profile_id: uuid.UUID,
        profile_lock_version: int,
        expected_dimension: int,
        lexical_limit: int = LEXICAL_CANDIDATE_LIMIT,
        vector_limit: int = VECTOR_CANDIDATE_LIMIT,
        merged_limit: int = MERGED_CANDIDATE_LIMIT,
        rrf_k: int = RRF_K,
    ) -> ToolRetrievalSqlResult:
        _validate_limits(
            lexical_limit=lexical_limit,
            vector_limit=vector_limit,
            merged_limit=merged_limit,
            rrf_k=rrf_k,
        )
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

        stmt = _build_authorized_retrieval_statement(
            user_id=user_id,
            agent_version_id=agent_version_id,
            query_text=query_text,
            query_vector=query_vector,
            profile_id=profile_id,
            profile_lock_version=profile_lock_version,
            lexical_limit=lexical_limit,
            vector_limit=vector_limit,
            merged_limit=merged_limit,
            rrf_k=rrf_k,
        )
        result = await self._session.execute(stmt)
        rows = list(result.mappings().all())
        if not rows:
            return ToolRetrievalSqlResult(profile_current=False, hits=[])

        profile_current = bool(rows[0]["profile_current"])
        hits: list[ToolRetrievalHit] = []
        for row in rows:
            if row["tool_version_id"] is None:
                continue
            rrf_raw = float(row["rrf_raw"] or 0.0)
            policy_present = row["tool_policy_id"] is not None
            policy_risk = row["policy_risk_class"]
            hits.append(
                ToolRetrievalHit(
                    mcp_tool_id=row["mcp_tool_id"],
                    tool_version_id=row["tool_version_id"],
                    remote_name=row["remote_name"],
                    description_override=row["description_override"],
                    remote_description=row["remote_description"],
                    tags=row["tags"],
                    input_schema=row["input_schema"],
                    output_schema=row["output_schema"],
                    lexical_rank=(
                        int(row["lexical_rank"])
                        if row["lexical_rank"] is not None
                        else None
                    ),
                    vector_rank=(
                        int(row["vector_rank"])
                        if row["vector_rank"] is not None
                        else None
                    ),
                    rrf_raw=rrf_raw,
                    retrieval_score=normalize_rrf_score(rrf_raw),
                    agent_requires_confirmation=bool(
                        row["agent_requires_confirmation"]
                    ),
                    parameter_constraints=row["parameter_constraints"],
                    policy_present=policy_present,
                    tool_policy_id=row["tool_policy_id"],
                    tool_policy_lock_version=(
                        int(row["tool_policy_lock_version"])
                        if row["tool_policy_lock_version"] is not None
                        else None
                    ),
                    risk_class=(
                        str(policy_risk)
                        if policy_present and policy_risk is not None
                        else RiskClass.UNKNOWN.value
                    ),
                    policy_requires_confirmation=(
                        bool(row["policy_requires_confirmation"])
                        if policy_present
                        else None
                    ),
                    policy_requires_approval=(
                        bool(row["policy_requires_approval"])
                        if policy_present
                        else None
                    ),
                    approval_policy_id=row["approval_policy_id"],
                    allow_auto_select=(
                        bool(row["allow_auto_select"]) if policy_present else None
                    ),
                )
            )
        return ToolRetrievalSqlResult(profile_current=profile_current, hits=hits)


# STALE embeddings are intentionally excluded from both lexical and vector channels.
assert ToolEmbeddingStatus.STALE.value == "STALE"
