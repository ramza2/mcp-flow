"""User-facing Execution cancellation service (docs/06 §14.4)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.writer import ACTION_EXECUTION_CANCEL, AuditWriter
from app.core.errors import AppError
from app.domain.enums import AuditActorType, AuditResult, UserStatus
from app.execution.cancellation import (
    CancellationMode,
    CancellationOutcome,
    apply_cancellation_locked,
)
from app.models.execution import Execution
from app.repositories.execution import ExecutionRepository
from app.repositories.user import UserRepository
from app.services.authorization import AuthorizationResolver

_EXECUTION_CANCEL = "execution.cancel"


class ExecutionCancellationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._executions = ExecutionRepository(session)
        self._users = UserRepository(session)
        self._authz = AuthorizationResolver(session)
        self._audit = AuditWriter(session)

    async def _assert_actor_may_cancel(self, actor_user_id: uuid.UUID) -> None:
        user = await self._users.get(actor_user_id)
        if user is None or user.status != UserStatus.ACTIVE.value:
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Actor is not an ACTIVE user.",
                status_code=status.HTTP_403_FORBIDDEN,
            )
        if not await self._authz.has_permission(actor_user_id, _EXECUTION_CANCEL):
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Missing execution.cancel permission.",
                status_code=status.HTTP_403_FORBIDDEN,
            )

    async def _require_owned_execution(
        self, execution_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> Execution:
        execution = await self._executions.lock_execution(execution_id)
        if execution is None or execution.requester_id != actor_user_id:
            raise AppError(
                code="NOT_FOUND",
                message="Execution not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return execution

    async def request_user_cancel(
        self,
        execution_id: uuid.UUID,
        *,
        actor_user_id: uuid.UUID,
        reason: str | None,
    ) -> CancellationOutcome:
        """Authorize, lock, apply, and commit user cancellation."""
        await self._assert_actor_may_cancel(actor_user_id)
        execution = await self._require_owned_execution(execution_id, actor_user_id)
        prior_status = execution.status
        outcome = await apply_cancellation_locked(
            self._session,
            execution,
            now=datetime.now(UTC),
            requested_by=actor_user_id,
            reason=reason,
        )
        # First accepted cancellation only — duplicate/idempotent must not re-audit.
        if outcome.mode != CancellationMode.IDEMPOTENT:
            change_set: dict[str, object] = {}
            if outcome.cancel_requested_at is not None:
                change_set["cancel_requested_at"] = (
                    outcome.cancel_requested_at.isoformat()
                )
            await self._audit.append(
                actor_type=AuditActorType.USER,
                actor_id=str(actor_user_id),
                action=ACTION_EXECUTION_CANCEL,
                result=AuditResult.SUCCESS,
                resource_type="EXECUTION",
                resource_id=str(execution_id),
                execution_id=execution_id,
                before_data={"status": prior_status},
                after_data={"status": outcome.status},
                change_set=change_set or None,
                reason="USER_REQUEST",
            )
        await self._session.commit()
        return outcome

    async def request_internal_cancel_locked(
        self,
        execution_id: uuid.UUID,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> CancellationOutcome:
        """Composable internal cancellation for Schedule REPLACE (#56).

        Locks the Execution row and applies cancellation. Does NOT commit or
        rollback — the caller owns the surrounding transaction and any Schedule
        locks. Not exposed via public API.

        Internal Schedule REPLACE cancellation does NOT emit a USER
        execution.cancel AuditEvent in #57.
        """
        execution = await self._executions.lock_execution(execution_id)
        if execution is None:
            raise AppError(
                code="NOT_FOUND",
                message="Execution not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return await apply_cancellation_locked(
            self._session,
            execution,
            now=now or datetime.now(UTC),
            requested_by=None,
            reason=reason,
        )
