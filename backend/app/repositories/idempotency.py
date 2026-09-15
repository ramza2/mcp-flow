"""API idempotency record repository — docs/05 §15.3."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.idempotency import ApiIdempotencyRecord


class IdempotencyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self,
        *,
        principal_key: str,
        operation_scope: str,
        idempotency_key: str,
    ) -> ApiIdempotencyRecord | None:
        stmt = select(ApiIdempotencyRecord).where(
            ApiIdempotencyRecord.principal_key == principal_key,
            ApiIdempotencyRecord.operation_scope == operation_scope,
            ApiIdempotencyRecord.idempotency_key == idempotency_key,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def create_completed(
        self,
        *,
        principal_key: str,
        operation_scope: str,
        idempotency_key: str,
        request_hash: str,
        response_status: int,
        response_body: dict[str, Any],
        resource_type: str,
        resource_id: uuid.UUID,
        completed_at: datetime,
    ) -> ApiIdempotencyRecord:
        row = ApiIdempotencyRecord(
            principal_key=principal_key,
            operation_scope=operation_scope,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            status="COMPLETED",
            response_status=response_status,
            response_body=dict(response_body),
            resource_type=resource_type,
            resource_id=resource_id,
            completed_at=completed_at,
            expires_at=None,
        )
        self._session.add(row)
        await self._session.flush()
        return row
