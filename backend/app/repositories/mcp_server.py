"""MCP Server repository (docs/05 mcp_servers)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mcp import MCPServer
from app.schemas.common_page import ALLOWED_SERVER_SORT, parse_sort


class MCPServerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _live(self) -> Select[tuple[MCPServer]]:
        return select(MCPServer).where(MCPServer.deleted_at.is_(None))

    async def create(
        self,
        *,
        code: str,
        name: str,
        description: str | None = None,
        transport_type: str,
        endpoint_url: str | None = None,
        stdio_manifest_id: str | None = None,
        transport_config: dict[str, Any] | None = None,
        auth_type: str = "NONE",
        auth_secret_id: uuid.UUID | None = None,
        status: str = "DRAFT",
        protocol_era: str = "CURRENT",
        connect_timeout_ms: int = 10000,
        call_timeout_ms: int = 60000,
        max_concurrency: int = 5,
        created_by: uuid.UUID | None = None,
    ) -> MCPServer:
        server = MCPServer(
            code=code,
            name=name,
            description=description,
            transport_type=transport_type,
            endpoint_url=endpoint_url,
            stdio_manifest_id=stdio_manifest_id,
            transport_config=transport_config,
            auth_type=auth_type,
            auth_secret_id=auth_secret_id,
            status=status,
            protocol_era=protocol_era,
            connect_timeout_ms=connect_timeout_ms,
            call_timeout_ms=call_timeout_ms,
            max_concurrency=max_concurrency,
            created_by=created_by,
            updated_by=created_by,
        )
        self._session.add(server)
        await self._session.flush()
        await self._session.refresh(server)
        return server

    async def get(self, server_id: uuid.UUID) -> MCPServer | None:
        stmt = self._live().where(MCPServer.id == server_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_code(self, code: str) -> MCPServer | None:
        stmt = self._live().where(MCPServer.code == code)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: str | None = None,
        transport_type: str | None = None,
        q: str | None = None,
        sort: str = "-updated_at",
    ) -> tuple[list[MCPServer], int]:
        field, direction = parse_sort(sort, allowed=ALLOWED_SERVER_SORT)
        stmt = self._live()

        if status:
            statuses = [part.strip() for part in status.split(",") if part.strip()]
            if len(statuses) == 1:
                stmt = stmt.where(MCPServer.status == statuses[0])
            elif statuses:
                stmt = stmt.where(MCPServer.status.in_(statuses))

        if transport_type:
            transports = [part.strip() for part in transport_type.split(",") if part.strip()]
            if len(transports) == 1:
                stmt = stmt.where(MCPServer.transport_type == transports[0])
            elif transports:
                stmt = stmt.where(MCPServer.transport_type.in_(transports))

        if q:
            pattern = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(
                    MCPServer.name.ilike(pattern),
                    MCPServer.code.ilike(pattern),
                    MCPServer.description.ilike(pattern),
                )
            )

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())

        sort_col = getattr(MCPServer, field)
        order = sort_col.desc() if direction == "desc" else sort_col.asc()
        offset = (page - 1) * page_size
        rows_stmt = stmt.order_by(order).offset(offset).limit(page_size)
        rows = list((await self._session.execute(rows_stmt)).scalars().all())
        return rows, total

    async def update(
        self,
        server: MCPServer,
        *,
        expected_lock_version: int | None = None,
        updated_by: uuid.UUID | None = None,
        **fields: Any,
    ) -> MCPServer:
        if expected_lock_version is not None and server.lock_version != expected_lock_version:
            raise ValueError("RESOURCE_VERSION_CONFLICT")

        for key, value in fields.items():
            if not hasattr(server, key):
                continue
            setattr(server, key, value)

        server.lock_version = int(server.lock_version) + 1
        if updated_by is not None:
            server.updated_by = updated_by
        await self._session.flush()
        await self._session.refresh(server)
        return server

    async def soft_delete(
        self,
        server: MCPServer,
        *,
        deleted_by: uuid.UUID | None = None,
    ) -> MCPServer:
        server.deleted_at = datetime.now(UTC)
        server.lock_version = int(server.lock_version) + 1
        if deleted_by is not None:
            server.updated_by = deleted_by
        await self._session.flush()
        await self._session.refresh(server)
        return server
