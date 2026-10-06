"""Manual Schedule trigger API service (docs/06 §17 POST /trigger)."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import (
    OccurrenceStatus,
    ScheduleStatus,
    ScheduleTargetType,
    UserStatus,
)
from app.models.execution import Execution
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.schedule import ScheduleRepository
from app.repositories.schedule_occurrence import ScheduleOccurrenceRepository
from app.repositories.user import UserRepository
from app.repositories.workflow_version import WorkflowVersionRepository
from app.scheduler import decision_reasons as reasons
from app.scheduler.runtime import ScheduleRuntimeService
from app.schemas.schedule import ScheduleOccurrenceResponse, occurrence_to_response
from app.services.authorization import AuthorizationResolver
from app.services.workflow_execution_creation import WorkflowExecutionCreationService

_SCHEDULE_MANAGE = "schedule.manage"
_OPERATION_SCOPE = "SCHEDULE_TRIGGER_V1"
_RESOURCE_OCCURRENCE = "SCHEDULE_OCCURRENCE"
_IDEMPOTENCY_KEY_MAX_LEN = 128
_IDEMPOTENCY_PK_NAME = "pk_api_idempotency_records"


@dataclass(frozen=True, slots=True)
class ScheduleTriggerOutcome:
    occurrence: ScheduleOccurrenceResponse
    execution_id: uuid.UUID | None
    http_status: int
    replayed: bool


def _request_hash(*, schedule_id: uuid.UUID) -> str:
    """Stable hash — must NOT include current time / scheduled_for."""
    payload = {
        "schedule_id": str(schedule_id),
        "source_type": "SCHEDULE_OCCURRENCE",
        "trigger_type": "USER",
    }
    raw = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _constraint_name(exc: IntegrityError) -> str | None:
    orig = getattr(exc, "orig", None)
    if orig is None:
        return None
    diag = getattr(orig, "diag", None)
    name = getattr(diag, "constraint_name", None) if diag is not None else None
    if name:
        return str(name)
    name = getattr(orig, "constraint_name", None)
    return str(name) if name else None


def _is_idempotency_pk_violation(exc: IntegrityError) -> bool:
    name = _constraint_name(exc)
    if name == _IDEMPOTENCY_PK_NAME:
        return True
    msg = str(getattr(exc, "orig", None) or exc)
    return _IDEMPOTENCY_PK_NAME in msg or (
        "api_idempotency_records" in msg.lower() and "idempotency_key" in msg.lower()
    )


class ScheduleTriggerService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._schedules = ScheduleRepository(session)
        self._occurrences = ScheduleOccurrenceRepository(session)
        self._users = UserRepository(session)
        self._authz = AuthorizationResolver(session)
        self._idempotency = IdempotencyRepository(session)
        self._runtime = ScheduleRuntimeService(session)
        self._versions = WorkflowVersionRepository(session)
        self._workflow_exec = WorkflowExecutionCreationService(session)

    async def trigger(
        self,
        schedule_id: uuid.UUID,
        *,
        actor_user_id: uuid.UUID,
        idempotency_key: str,
    ) -> ScheduleTriggerOutcome:
        key = idempotency_key.strip()
        if not key:
            raise AppError(
                code="VALIDATION_ERROR",
                message="Idempotency-Key must be a non-empty string.",
                status_code=400,
            )
        if len(key) > _IDEMPOTENCY_KEY_MAX_LEN:
            raise AppError(
                code="VALIDATION_ERROR",
                message=(
                    f"Idempotency-Key must be at most "
                    f"{_IDEMPOTENCY_KEY_MAX_LEN} characters."
                ),
                status_code=400,
            )

        await self._assert_actor(actor_user_id)
        schedule = await self._schedules.lock_for_update(schedule_id)
        if schedule is None or schedule.owner_id != actor_user_id:
            raise AppError(
                code="NOT_FOUND",
                message="Schedule not found.",
                status_code=404,
            )
        if schedule.status not in {
            ScheduleStatus.ACTIVE.value,
            ScheduleStatus.PAUSED.value,
        }:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Only ACTIVE or PAUSED Schedules can be triggered.",
                status_code=409,
            )
        if schedule.target_type == ScheduleTargetType.AGENT_VERSION.value:
            raise AppError(
                code="SCHEDULE_AGENT_VERSION_UNSUPPORTED",
                message=(
                    "AGENT_VERSION Schedule execution is not supported in this slice."
                ),
                status_code=409,
            )

        req_hash = _request_hash(schedule_id=schedule.id)
        principal_key = str(actor_user_id)

        # Idempotency lookup BEFORE generating the new occurrence timestamp.
        existing = await self._idempotency.get(
            principal_key=principal_key,
            operation_scope=_OPERATION_SCOPE,
            idempotency_key=key,
        )
        if existing is not None:
            return await self._replay(existing, req_hash)

        # Preflight before creating any occurrence / Execution / idempotency row.
        await self._preflight_workflow_creation(schedule)

        now = datetime.now(UTC)
        # Do not truncate to whole seconds — idempotency record owns replay identity.
        fire_at = now

        occurrence = await self._occurrences.create_planned(schedule.id, fire_at)
        if occurrence.status != OccurrenceStatus.PLANNED.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Occurrence already exists for scheduled_for.",
                status_code=409,
            )
        occurrence.decision_reason = reasons.MANUAL_TRIGGER

        fire = await self._runtime.try_fire_occurrence(
            schedule,
            occurrence,
            now=now,
            trigger_type="USER",
        )

        # Overlap SKIP/QUEUE/REPLACE_WAIT still return 201 with the occurrence.
        # Preflight failures during fire should not happen after the upfront
        # preflight, but if they do (race), automatic-style FAILED is wrong for
        # manual — map to 403/409 and roll back occurrence by raising before
        # idempotency persist. Runtime marks FAILED for automatic; for manual
        # we convert and abort the TX via exception after rolling back the
        # occurrence creation by raising before commit.
        if fire == "SKIPPED":
            if occurrence.status == OccurrenceStatus.FAILED.value:
                # Unexpected post-preflight failure — surface as 409 and abort.
                code = (
                    "FORBIDDEN"
                    if occurrence.decision_reason == reasons.AUTHORIZATION_REVOKED
                    else "EXECUTION_PRECONDITION_FAILED"
                )
                status = (
                    403
                    if occurrence.decision_reason == reasons.AUTHORIZATION_REVOKED
                    else 409
                )
                raise AppError(
                    code=code,
                    message=(
                        "Manual trigger fire-time Workflow/Tool authorization failed."
                    ),
                    status_code=status,
                )
            # OVERLAP_SKIP → 201 with SKIPPED occurrence, execution_id null.
            return await self._persist_outcome(
                principal_key=principal_key,
                key=key,
                req_hash=req_hash,
                occurrence=occurrence,
                execution_id=None,
                now=now,
            )

        if fire in {"WAIT", "REPLACE_WAIT"}:
            return await self._persist_outcome(
                principal_key=principal_key,
                key=key,
                req_hash=req_hash,
                occurrence=occurrence,
                execution_id=None,
                now=now,
            )

        execution = await self._runtime.execution_for_occurrence(occurrence.id)
        execution_id = execution.id if execution is not None else None
        return await self._persist_outcome(
            principal_key=principal_key,
            key=key,
            req_hash=req_hash,
            occurrence=occurrence,
            execution_id=execution_id,
            now=now,
        )

    async def _preflight_workflow_creation(self, schedule: Any) -> None:
        if schedule.target_type != ScheduleTargetType.WORKFLOW_VERSION.value:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="Schedule target_type must be WORKFLOW_VERSION.",
                status_code=409,
            )
        if schedule.workflow_version_id is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="Schedule.workflow_version_id is required.",
                status_code=409,
            )
        version = await self._versions.get(schedule.workflow_version_id)
        if version is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="Pinned WorkflowVersion not found.",
                status_code=409,
            )
        # Reuse the same strict creation preflight as materialization.
        # Failures raise AppError 403/409 — no occurrence / Execution / idempotency.
        await self._workflow_exec.prepare_schedule_creation_preflight(
            workflow_id=version.workflow_id,
            version_id=version.id,
            requester_id=schedule.owner_id,
            request_inputs=dict(schedule.input_template or {}),
        )

    async def _persist_outcome(
        self,
        *,
        principal_key: str,
        key: str,
        req_hash: str,
        occurrence: Any,
        execution_id: uuid.UUID | None,
        now: datetime,
    ) -> ScheduleTriggerOutcome:
        body = occurrence_to_response(occurrence, execution_id=execution_id)
        try:
            from app.audit.writer import ACTION_SCHEDULE_TRIGGER, AuditWriter
            from app.domain.enums import AuditActorType, AuditResult

            await AuditWriter(self._session).append(
                actor_type=AuditActorType.USER,
                actor_id=principal_key,
                action=ACTION_SCHEDULE_TRIGGER,
                result=AuditResult.SUCCESS,
                resource_type="SCHEDULE",
                resource_id=str(occurrence.schedule_id),
                execution_id=execution_id,
                change_set={
                    "occurrence_id": str(occurrence.id),
                    "occurrence_status": occurrence.status,
                    "decision_reason": occurrence.decision_reason,
                    "execution_created": execution_id is not None,
                },
                occurred_at=now,
            )
            await self._idempotency.create_completed(
                principal_key=principal_key,
                operation_scope=_OPERATION_SCOPE,
                idempotency_key=key,
                request_hash=req_hash,
                response_status=201,
                response_body={
                    "occurrence": body.model_dump(mode="json"),
                    "execution_id": str(execution_id) if execution_id else None,
                },
                resource_type=_RESOURCE_OCCURRENCE,
                resource_id=occurrence.id,
                completed_at=now,
            )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if not _is_idempotency_pk_violation(exc):
                raise
            raced = await self._idempotency.get(
                principal_key=principal_key,
                operation_scope=_OPERATION_SCOPE,
                idempotency_key=key,
            )
            if raced is None:
                raise
            return await self._replay(raced, req_hash)

        return ScheduleTriggerOutcome(
            occurrence=body,
            execution_id=execution_id,
            http_status=201,
            replayed=False,
        )

    async def _assert_actor(self, actor_user_id: uuid.UUID) -> None:
        user = await self._users.get(actor_user_id)
        if user is None or user.status != UserStatus.ACTIVE.value:
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Actor is not an ACTIVE user.",
                status_code=403,
            )
        if not await self._authz.has_permission(actor_user_id, _SCHEDULE_MANAGE):
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Missing schedule.manage permission.",
                status_code=403,
            )

    async def _replay(
        self, record: Any, request_hash: str
    ) -> ScheduleTriggerOutcome:
        if record.request_hash != request_hash:
            raise AppError(
                code="IDEMPOTENCY_KEY_REUSED",
                message="Idempotency-Key가 다른 요청에 재사용되었습니다.",
                status_code=409,
            )
        if record.status != "COMPLETED" or record.response_body is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency record가 COMPLETED가 아닙니다.",
                status_code=409,
            )
        body = record.response_body
        try:
            occ = ScheduleOccurrenceResponse.model_validate(body["occurrence"])
            raw_exec = body.get("execution_id")
            execution_id = uuid.UUID(str(raw_exec)) if raw_exec else None
        except (ValidationError, TypeError, ValueError, KeyError) as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency response snapshot이 유효하지 않습니다.",
                status_code=409,
            ) from exc
        return ScheduleTriggerOutcome(
            occurrence=occ,
            execution_id=execution_id,
            http_status=int(record.response_status),
            replayed=True,
        )


async def bulk_execution_ids_for_occurrences(
    session: AsyncSession,
    occurrence_ids: list[uuid.UUID],
) -> dict[uuid.UUID, uuid.UUID]:
    """One query: occurrence_id → execution_id for list projections."""
    if not occurrence_ids:
        return {}
    stmt = select(Execution.schedule_occurrence_id, Execution.id).where(
        Execution.schedule_occurrence_id.in_(occurrence_ids)
    )
    rows = (await session.execute(stmt)).all()
    out: dict[uuid.UUID, uuid.UUID] = {}
    for occ_id, exec_id in rows:
        if occ_id is not None:
            out[occ_id] = exec_id
    return out
