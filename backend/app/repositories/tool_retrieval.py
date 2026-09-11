"""Authorized hybrid Tool retrieval — single-statement Hard Filter + RRF.

Hard Filter (auth, Agent ALLOW grant, lifecycle) runs BEFORE lexical/vector
ranking. Post-hoc N+1 authorize_resource filtering is forbidden.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    AgentToolGrantEffect,
    MCPServerStatus,
    MCPToolStatus,
    ResourceGrantResourceType,
    RiskClass,
    ToolEmbeddingStatus,
    ToolVersionValidationStatus,
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


# Single PostgreSQL statement. profile_current is always returned so callers can
# distinguish "no eligible tools" from "active EmbeddingProfile raced away".
_AUTHORIZED_RETRIEVAL_SQL = """
WITH auth_gate AS (
  SELECT EXISTS (
    SELECT 1
    FROM users AS u
    JOIN user_roles AS ur ON ur.user_id = u.id
    JOIN roles AS r ON r.id = ur.role_id
    JOIN role_permissions AS rp ON rp.role_id = r.id
    JOIN permissions AS p ON p.id = rp.permission_id
    WHERE u.id = CAST(:user_id AS uuid)
      AND u.deleted_at IS NULL
      AND u.status = 'ACTIVE'
      AND r.deleted_at IS NULL
      AND p.code = :permission_code
  ) AS allowed
),
profile_gate AS (
  SELECT EXISTS (
    SELECT 1
    FROM embedding_profiles AS ep
    WHERE ep.id = CAST(:profile_id AS uuid)
      AND ep.is_active_for_tools IS TRUE
      AND ep.lock_version = :profile_lock_version
  ) AS profile_current
),
eligible_tools AS (
  SELECT
    t.id AS mcp_tool_id,
    t.current_version_id AS tool_version_id,
    t.remote_name,
    t.description_override,
    t.tags,
    v.remote_description,
    v.input_schema,
    v.output_schema,
    g.requires_confirmation AS agent_requires_confirmation,
    g.parameter_constraints,
    pol.id AS tool_policy_id,
    pol.lock_version AS tool_policy_lock_version,
    pol.risk_class AS policy_risk_class,
    pol.requires_confirmation AS policy_requires_confirmation,
    pol.requires_approval AS policy_requires_approval,
    pol.approval_policy_id,
    pol.allow_auto_select
  FROM auth_gate AS ag
  JOIN profile_gate AS pg ON pg.profile_current IS TRUE
  JOIN mcp_tools AS t ON ag.allowed IS TRUE
  JOIN mcp_servers AS s ON s.id = t.mcp_server_id
  JOIN mcp_tool_versions AS v ON v.id = t.current_version_id
  JOIN agent_tool_grants AS g
    ON g.agent_version_id = CAST(:agent_version_id AS uuid)
   AND g.mcp_tool_id = t.id
   AND g.effect = :allow_effect
  LEFT JOIN mcp_tool_policies AS pol ON pol.mcp_tool_id = t.id
  WHERE t.deleted_at IS NULL
    AND t.status = :tool_active
    AND t.current_version_id IS NOT NULL
    AND s.deleted_at IS NULL
    AND s.status = :server_active
    AND v.validation_status = :version_valid
    AND EXISTS (
      SELECT 1
      FROM resource_grants AS rg
      WHERE rg.resource_type = :resource_type_tool
        AND rg.resource_id = t.id
        AND (
          rg.user_id = CAST(:user_id AS uuid)
          OR (
            rg.role_id IS NOT NULL
            AND EXISTS (
              SELECT 1
              FROM user_roles AS ur2
              JOIN roles AS r2 ON r2.id = ur2.role_id
              WHERE ur2.user_id = CAST(:user_id AS uuid)
                AND ur2.role_id = rg.role_id
                AND r2.deleted_at IS NULL
            )
          )
        )
    )
),
lexical_ranked AS (
  SELECT
    e.*,
    ROW_NUMBER() OVER (
      ORDER BY ts_rank_cd(
               te.search_tsv,
               plainto_tsquery('simple', :query_text)
             ) DESC,
             e.tool_version_id ASC
    ) AS lexical_rank
  FROM eligible_tools AS e
  JOIN tool_embeddings AS te
    ON te.mcp_tool_version_id = e.tool_version_id
   AND te.embedding_profile_id = CAST(:profile_id AS uuid)
   AND te.status IN ('READY', 'FAILED')
   AND te.search_tsv @@ plainto_tsquery('simple', :query_text)
),
lexical_top AS (
  SELECT * FROM lexical_ranked WHERE lexical_rank <= :lexical_limit
),
vector_ranked AS (
  SELECT
    e.*,
    ROW_NUMBER() OVER (
      ORDER BY (te.embedding <=> CAST(:query_vector AS vector)) ASC,
               e.tool_version_id ASC
    ) AS vector_rank
  FROM eligible_tools AS e
  JOIN tool_embeddings AS te
    ON te.mcp_tool_version_id = e.tool_version_id
   AND te.embedding_profile_id = CAST(:profile_id AS uuid)
   AND te.status = 'READY'
   AND te.embedding IS NOT NULL
),
vector_top AS (
  SELECT * FROM vector_ranked WHERE vector_rank <= :vector_limit
),
merged AS (
  SELECT
    COALESCE(l.mcp_tool_id, v.mcp_tool_id) AS mcp_tool_id,
    COALESCE(l.tool_version_id, v.tool_version_id) AS tool_version_id,
    COALESCE(l.remote_name, v.remote_name) AS remote_name,
    COALESCE(l.description_override, v.description_override) AS description_override,
    COALESCE(l.remote_description, v.remote_description) AS remote_description,
    COALESCE(l.tags, v.tags) AS tags,
    COALESCE(l.input_schema, v.input_schema) AS input_schema,
    COALESCE(l.output_schema, v.output_schema) AS output_schema,
    l.lexical_rank,
    v.vector_rank,
    (
      COALESCE(1.0 / (:rrf_k + l.lexical_rank), 0.0)
      + COALESCE(1.0 / (:rrf_k + v.vector_rank), 0.0)
    ) AS rrf_raw,
    COALESCE(l.agent_requires_confirmation, v.agent_requires_confirmation)
      AS agent_requires_confirmation,
    COALESCE(l.parameter_constraints, v.parameter_constraints)
      AS parameter_constraints,
    COALESCE(l.tool_policy_id, v.tool_policy_id) AS tool_policy_id,
    COALESCE(l.tool_policy_lock_version, v.tool_policy_lock_version)
      AS tool_policy_lock_version,
    COALESCE(l.policy_risk_class, v.policy_risk_class) AS policy_risk_class,
    COALESCE(l.policy_requires_confirmation, v.policy_requires_confirmation)
      AS policy_requires_confirmation,
    COALESCE(l.policy_requires_approval, v.policy_requires_approval)
      AS policy_requires_approval,
    COALESCE(l.approval_policy_id, v.approval_policy_id) AS approval_policy_id,
    COALESCE(l.allow_auto_select, v.allow_auto_select) AS allow_auto_select
  FROM lexical_top AS l
  FULL OUTER JOIN vector_top AS v
    ON v.tool_version_id = l.tool_version_id
),
ranked AS (
  SELECT
    m.*,
    ROW_NUMBER() OVER (
      ORDER BY m.rrf_raw DESC, m.tool_version_id ASC
    ) AS merge_rank
  FROM merged AS m
)
SELECT
  pg.profile_current,
  r.mcp_tool_id,
  r.tool_version_id,
  r.remote_name,
  r.description_override,
  r.remote_description,
  r.tags,
  r.input_schema,
  r.output_schema,
  r.lexical_rank,
  r.vector_rank,
  r.rrf_raw,
  r.agent_requires_confirmation,
  r.parameter_constraints,
  r.tool_policy_id,
  r.tool_policy_lock_version,
  r.policy_risk_class,
  r.policy_requires_confirmation,
  r.policy_requires_approval,
  r.approval_policy_id,
  r.allow_auto_select
FROM profile_gate AS pg
LEFT JOIN ranked AS r
  ON r.merge_rank <= :merged_limit
ORDER BY r.rrf_raw DESC NULLS LAST, r.tool_version_id ASC NULLS LAST
"""


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

        params = {
            "user_id": str(user_id),
            "agent_version_id": str(agent_version_id),
            "permission_code": TOOL_EXECUTE_PERMISSION,
            "profile_id": str(profile_id),
            "profile_lock_version": int(profile_lock_version),
            "query_text": query_text,
            "query_vector": str(query_vector),
            "lexical_limit": int(lexical_limit),
            "vector_limit": int(vector_limit),
            "merged_limit": int(merged_limit),
            "rrf_k": int(rrf_k),
            "allow_effect": AgentToolGrantEffect.ALLOW.value,
            "tool_active": MCPToolStatus.ACTIVE.value,
            "server_active": MCPServerStatus.ACTIVE.value,
            "version_valid": ToolVersionValidationStatus.VALID.value,
            "resource_type_tool": ResourceGrantResourceType.MCP_TOOL.value,
        }

        result = await self._session.execute(text(_AUTHORIZED_RETRIEVAL_SQL), params)
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
