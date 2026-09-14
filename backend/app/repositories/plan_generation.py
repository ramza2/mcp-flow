"""Plan generation run repository — docs/05 §10.7."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.plan_generation import PlanGenerationRun, PlanGenerationToolRef


class PlanGenerationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_run(
        self,
        *,
        agent_request_id: uuid.UUID,
        parameter_build_run_id: uuid.UUID,
        agent_version_id: uuid.UUID,
        plan_schema_version: str,
        plan_snapshot: dict[str, Any],
        plan_hash: str,
        planning_settings_snapshot: dict[str, Any],
    ) -> PlanGenerationRun:
        if len(plan_hash) != 64:
            raise ValueError("plan_hash must be 64 hex characters")
        row = PlanGenerationRun(
            id=uuid.uuid4(),
            agent_request_id=agent_request_id,
            parameter_build_run_id=parameter_build_run_id,
            agent_version_id=agent_version_id,
            plan_schema_version=plan_schema_version,
            plan_snapshot=plan_snapshot,
            plan_hash=plan_hash,
            planning_settings_snapshot=dict(planning_settings_snapshot),
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def add_tool_refs(
        self,
        *,
        plan_generation_run_id: uuid.UUID,
        refs: list[dict[str, Any]],
    ) -> list[PlanGenerationToolRef]:
        rows: list[PlanGenerationToolRef] = []
        for item in refs:
            step_key = str(item["step_key"]).strip()
            if not step_key:
                raise ValueError("step_key must be non-empty")
            row = PlanGenerationToolRef(
                plan_generation_run_id=plan_generation_run_id,
                step_key=step_key,
                mcp_tool_version_id=item["mcp_tool_version_id"],
            )
            self._session.add(row)
            rows.append(row)
        await self._session.flush()
        return rows

    async def get_by_id(self, run_id: uuid.UUID) -> PlanGenerationRun | None:
        stmt = select(PlanGenerationRun).where(PlanGenerationRun.id == run_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_latest_for_agent_request(
        self, agent_request_id: uuid.UUID
    ) -> PlanGenerationRun | None:
        stmt = (
            select(PlanGenerationRun)
            .where(PlanGenerationRun.agent_request_id == agent_request_id)
            .order_by(
                PlanGenerationRun.created_at.desc(),
                PlanGenerationRun.id.desc(),
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_tool_refs_for_run(
        self, plan_generation_run_id: uuid.UUID
    ) -> list[PlanGenerationToolRef]:
        stmt = (
            select(PlanGenerationToolRef)
            .where(
                PlanGenerationToolRef.plan_generation_run_id == plan_generation_run_id
            )
            .order_by(PlanGenerationToolRef.step_key.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())
