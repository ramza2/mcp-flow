"""Workflow logical resource service (docs/05–06)."""

from __future__ import annotations

import re
import uuid

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import WorkflowStatus, WorkflowVersionStatus, WorkflowVisibility
from app.models.workflow import Workflow
from app.repositories.workflow import WorkflowRepository
from app.repositories.workflow_version import WorkflowVersionRepository
from app.schemas.workflow import WorkflowCreate, WorkflowUpdate

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify_name(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.lower().strip()).strip("-")
    return (slug[:48] if slug else "workflow")


def _generate_workflow_code(name: str) -> str:
    return f"{_slugify_name(name)}-{uuid.uuid4().hex[:8]}"


class WorkflowService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._workflows = WorkflowRepository(session)
        self._versions = WorkflowVersionRepository(session)

    async def _require(self, workflow_id: uuid.UUID) -> Workflow:
        workflow = await self._workflows.get(workflow_id)
        if workflow is None:
            raise AppError(
                code="NOT_FOUND",
                message="Workflow not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return workflow

    def _raise_version_conflict(self) -> None:
        raise AppError(
            code="RESOURCE_VERSION_CONFLICT",
            message="Workflow lock_version does not match.",
            status_code=status.HTTP_409_CONFLICT,
        )

    async def create(self, data: WorkflowCreate) -> Workflow:
        code = _generate_workflow_code(data.name)
        if await self._workflows.get_by_code(code) is not None:
            code = _generate_workflow_code(data.name)

        workflow = await self._workflows.create(
            code=code,
            name=data.name,
            description=data.description,
            visibility=str(data.visibility),
            status=WorkflowStatus.DRAFT,
        )
        await self._session.commit()
        await self._session.refresh(workflow)
        return workflow

    async def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status_filter: str | None = None,
        q: str | None = None,
        sort: str = "-updated_at",
    ) -> tuple[list[Workflow], int]:
        return await self._workflows.list(
            page=page,
            page_size=page_size,
            status=status_filter,
            q=q,
            sort=sort,
        )

    async def get(self, workflow_id: uuid.UUID) -> Workflow:
        return await self._require(workflow_id)

    async def update(
        self,
        workflow_id: uuid.UUID,
        data: WorkflowUpdate,
        *,
        expected_lock_version: int,
    ) -> Workflow:
        workflow = await self._require(workflow_id)
        payload = data.model_dump(exclude_unset=True, exclude={"lock_version"})

        if "status" in payload:
            new_status = WorkflowStatus(payload["status"])
            await self._assert_status_transition(workflow, new_status)
            payload["status"] = str(new_status)
        if "visibility" in payload and payload["visibility"] is not None:
            payload["visibility"] = str(WorkflowVisibility(payload["visibility"]))

        updated = await self._workflows.update_atomic(
            workflow_id,
            expected_lock_version=expected_lock_version,
            **payload,
        )
        if updated is None:
            current = await self._workflows.get(workflow_id)
            if current is None:
                raise AppError(
                    code="NOT_FOUND",
                    message="Workflow not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            self._raise_version_conflict()

        await self._session.commit()
        await self._session.refresh(updated)
        return updated

    async def _assert_status_transition(
        self, workflow: Workflow, new_status: WorkflowStatus
    ) -> None:
        current = WorkflowStatus(workflow.status)
        if current == new_status:
            return

        if current == WorkflowStatus.ARCHIVED:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="ARCHIVED Workflow status cannot be changed.",
                status_code=status.HTTP_409_CONFLICT,
            )

        if new_status == WorkflowStatus.ACTIVE:
            if workflow.current_version_id is None:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        "Cannot set Workflow ACTIVE without a current published "
                        "version."
                    ),
                    status_code=status.HTTP_409_CONFLICT,
                )
            version = await self._versions.get(workflow.current_version_id)
            if (
                version is None
                or version.workflow_id != workflow.id
                or version.status != WorkflowVersionStatus.PUBLISHED
            ):
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        "Cannot set Workflow ACTIVE unless current_version is "
                        "PUBLISHED and belongs to this Workflow."
                    ),
                    status_code=status.HTTP_409_CONFLICT,
                )
