"""MCP Tool and ToolVersion repositories (docs/05 mcp_tools / mcp_tool_versions)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mcp import MCPTool, MCPToolVersion
from app.schemas.common_page import ALLOWED_TOOL_SORT, parse_sort


class MCPToolRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _live_tools(self) -> Select[tuple[MCPTool]]:
        return select(MCPTool).where(MCPTool.deleted_at.is_(None))

    async def get(self, tool_id: uuid.UUID) -> MCPTool | None:
        stmt = self._live_tools().where(MCPTool.id == tool_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def lock_for_update(self, tool_id: uuid.UUID) -> MCPTool | None:
        """Lock live MCP Tool row for short critical sections (SELECT ... FOR UPDATE)."""

        stmt = self._live_tools().where(MCPTool.id == tool_id).with_for_update()
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_atomic(
        self,
        tool_id: uuid.UUID,
        *,
        expected_lock_version: int,
        updated_by: uuid.UUID | None = None,
        **fields: Any,
    ) -> MCPTool | None:
        """Compare-and-swap update on ``lock_version`` (docs/05 optimistic lock)."""

        values: dict[str, Any] = {
            key: value
            for key, value in fields.items()
            if hasattr(MCPTool, key) and key not in {"id", "lock_version"}
        }
        values["lock_version"] = MCPTool.lock_version + 1
        values["updated_at"] = func.now()
        if updated_by is not None:
            values["updated_by"] = updated_by

        stmt = (
            update(MCPTool)
            .where(
                MCPTool.id == tool_id,
                MCPTool.lock_version == expected_lock_version,
                MCPTool.deleted_at.is_(None),
            )
            .values(**values)
            .returning(MCPTool)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        await self._session.refresh(row)
        return row

    async def update_status_atomic(
        self,
        tool_id: uuid.UUID,
        *,
        expected_lock_version: int,
        new_status: str,
        allowed_from_statuses: set[str] | frozenset[str] | list[str],
        updated_by: uuid.UUID | None = None,
    ) -> MCPTool | None:
        """Atomic status transition with lock_version CAS and status guard in WHERE."""

        values: dict[str, Any] = {
            "status": new_status,
            "lock_version": MCPTool.lock_version + 1,
            "updated_at": func.now(),
        }
        if updated_by is not None:
            values["updated_by"] = updated_by

        stmt = (
            update(MCPTool)
            .where(
                MCPTool.id == tool_id,
                MCPTool.lock_version == expected_lock_version,
                MCPTool.deleted_at.is_(None),
                MCPTool.status.in_(list(allowed_from_statuses)),
            )
            .values(**values)
            .returning(MCPTool)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        await self._session.refresh(row)
        return row

    async def get_by_server_and_remote_name(
        self,
        mcp_server_id: uuid.UUID,
        remote_name: str,
    ) -> MCPTool | None:
        stmt = self._live_tools().where(
            MCPTool.mcp_server_id == mcp_server_id,
            MCPTool.remote_name == remote_name,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_tool(
        self,
        *,
        mcp_server_id: uuid.UUID,
        remote_name: str,
        display_name: str | None = None,
        description_override: str | None = None,
        tags: list[Any] | None = None,
        status: str = "DISCOVERED",
        created_by: uuid.UUID | None = None,
    ) -> MCPTool:
        now = datetime.now(UTC)
        tool = MCPTool(
            mcp_server_id=mcp_server_id,
            remote_name=remote_name,
            display_name=display_name,
            description_override=description_override,
            tags=tags,
            status=status,
            first_seen_at=now,
            last_seen_at=now,
            created_by=created_by,
            updated_by=created_by,
        )
        self._session.add(tool)
        await self._session.flush()
        await self._session.refresh(tool)
        return tool

    async def get_or_create_tool(
        self,
        *,
        mcp_server_id: uuid.UUID,
        remote_name: str,
        created_by: uuid.UUID | None = None,
    ) -> tuple[MCPTool, bool]:
        existing = await self.get_by_server_and_remote_name(mcp_server_id, remote_name)
        if existing is not None:
            return existing, False
        created = await self.create_tool(
            mcp_server_id=mcp_server_id,
            remote_name=remote_name,
            created_by=created_by,
        )
        return created, True

    async def list_tools(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        mcp_server_id: uuid.UUID | None = None,
        status: str | None = None,
        q: str | None = None,
        sort: str = "-updated_at",
    ) -> tuple[list[MCPTool], int]:
        field, direction = parse_sort(sort, allowed=ALLOWED_TOOL_SORT)
        stmt = self._live_tools()

        if mcp_server_id is not None:
            stmt = stmt.where(MCPTool.mcp_server_id == mcp_server_id)

        if status:
            statuses = [part.strip() for part in status.split(",") if part.strip()]
            if len(statuses) == 1:
                stmt = stmt.where(MCPTool.status == statuses[0])
            elif statuses:
                stmt = stmt.where(MCPTool.status.in_(statuses))

        if q:
            pattern = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(
                    MCPTool.remote_name.ilike(pattern),
                    MCPTool.display_name.ilike(pattern),
                )
            )

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())

        sort_col = getattr(MCPTool, field)
        order = sort_col.desc() if direction == "desc" else sort_col.asc()
        offset = (page - 1) * page_size
        rows_stmt = stmt.order_by(order).offset(offset).limit(page_size)
        rows = list((await self._session.execute(rows_stmt)).scalars().all())
        return rows, total

    async def touch_last_seen(self, tool: MCPTool) -> MCPTool:
        tool.last_seen_at = datetime.now(UTC)
        await self._session.flush()
        await self._session.refresh(tool)
        return tool

    async def next_version_no(self, mcp_tool_id: uuid.UUID) -> int:
        stmt = select(func.coalesce(func.max(MCPToolVersion.version_no), 0)).where(
            MCPToolVersion.mcp_tool_id == mcp_tool_id
        )
        current = int((await self._session.execute(stmt)).scalar_one())
        return current + 1

    async def get_version(self, version_id: uuid.UUID) -> MCPToolVersion | None:
        stmt = select(MCPToolVersion).where(MCPToolVersion.id == version_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_version_by_hash(
        self,
        mcp_tool_id: uuid.UUID,
        content_hash: str,
    ) -> MCPToolVersion | None:
        stmt = select(MCPToolVersion).where(
            MCPToolVersion.mcp_tool_id == mcp_tool_id,
            MCPToolVersion.content_hash == content_hash,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_version(
        self,
        *,
        mcp_tool_id: uuid.UUID,
        version_no: int,
        content_hash: str,
        validation_status: str,
        remote_description: str | None = None,
        input_schema: Any | None = None,
        output_schema: Any | None = None,
        annotations: dict[str, Any] | None = None,
        raw_descriptor: dict[str, Any] | None = None,
        schema_dialect: str | None = None,
        validation_errors: list[Any] | None = None,
        created_by: uuid.UUID | None = None,
    ) -> MCPToolVersion:
        version = MCPToolVersion(
            mcp_tool_id=mcp_tool_id,
            version_no=version_no,
            remote_description=remote_description,
            input_schema=input_schema,
            output_schema=output_schema,
            annotations=annotations,
            raw_descriptor=raw_descriptor,
            schema_dialect=schema_dialect,
            content_hash=content_hash,
            validation_status=validation_status,
            validation_errors=validation_errors,
            created_by=created_by,
        )
        self._session.add(version)
        await self._session.flush()
        await self._session.refresh(version)
        return version

    async def list_versions(
        self,
        *,
        mcp_tool_id: uuid.UUID,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[MCPToolVersion], int]:
        base = select(MCPToolVersion).where(MCPToolVersion.mcp_tool_id == mcp_tool_id)
        count_stmt = select(func.count()).select_from(base.order_by(None).subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())
        offset = (page - 1) * page_size
        rows_stmt = (
            base.order_by(MCPToolVersion.version_no.desc()).offset(offset).limit(page_size)
        )
        rows = list((await self._session.execute(rows_stmt)).scalars().all())
        return rows, total
