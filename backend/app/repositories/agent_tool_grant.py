"""Agent Tool Grant repository (docs/05 agent_tool_grants)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import AgentToolGrant


class AgentToolGrantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_version(self, agent_version_id: uuid.UUID) -> list[AgentToolGrant]:
        stmt = (
            select(AgentToolGrant)
            .where(AgentToolGrant.agent_version_id == agent_version_id)
            .order_by(AgentToolGrant.mcp_tool_id.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def replace_all(
        self,
        agent_version_id: uuid.UUID,
        items: list[dict[str, Any]],
        *,
        created_by: uuid.UUID | None = None,
    ) -> list[AgentToolGrant]:
        await self._session.execute(
            delete(AgentToolGrant).where(
                AgentToolGrant.agent_version_id == agent_version_id
            )
        )
        grants: list[AgentToolGrant] = []
        for item in items:
            grant = AgentToolGrant(
                agent_version_id=agent_version_id,
                mcp_tool_id=item["mcp_tool_id"],
                effect=item["effect"],
                parameter_constraints=item.get("parameter_constraints"),
                requires_confirmation=bool(item.get("requires_confirmation", False)),
                created_by=created_by,
            )
            self._session.add(grant)
            grants.append(grant)
        await self._session.flush()
        for grant in grants:
            await self._session.refresh(grant)
        return grants

    async def copy_from_version(
        self,
        *,
        source_version_id: uuid.UUID,
        target_version_id: uuid.UUID,
        created_by: uuid.UUID | None = None,
    ) -> list[AgentToolGrant]:
        source = await self.list_for_version(source_version_id)
        payload = [
            {
                "mcp_tool_id": grant.mcp_tool_id,
                "effect": grant.effect,
                "parameter_constraints": grant.parameter_constraints,
                "requires_confirmation": grant.requires_confirmation,
            }
            for grant in source
        ]
        return await self.replace_all(
            target_version_id, payload, created_by=created_by
        )
