"""WorkflowVersionToolRef repository (docs/05 workflow_version_tool_refs)."""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workflow import WorkflowVersionToolRef


class WorkflowVersionToolRefRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_version(
        self, workflow_version_id: uuid.UUID
    ) -> list[WorkflowVersionToolRef]:
        stmt = (
            select(WorkflowVersionToolRef)
            .where(
                WorkflowVersionToolRef.workflow_version_id == workflow_version_id
            )
            .order_by(WorkflowVersionToolRef.step_key.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def clear(self, workflow_version_id: uuid.UUID) -> None:
        await self._session.execute(
            delete(WorkflowVersionToolRef).where(
                WorkflowVersionToolRef.workflow_version_id == workflow_version_id
            )
        )
        await self._session.flush()

    async def replace_all(
        self,
        workflow_version_id: uuid.UUID,
        items: list[tuple[str, uuid.UUID]],
    ) -> list[WorkflowVersionToolRef]:
        """Replace tool refs. ``items`` are (step_key, mcp_tool_version_id) in Plan order."""

        await self.clear(workflow_version_id)
        refs: list[WorkflowVersionToolRef] = []
        for step_key, mcp_tool_version_id in items:
            ref = WorkflowVersionToolRef(
                workflow_version_id=workflow_version_id,
                step_key=step_key,
                mcp_tool_version_id=mcp_tool_version_id,
            )
            self._session.add(ref)
            refs.append(ref)
        await self._session.flush()
        for ref in refs:
            await self._session.refresh(ref)
        return refs
