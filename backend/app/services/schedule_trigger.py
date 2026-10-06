"""Manual Schedule trigger API service (docs/06 §17 POST /trigger)."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import (
    OccurrenceStatus,
    ScheduleStatus,
    ScheduleTargetType,
    UserStatus,
)
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.schedule import ScheduleRepository
from app.repositories.schedule_occurrence import ScheduleOccurrenceRepository
from app.repositories.user import UserRepository
from app.scheduler import decision_reasons as reasons
from app.scheduler.runtime import ScheduleRuntimeService
from app.schemas.schedule import ScheduleOccurrenceResponse, occurrence_to_response
from app.services.authorization import AuthorizationResolver

_SCHEDULE_MANAGE = "schedule.manage"
_OPERATION_SCOPE = "SCHEDULE_MANUAL_TRIGGER_V1"
_RESOURCE_OCCURRENCE = "SCHEDULE_OCCURRENCE"
_IDEMPOTENCY_KEY_MAX_LEN = 128
_IDEMPOTENCY_PK_NAME = "pk_api_idempotency_records"


@dataclass(frozen=True, slots=True)
class ScheduleTriggerOutcome:
    occurrence: ScheduleOccurrenceResponse
    execution_id: uuid.UUID | None
    http_status: int
    replayed: bool


def _request_hash(*, schedule_id: uuid.UUID, scheduled_for: datetime) -> str:
    payload = {
        "schedule_id": str(schedule_id),
        "scheduled_for": scheduled_for.astimezone(UTC).isoformat(),
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

    async def trigger(
        self,
        schedule_id: uuid.UUID,
        *,
        actor_user_id: uuid.UUID,
        idempotency_key: str,
        scheduled_for: datetime | None = None,
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

        now = datetime.now(UTC)
        # Truncate to seconds so Idempotency-Key + occurrence unique key stay stable.
        fire_at = (scheduled_for or now).astimezone(UTC).replace(microsecond=0)
        req_hash = _request_hash(schedule_id=schedule.id, scheduled_for=fire_at)
        principal_key = str(actor_user_id)

        existing = await self._idempotency.get(
            principal_key=principal_key,
            operation_scope=_OPERATION_SCOPE,
            idempotency_key=key,
        )
        if existing is not None:
            return await self._replay(existing, req_hash)

        occurrence = await self._occurrences.create_planned(schedule.id, fire_at)
        if occurrence.status != OccurrenceStatus.PLANNED.value:
            # Prior automatic occurrence at the same second — fail closed for manual.
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
        if fire == "SKIPPED":
            if occurrence.decision_reason == reasons.FIRE_PRECONDITION_FAILED:
                raise AppError(
                    code="EXECUTION_PRECONDITION_FAILED",
                    message=(
                        "Manual trigger fire-time Workflow/Tool authorization failed."
                    ),
                    status_code=409,
                )
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Manual trigger skipped by overlap/misfire policy "
                    f"({occurrence.decision_reason})."
                ),
                status_code=409,
            )
        if fire in {"WAIT", "REPLACE_WAIT"}:
            # Manual trigger still returns the waiting occurrence (no Execution yet).
            body = occurrence_to_response(occurrence)
            await self._idempotency.create_completed(
                principal_key=principal_key,
                operation_scope=_OPERATION_SCOPE,
                idempotency_key=key,
                request_hash=req_hash,
                response_status=202,
                response_body={
                    "occurrence": body.model_dump(mode="json"),
                    "execution_id": None,
                },
                resource_type=_RESOURCE_OCCURRENCE,
                resource_id=occurrence.id,
                completed_at=now,
            )
            await self._session.commit()
            return ScheduleTriggerOutcome(
                occurrence=body,
                execution_id=None,
                http_status=202,
                replayed=False,
            )

        execution = await self._runtime.execution_for_occurrence(occurrence.id)
        execution_id = execution.id if execution is not None else None
        body = occurrence_to_response(occurrence)
        try:
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
