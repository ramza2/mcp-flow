"""Parameter build run repository — docs/05 §10.6."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.parameter_build import ParameterBuildRun


class ParameterBuildRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        agent_request_id: uuid.UUID,
        tool_selection_run_id: uuid.UUID,
        tool_version_id: uuid.UUID,
        input_schema_snapshot: dict[str, Any],
        parameter_constraints_snapshot: dict[str, Any] | None,
        bindings_snapshot: dict[str, Any],
        missing_fields: list[str],
        is_complete: bool,
    ) -> ParameterBuildRun:
        if is_complete and missing_fields:
            raise ValueError("complete ParameterBuildRun cannot have missing_fields")
        row = ParameterBuildRun(
            id=uuid.uuid4(),
            agent_request_id=agent_request_id,
            tool_selection_run_id=tool_selection_run_id,
            tool_version_id=tool_version_id,
            input_schema_snapshot=input_schema_snapshot,
            parameter_constraints_snapshot=parameter_constraints_snapshot,
            bindings_snapshot=bindings_snapshot,
            missing_fields=list(missing_fields),
            is_complete=is_complete,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_by_id(self, run_id: uuid.UUID) -> ParameterBuildRun | None:
        stmt = select(ParameterBuildRun).where(ParameterBuildRun.id == run_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_latest_for_agent_request(
        self, agent_request_id: uuid.UUID
    ) -> ParameterBuildRun | None:
        stmt = (
            select(ParameterBuildRun)
            .where(ParameterBuildRun.agent_request_id == agent_request_id)
            .order_by(
                ParameterBuildRun.created_at.desc(),
                ParameterBuildRun.id.desc(),
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_latest_complete_for_agent_request(
        self, agent_request_id: uuid.UUID
    ) -> ParameterBuildRun | None:
        stmt = (
            select(ParameterBuildRun)
            .where(
                ParameterBuildRun.agent_request_id == agent_request_id,
                ParameterBuildRun.is_complete.is_(True),
            )
            .order_by(
                ParameterBuildRun.created_at.desc(),
                ParameterBuildRun.id.desc(),
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()
