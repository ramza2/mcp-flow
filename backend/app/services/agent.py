"""Agent logical resource service (docs/05–06)."""

from __future__ import annotations

import re
import uuid

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import AgentStatus, AgentVersionStatus, AgentVisibility
from app.models.agent import Agent
from app.repositories.agent import AgentRepository
from app.repositories.agent_version import AgentVersionRepository
from app.schemas.agent import AgentCreate, AgentUpdate

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify_name(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.lower().strip()).strip("-")
    return (slug[:48] if slug else "agent")


def _generate_agent_code(name: str) -> str:
    return f"{_slugify_name(name)}-{uuid.uuid4().hex[:8]}"


class AgentService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._agents = AgentRepository(session)
        self._versions = AgentVersionRepository(session)

    async def _require(self, agent_id: uuid.UUID) -> Agent:
        agent = await self._agents.get(agent_id)
        if agent is None:
            raise AppError(
                code="NOT_FOUND",
                message="Agent not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return agent

    def _raise_version_conflict(self) -> None:
        raise AppError(
            code="RESOURCE_VERSION_CONFLICT",
            message="Agent lock_version does not match.",
            status_code=status.HTTP_409_CONFLICT,
        )

    async def create(self, data: AgentCreate) -> Agent:
        code = _generate_agent_code(data.name)
        if await self._agents.get_by_code(code) is not None:
            code = _generate_agent_code(data.name)

        agent = await self._agents.create(
            code=code,
            name=data.name,
            description=data.description,
            visibility=str(data.visibility),
            status=AgentStatus.DRAFT,
        )
        await self._session.commit()
        await self._session.refresh(agent)
        return agent

    async def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status_filter: str | None = None,
        q: str | None = None,
        sort: str = "-updated_at",
    ) -> tuple[list[Agent], int]:
        return await self._agents.list(
            page=page,
            page_size=page_size,
            status=status_filter,
            q=q,
            sort=sort,
        )

    async def get(self, agent_id: uuid.UUID) -> Agent:
        return await self._require(agent_id)

    async def update(
        self,
        agent_id: uuid.UUID,
        data: AgentUpdate,
        *,
        expected_lock_version: int,
    ) -> Agent:
        agent = await self._require(agent_id)
        payload = data.model_dump(exclude_unset=True, exclude={"lock_version"})

        if "status" in payload:
            new_status = AgentStatus(payload["status"])
            await self._assert_status_transition(agent, new_status)
            payload["status"] = str(new_status)
        if "visibility" in payload and payload["visibility"] is not None:
            payload["visibility"] = str(AgentVisibility(payload["visibility"]))

        updated = await self._agents.update_atomic(
            agent_id,
            expected_lock_version=expected_lock_version,
            **payload,
        )
        if updated is None:
            current = await self._agents.get(agent_id)
            if current is None:
                raise AppError(
                    code="NOT_FOUND",
                    message="Agent not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            self._raise_version_conflict()

        await self._session.commit()
        await self._session.refresh(updated)
        return updated

    async def _assert_status_transition(
        self, agent: Agent, new_status: AgentStatus
    ) -> None:
        current = AgentStatus(agent.status)
        if current == new_status:
            return

        if current == AgentStatus.ARCHIVED:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="ARCHIVED Agent status cannot be changed.",
                status_code=status.HTTP_409_CONFLICT,
            )

        if new_status == AgentStatus.ACTIVE:
            if agent.current_version_id is None:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        "Cannot set Agent ACTIVE without a current published version."
                    ),
                    status_code=status.HTTP_409_CONFLICT,
                )
            version = await self._versions.get(agent.current_version_id)
            if version is None or version.status != AgentVersionStatus.PUBLISHED:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        "Cannot set Agent ACTIVE unless current_version is PUBLISHED."
                    ),
                    status_code=status.HTTP_409_CONFLICT,
                )
