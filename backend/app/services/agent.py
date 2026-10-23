"""Agent logical resource service (docs/05–06)."""

from __future__ import annotations

import re
import uuid

from fastapi import status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.writer import ACTION_AGENT_DELETE, AuditWriter
from app.core.errors import AppError
from app.domain.enums import (
    AgentStatus,
    AgentVersionStatus,
    AgentVisibility,
    AuditActorType,
    AuditResult,
)
from app.models.agent import Agent, AgentVersion
from app.models.conversation import AgentRequest, Conversation
from app.models.schedule import Schedule
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

    @staticmethod
    def is_visible(agent: Agent, viewer_id: uuid.UUID) -> bool:
        """Registry/read visibility — INTERNAL shared; PRIVATE/RESTRICTED owner-only."""

        if agent.visibility == AgentVisibility.INTERNAL:
            return True
        return agent.owner_id is not None and agent.owner_id == viewer_id

    async def require_visible(
        self, agent_id: uuid.UUID, *, viewer_id: uuid.UUID
    ) -> Agent:
        agent = await self._require(agent_id)
        if not self.is_visible(agent, viewer_id):
            raise AppError(
                code="NOT_FOUND",
                message="Agent not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return agent

    async def require_owned(
        self, agent_id: uuid.UUID, *, owner_id: uuid.UUID
    ) -> Agent:
        """Mutations require ownership (404, not 403 — match Schedule / Run submit)."""

        agent = await self._require(agent_id)
        if agent.owner_id is None or agent.owner_id != owner_id:
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

    async def create(self, data: AgentCreate, *, owner_id: uuid.UUID) -> Agent:
        code = _generate_agent_code(data.name)
        if await self._agents.get_by_code(code) is not None:
            code = _generate_agent_code(data.name)

        agent = await self._agents.create(
            code=code,
            name=data.name,
            description=data.description,
            visibility=str(data.visibility),
            status=AgentStatus.DRAFT,
            owner_id=owner_id,
            created_by=owner_id,
        )
        await self._session.commit()
        await self._session.refresh(agent)
        return agent

    async def list(
        self,
        *,
        viewer_id: uuid.UUID,
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
            viewer_id=viewer_id,
        )

    async def get(self, agent_id: uuid.UUID, *, viewer_id: uuid.UUID) -> Agent:
        return await self.require_visible(agent_id, viewer_id=viewer_id)

    async def update(
        self,
        agent_id: uuid.UUID,
        data: AgentUpdate,
        *,
        expected_lock_version: int,
        actor_id: uuid.UUID,
    ) -> Agent:
        agent = await self.require_owned(agent_id, owner_id=actor_id)
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
            updated_by=actor_id,
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

    async def delete(self, agent_id: uuid.UUID, *, actor_id: uuid.UUID) -> None:
        """Soft-delete own DRAFT Agents with no publish history and no live refs."""

        agent = await self.require_owned(agent_id, owner_id=actor_id)

        if AgentStatus(agent.status) != AgentStatus.DRAFT:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Only DRAFT Agents without publish history can be deleted.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if agent.current_version_id is not None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Agents with a current published version cannot be deleted.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if await self._versions.has_publish_history(agent_id):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Agents with publish history cannot be deleted.",
                status_code=status.HTTP_409_CONFLICT,
            )

        await self._assert_no_delete_blockers(agent_id)

        before = {
            "id": str(agent.id),
            "code": agent.code,
            "name": agent.name,
            "status": agent.status,
            "owner_id": str(agent.owner_id) if agent.owner_id else None,
            "visibility": agent.visibility,
        }
        await self._agents.soft_delete(agent, deleted_by=actor_id)
        await AuditWriter(self._session).append(
            actor_type=AuditActorType.USER,
            actor_id=str(actor_id),
            action=ACTION_AGENT_DELETE,
            result=AuditResult.SUCCESS,
            resource_type="AGENT",
            resource_id=str(agent_id),
            before_data=before,
            after_data={"deleted": True},
            change_set={"deleted_at": "set"},
            reason="owner_soft_delete_draft",
        )
        await self._session.commit()

    async def _assert_no_delete_blockers(self, agent_id: uuid.UUID) -> None:
        conversation_count = int(
            (
                await self._session.execute(
                    select(func.count())
                    .select_from(Conversation)
                    .where(
                        Conversation.agent_id == agent_id,
                        Conversation.deleted_at.is_(None),
                    )
                )
            ).scalar_one()
        )
        if conversation_count > 0:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Agent is referenced by conversations and cannot be deleted.",
                status_code=status.HTTP_409_CONFLICT,
            )

        version_ids = select(AgentVersion.id).where(AgentVersion.agent_id == agent_id)
        request_count = int(
            (
                await self._session.execute(
                    select(func.count())
                    .select_from(AgentRequest)
                    .where(AgentRequest.agent_version_id.in_(version_ids))
                )
            ).scalar_one()
        )
        if request_count > 0:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Agent is referenced by agent requests and cannot be deleted.",
                status_code=status.HTTP_409_CONFLICT,
            )

        schedule_count = int(
            (
                await self._session.execute(
                    select(func.count())
                    .select_from(Schedule)
                    .where(
                        Schedule.deleted_at.is_(None),
                        Schedule.agent_version_id.in_(version_ids),
                    )
                )
            ).scalar_one()
        )
        if schedule_count > 0:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Agent is referenced by schedules and cannot be deleted.",
                status_code=status.HTTP_409_CONFLICT,
            )

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
