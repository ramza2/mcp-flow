"""Tool selection run/candidate repository — docs/05 §10.5."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tool_selection import ToolSelectionCandidate, ToolSelectionRun


class ToolSelectionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_run(
        self,
        *,
        agent_request_id: uuid.UUID,
        agent_version_id: uuid.UUID,
        embedding_profile_id: uuid.UUID,
        llm_profile_id: uuid.UUID,
        registry_snapshot: dict[str, Any],
        model_snapshot: dict[str, Any],
        threshold_snapshot: dict[str, Any],
        decision: str,
        selected_tool_version_id: uuid.UUID | None,
        confidence: float | None,
        candidate_margin: float | None,
        required_input_coverage: float | None,
        reason_summary: str | None,
        ambiguities: list[str],
    ) -> ToolSelectionRun:
        row = ToolSelectionRun(
            id=uuid.uuid4(),
            agent_request_id=agent_request_id,
            agent_version_id=agent_version_id,
            embedding_profile_id=embedding_profile_id,
            llm_profile_id=llm_profile_id,
            registry_snapshot=registry_snapshot,
            model_snapshot=model_snapshot,
            threshold_snapshot=threshold_snapshot,
            decision=decision,
            selected_tool_version_id=selected_tool_version_id,
            confidence=confidence,
            candidate_margin=candidate_margin,
            required_input_coverage=required_input_coverage,
            reason_summary=reason_summary,
            ambiguities=list(ambiguities),
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def add_candidates(
        self,
        *,
        tool_selection_run_id: uuid.UUID,
        candidates: list[dict[str, Any]],
    ) -> list[ToolSelectionCandidate]:
        rows: list[ToolSelectionCandidate] = []
        for item in candidates:
            row = ToolSelectionCandidate(
                id=uuid.uuid4(),
                tool_selection_run_id=tool_selection_run_id,
                tool_version_id=item["tool_version_id"],
                input_rank=int(item["input_rank"]),
                retrieval_score=float(item["retrieval_score"]),
                llm_fit_score=item.get("llm_fit_score"),
                reason_summary=item.get("reason_summary"),
                risk_class=str(item["risk_class"]),
            )
            self._session.add(row)
            rows.append(row)
        await self._session.flush()
        return rows

    async def get_latest_for_agent_request(
        self, agent_request_id: uuid.UUID
    ) -> ToolSelectionRun | None:
        stmt = (
            select(ToolSelectionRun)
            .where(ToolSelectionRun.agent_request_id == agent_request_id)
            .order_by(ToolSelectionRun.created_at.desc(), ToolSelectionRun.id.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_candidates_for_run(
        self, tool_selection_run_id: uuid.UUID
    ) -> list[ToolSelectionCandidate]:
        stmt = (
            select(ToolSelectionCandidate)
            .where(
                ToolSelectionCandidate.tool_selection_run_id == tool_selection_run_id
            )
            .order_by(
                ToolSelectionCandidate.input_rank.asc(),
                ToolSelectionCandidate.tool_version_id.asc(),
            )
        )
        return list((await self._session.execute(stmt)).scalars().all())
