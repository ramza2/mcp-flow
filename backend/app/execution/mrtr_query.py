"""Safe MRTR input-request projections for API (docs/06 §15).

Never expose requestState, response_payload secrets, or MCP credentials.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import McpInputRequestStatus, UserStatus
from app.models.mcp_input_request import MCPInputRequest
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_input_request import MCPInputRequestRepository
from sqlalchemy import select

from app.models.auth import User


@dataclass(frozen=True, slots=True)
class SafeMrtrInputRequest:
    id: uuid.UUID
    status: str
    source: str
    execution_id: uuid.UUID
    step_execution_id: uuid.UUID
    round_no: int
    input_requests: dict[str, Any]
    expires_at: datetime
    requested_at: datetime
    answered_at: datetime | None


def _safe_input_requests(raw: Any) -> dict[str, Any]:
    """Return a UI-safe copy of input_requests (no requestState nesting)."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key in {"requestState", "request_state"}:
            continue
        if isinstance(value, dict):
            cleaned = {
                k: v
                for k, v in value.items()
                if k not in {"requestState", "request_state", "secret_id"}
            }
            out[str(key)] = cleaned
        else:
            out[str(key)] = value
    return out


def project_safe(row: MCPInputRequest) -> SafeMrtrInputRequest:
    return SafeMrtrInputRequest(
        id=row.id,
        status=row.status,
        source="MCP_MRTR",
        execution_id=row.execution_id,
        step_execution_id=row.step_execution_id,
        round_no=row.round_no,
        input_requests=_safe_input_requests(row.input_requests),
        expires_at=row.expires_at,
        requested_at=row.requested_at,
        answered_at=row.answered_at,
    )


class MrtrQueryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._executions = ExecutionRepository(session)
        self._inputs = MCPInputRequestRepository(session)

    async def list_for_execution(
        self,
        *,
        execution_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        status: str | None = None,
    ) -> list[SafeMrtrInputRequest]:
        await self._assert_actor_active(actor_user_id)
        execution = await self._require_requester_execution(
            execution_id, actor_user_id
        )
        steps = await self._executions.list_steps(execution.id)
        if not steps:
            return []
        # AgentRequest executions have one step; list across all for safety.
        items: list[SafeMrtrInputRequest] = []
        for step in steps:
            rows = await self._inputs.list_for_step(
                execution_id=execution.id, step_execution_id=step.id
            )
            for row in rows:
                if status is not None and row.status != status:
                    continue
                items.append(project_safe(row))
        items.sort(key=lambda i: (i.round_no, i.requested_at))
        return items

    async def get_for_execution(
        self,
        *,
        execution_id: uuid.UUID,
        input_request_id: uuid.UUID,
        actor_user_id: uuid.UUID,
    ) -> SafeMrtrInputRequest:
        await self._assert_actor_active(actor_user_id)
        execution = await self._require_requester_execution(
            execution_id, actor_user_id
        )
        row = await self._inputs.get(input_request_id)
        if row is None or row.execution_id != execution.id:
            raise AppError(
                code="NOT_FOUND",
                message="MCPInputRequest not found.",
                status_code=404,
            )
        return project_safe(row)

    async def get_open_for_execution(
        self,
        *,
        execution_id: uuid.UUID,
        actor_user_id: uuid.UUID,
    ) -> SafeMrtrInputRequest | None:
        """Return the single OPEN request when Execution is WAITING_INPUT."""
        items = await self.list_for_execution(
            execution_id=execution_id,
            actor_user_id=actor_user_id,
            status=McpInputRequestStatus.OPEN.value,
        )
        if not items:
            return None
        if len(items) > 1:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Multiple OPEN MCPInputRequest rows.",
                status_code=409,
            )
        return items[0]

    async def _require_requester_execution(
        self, execution_id: uuid.UUID, actor_user_id: uuid.UUID
    ):
        execution = await self._executions.get(execution_id)
        if execution is None or execution.requester_id != actor_user_id:
            raise AppError(
                code="NOT_FOUND",
                message="Execution not found.",
                status_code=404,
            )
        return execution

    async def _assert_actor_active(self, user_id: uuid.UUID) -> None:
        stmt = select(User).where(User.id == user_id)
        user = (await self._session.execute(stmt)).scalar_one_or_none()
        if user is None or user.status != UserStatus.ACTIVE.value:
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Actor is not an ACTIVE user.",
                status_code=403,
            )
