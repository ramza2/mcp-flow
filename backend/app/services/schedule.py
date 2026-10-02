"""Schedule registry service (docs/02 FNC-SCH, docs/06 §17)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import (
    AgentStatus,
    AgentVersionStatus,
    AgentVersionValidationStatus,
    ResourceGrantResourceType,
    ScheduleMisfirePolicy,
    ScheduleOverlapPolicy,
    ScheduleStatus,
    ScheduleTargetType,
    ScheduleType,
    UserStatus,
    WorkflowStatus,
    WorkflowVersionStatus,
    WorkflowVersionValidationStatus,
)
from app.execution.plan_inputs import normalize_plan_inputs
from app.models.schedule import Schedule, ScheduleOccurrence
from app.repositories.agent import AgentRepository
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.authorization import AuthorizationRepository
from app.repositories.schedule import ScheduleRepository
from app.repositories.schedule_occurrence import ScheduleOccurrenceRepository
from app.repositories.user import UserRepository
from app.repositories.workflow import WorkflowRepository
from app.repositories.workflow_version import WorkflowVersionRepository
from app.scheduler.recurrence import next_scheduled_at, validate_schedule_expression
from app.schemas.execution_plan import EXECUTION_PLAN_SCHEMA_VERSION, ExecutionPlanV1
from app.schemas.schedule import ScheduleCreate, ScheduleUpdate
from app.services.authorization import AuthorizationResolver

_SCHEDULE_MANAGE = "schedule.manage"
_WORKFLOW_EXECUTE = "workflow.execute"
_CONFIG_FIELDS = frozenset(
    {
        "target_type",
        "target_id",
        "schedule_type",
        "schedule_expression",
        "timezone",
        "inputs",
        "overlap_policy",
        "misfire_policy",
        "max_catch_up",
        "start_at",
        "end_at",
    }
)


class ScheduleService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._schedules = ScheduleRepository(session)
        self._occurrences = ScheduleOccurrenceRepository(session)
        self._users = UserRepository(session)
        self._authz = AuthorizationResolver(session)
        self._auth = AuthorizationRepository(session)
        self._agents = AgentRepository(session)
        self._agent_versions = AgentVersionRepository(session)
        self._workflows = WorkflowRepository(session)
        self._workflow_versions = WorkflowVersionRepository(session)

    async def _assert_manage(self, actor_user_id: uuid.UUID) -> None:
        user = await self._users.get(actor_user_id)
        if user is None or user.status != UserStatus.ACTIVE.value:
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Actor is not an ACTIVE user.",
                status_code=status.HTTP_403_FORBIDDEN,
            )
        if not await self._authz.has_permission(actor_user_id, _SCHEDULE_MANAGE):
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Missing schedule.manage permission.",
                status_code=status.HTTP_403_FORBIDDEN,
            )

    async def _require_owned(
        self, schedule_id: uuid.UUID, owner_id: uuid.UUID
    ) -> Schedule:
        await self._assert_manage(owner_id)
        schedule = await self._schedules.get_for_owner(schedule_id, owner_id)
        if schedule is None:
            raise AppError(
                code="NOT_FOUND",
                message="Schedule not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return schedule

    def _raise_version_conflict(self) -> None:
        raise AppError(
            code="RESOURCE_VERSION_CONFLICT",
            message="Schedule lock_version does not match.",
            status_code=status.HTTP_409_CONFLICT,
        )

    def _validate_max_catch_up(self, value: int, misfire: str) -> None:
        if value < 1 or value > 100:
            raise AppError(
                code="VALIDATION_ERROR",
                message="max_catch_up must be between 1 and 100.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        if misfire == ScheduleMisfirePolicy.CATCH_UP_LIMITED.value and (
            value < 1 or value > 100
        ):
            raise AppError(
                code="VALIDATION_ERROR",
                message="max_catch_up must be between 1 and 100 for CATCH_UP_LIMITED.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

    def _assert_window(self, start_at: datetime | None, end_at: datetime | None) -> None:
        if start_at is not None and end_at is not None and end_at <= start_at:
            raise AppError(
                code="VALIDATION_ERROR",
                message="end_at must be after start_at.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

    async def _validate_agent_inputs(self, inputs: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(inputs, dict):
            raise AppError(
                code="VALIDATION_ERROR",
                message="AGENT_VERSION Schedule inputs must be a JSON object.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        return dict(inputs)

    async def _validate_workflow_inputs(
        self,
        *,
        workflow_version_id: uuid.UUID,
        requester_id: uuid.UUID,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        version = await self._workflow_versions.get(workflow_version_id)
        if version is None:
            raise AppError(
                code="NOT_FOUND",
                message="Workflow version not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        workflow = await self._workflows.get(version.workflow_id)
        if workflow is None or workflow.deleted_at is not None:
            raise AppError(
                code="NOT_FOUND",
                message="Workflow not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        auth_snap = await self._auth.get_resource_authorization_snapshot(
            requester_id,
            permission_code=_WORKFLOW_EXECUTE,
            resource_type=ResourceGrantResourceType.WORKFLOW.value,
            resource_id=workflow.id,
        )
        if not (
            auth_snap.user_exists
            and auth_snap.user_active
            and auth_snap.permission_present
            and auth_snap.resource_grant_present
        ):
            raise AppError(
                code="FORBIDDEN",
                message="workflow.execute + WORKFLOW ResourceGrant required.",
                status_code=status.HTTP_403_FORBIDDEN,
            )
        if workflow.status != WorkflowStatus.ACTIVE.value:
            raise AppError(
                code="VALIDATION_ERROR",
                message="Workflow must be ACTIVE.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        if version.status != WorkflowVersionStatus.PUBLISHED.value:
            raise AppError(
                code="VALIDATION_ERROR",
                message="WorkflowVersion must be PUBLISHED.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        if version.validation_status != WorkflowVersionValidationStatus.VALID.value:
            raise AppError(
                code="VALIDATION_ERROR",
                message="WorkflowVersion.validation_status must be VALID.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        if version.plan_schema_version != EXECUTION_PLAN_SCHEMA_VERSION:
            raise AppError(
                code="VALIDATION_ERROR",
                message="plan_schema_version must be 1.0.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        try:
            plan = ExecutionPlanV1.model_validate(version.plan_definition)
        except Exception as exc:
            raise AppError(
                code="VALIDATION_ERROR",
                message="ExecutionPlanV1 validation failed.",
                status_code=status.HTTP_400_BAD_REQUEST,
            ) from exc
        return normalize_plan_inputs(plan, dict(inputs))

    async def _validate_target(
        self,
        *,
        target_type: ScheduleTargetType | str,
        target_id: uuid.UUID,
        requester_id: uuid.UUID,
        inputs: dict[str, Any],
    ) -> tuple[uuid.UUID | None, uuid.UUID | None, dict[str, Any]]:
        tt = (
            target_type
            if isinstance(target_type, ScheduleTargetType)
            else ScheduleTargetType(str(target_type))
        )
        if tt == ScheduleTargetType.AGENT_VERSION:
            version = await self._agent_versions.get(target_id)
            if version is None:
                raise AppError(
                    code="NOT_FOUND",
                    message="Agent version not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            if version.status != AgentVersionStatus.PUBLISHED.value:
                raise AppError(
                    code="VALIDATION_ERROR",
                    message="AgentVersion must be PUBLISHED.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            if version.validation_status != AgentVersionValidationStatus.VALID.value:
                raise AppError(
                    code="VALIDATION_ERROR",
                    message="AgentVersion.validation_status must be VALID.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            agent = await self._agents.get(version.agent_id)
            if agent is None or agent.deleted_at is not None:
                raise AppError(
                    code="NOT_FOUND",
                    message="Agent not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            if agent.status != AgentStatus.ACTIVE.value:
                raise AppError(
                    code="VALIDATION_ERROR",
                    message="Agent must be ACTIVE.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            normalized = await self._validate_agent_inputs(inputs)
            return target_id, None, normalized

        normalized = await self._validate_workflow_inputs(
            workflow_version_id=target_id,
            requester_id=requester_id,
            inputs=inputs,
        )
        return None, target_id, normalized

    def _compute_next_run(
        self,
        *,
        schedule_type: str,
        schedule_expression: str,
        timezone: str,
        after: datetime,
        start_at: datetime | None,
        end_at: datetime | None,
    ) -> datetime | None:
        return next_scheduled_at(
            schedule_type=schedule_type,
            expression=schedule_expression,
            timezone=timezone,
            after=after,
            start_at=start_at,
            end_at=end_at,
        )

    async def create(self, data: ScheduleCreate, *, owner_id: uuid.UUID) -> Schedule:
        await self._assert_manage(owner_id)

        overlap = (
            str(data.overlap_policy)
            if data.overlap_policy is not None
            else ScheduleOverlapPolicy.SKIP.value
        )
        misfire = (
            str(data.misfire_policy)
            if data.misfire_policy is not None
            else ScheduleMisfirePolicy.SKIP.value
        )
        max_catch_up = data.max_catch_up if data.max_catch_up is not None else 1
        self._validate_max_catch_up(max_catch_up, misfire)
        self._assert_window(data.start_at, data.end_at)

        validate_schedule_expression(
            str(data.schedule_type),
            data.schedule_expression,
            data.timezone,
        )

        agent_version_id, workflow_version_id, input_template = await self._validate_target(
            target_type=data.target_type,
            target_id=data.target_id,
            requester_id=owner_id,
            inputs=data.inputs,
        )

        schedule = await self._schedules.create(
            name=data.name,
            description=data.description,
            owner_id=owner_id,
            target_type=str(data.target_type),
            agent_version_id=agent_version_id,
            workflow_version_id=workflow_version_id,
            schedule_type=str(data.schedule_type),
            schedule_expression=data.schedule_expression.strip(),
            timezone=data.timezone,
            input_template=input_template,
            misfire_policy=misfire,
            overlap_policy=overlap,
            max_catch_up=max_catch_up,
            status=ScheduleStatus.PAUSED.value,
            next_run_at=None,
            last_run_at=None,
            start_at=data.start_at,
            end_at=data.end_at,
            created_by=owner_id,
        )
        await self._session.commit()
        await self._session.refresh(schedule)
        return schedule

    async def list(
        self,
        *,
        owner_id: uuid.UUID,
        page: int = 1,
        page_size: int = 20,
        q: str | None = None,
        status_filter: str | None = None,
        target_type: str | None = None,
        sort: str = "-updated_at",
    ) -> tuple[list[Schedule], int]:
        await self._assert_manage(owner_id)
        return await self._schedules.list_for_owner(
            owner_id,
            page=page,
            page_size=page_size,
            q=q,
            status=status_filter,
            target_type=target_type,
            sort=sort,
        )

    async def get(self, schedule_id: uuid.UUID, *, owner_id: uuid.UUID) -> Schedule:
        return await self._require_owned(schedule_id, owner_id)

    async def update(
        self,
        schedule_id: uuid.UUID,
        data: ScheduleUpdate,
        *,
        owner_id: uuid.UUID,
        expected_lock_version: int,
    ) -> Schedule:
        schedule = await self._require_owned(schedule_id, owner_id)
        payload = data.model_dump(exclude_unset=True, exclude={"lock_version"})
        if not payload:
            return schedule

        current_status = ScheduleStatus(schedule.status)
        config_keys = _CONFIG_FIELDS.intersection(payload.keys())

        if current_status == ScheduleStatus.ACTIVE:
            if config_keys:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="ACTIVE Schedule allows only name and description updates.",
                    status_code=status.HTTP_409_CONFLICT,
                )
        elif current_status == ScheduleStatus.PAUSED:
            pass
        elif config_keys:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"{schedule.status} Schedule allows only name and description updates.",
                status_code=status.HTTP_409_CONFLICT,
            )

        fields: dict[str, Any] = {}
        if "name" in payload:
            fields["name"] = payload["name"]
        if "description" in payload:
            fields["description"] = payload["description"]

        if current_status == ScheduleStatus.PAUSED and config_keys:
            fields["next_run_at"] = None

            target_type = payload.get("target_type", schedule.target_type)
            target_id = payload.get("target_id")
            if target_id is None:
                target_id = schedule.agent_version_id or schedule.workflow_version_id
            if target_id is None:
                raise AppError(
                    code="VALIDATION_ERROR",
                    message="Schedule target_id is missing.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            schedule_type = str(payload.get("schedule_type", schedule.schedule_type))
            schedule_expression = payload.get(
                "schedule_expression", schedule.schedule_expression
            )
            timezone = payload.get("timezone", schedule.timezone)
            start_at = payload.get("start_at", schedule.start_at)
            end_at = payload.get("end_at", schedule.end_at)
            self._assert_window(start_at, end_at)

            validate_schedule_expression(schedule_type, schedule_expression, timezone)

            inputs = payload.get("inputs", schedule.input_template or {})
            agent_version_id, workflow_version_id, input_template = (
                await self._validate_target(
                    target_type=target_type,
                    target_id=target_id,
                    requester_id=owner_id,
                    inputs=dict(inputs),
                )
            )

            misfire = str(
                payload.get("misfire_policy", schedule.misfire_policy)
            )
            max_catch_up = payload.get("max_catch_up", schedule.max_catch_up)
            self._validate_max_catch_up(int(max_catch_up), misfire)

            fields.update(
                {
                    "target_type": str(target_type),
                    "agent_version_id": agent_version_id,
                    "workflow_version_id": workflow_version_id,
                    "schedule_type": schedule_type,
                    "schedule_expression": str(schedule_expression).strip(),
                    "timezone": timezone,
                    "input_template": input_template,
                    "overlap_policy": str(
                        payload.get("overlap_policy", schedule.overlap_policy)
                    ),
                    "misfire_policy": misfire,
                    "max_catch_up": int(max_catch_up),
                    "start_at": start_at,
                    "end_at": end_at,
                }
            )

        updated = await self._schedules.update_atomic(
            schedule_id,
            expected_lock_version=expected_lock_version,
            updated_by=owner_id,
            **fields,
        )
        if updated is None:
            if await self._schedules.get_for_owner(schedule_id, owner_id) is None:
                raise AppError(
                    code="NOT_FOUND",
                    message="Schedule not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            self._raise_version_conflict()

        await self._session.commit()
        await self._session.refresh(updated)
        return updated

    async def activate(
        self, schedule_id: uuid.UUID, *, owner_id: uuid.UUID
    ) -> Schedule:
        schedule = await self._require_owned(schedule_id, owner_id)
        if schedule.status == ScheduleStatus.ACTIVE.value:
            return schedule
        if schedule.status != ScheduleStatus.PAUSED.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Only PAUSED Schedule can be activated.",
                status_code=status.HTTP_409_CONFLICT,
            )

        now = datetime.now(UTC)
        start_at = schedule.start_at
        if schedule.schedule_type == ScheduleType.INTERVAL.value and start_at is None:
            start_at = now

        target_id = schedule.agent_version_id or schedule.workflow_version_id
        if target_id is None:
            raise AppError(
                code="VALIDATION_ERROR",
                message="Schedule target is missing.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        await self._validate_target(
            target_type=schedule.target_type,
            target_id=target_id,
            requester_id=owner_id,
            inputs=dict(schedule.input_template or {}),
        )
        validate_schedule_expression(
            schedule.schedule_type,
            schedule.schedule_expression,
            schedule.timezone,
        )
        self._assert_window(start_at, schedule.end_at)

        next_run = self._compute_next_run(
            schedule_type=schedule.schedule_type,
            schedule_expression=schedule.schedule_expression,
            timezone=schedule.timezone,
            after=now,
            start_at=start_at,
            end_at=schedule.end_at,
        )
        if schedule.schedule_type == ScheduleType.ONCE.value and next_run is None:
            raise AppError(
                code="SCHEDULE_TIME_IN_PAST",
                message="ONCE schedule time is in the past.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if (
            schedule.schedule_type != ScheduleType.ONCE.value
            and next_run is None
        ):
            raise AppError(
                code="VALIDATION_ERROR",
                message="No future occurrence within schedule window.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        updated = await self._schedules.update_atomic(
            schedule_id,
            expected_lock_version=schedule.lock_version,
            updated_by=owner_id,
            status=ScheduleStatus.ACTIVE.value,
            start_at=start_at,
            next_run_at=next_run,
        )
        if updated is None:
            self._raise_version_conflict()
        await self._session.commit()
        await self._session.refresh(updated)
        return updated

    async def pause(self, schedule_id: uuid.UUID, *, owner_id: uuid.UUID) -> Schedule:
        schedule = await self._require_owned(schedule_id, owner_id)
        if schedule.status == ScheduleStatus.PAUSED.value:
            return schedule
        if schedule.status != ScheduleStatus.ACTIVE.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Only ACTIVE Schedule can be paused.",
                status_code=status.HTTP_409_CONFLICT,
            )
        # Preserve next_run_at for future misfire decisions.
        updated = await self._schedules.update_atomic(
            schedule_id,
            expected_lock_version=schedule.lock_version,
            updated_by=owner_id,
            status=ScheduleStatus.PAUSED.value,
        )
        if updated is None:
            self._raise_version_conflict()
        await self._session.commit()
        await self._session.refresh(updated)
        return updated

    async def resume(self, schedule_id: uuid.UUID, *, owner_id: uuid.UUID) -> Schedule:
        schedule = await self._require_owned(schedule_id, owner_id)
        if schedule.status == ScheduleStatus.ACTIVE.value:
            return schedule
        if schedule.status != ScheduleStatus.PAUSED.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Only PAUSED Schedule can be resumed.",
                status_code=status.HTTP_409_CONFLICT,
            )

        now = datetime.now(UTC)
        start_at = schedule.start_at
        if schedule.schedule_type == ScheduleType.INTERVAL.value and start_at is None:
            start_at = now

        target_id = schedule.agent_version_id or schedule.workflow_version_id
        if target_id is None:
            raise AppError(
                code="VALIDATION_ERROR",
                message="Schedule target is missing.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        await self._validate_target(
            target_type=schedule.target_type,
            target_id=target_id,
            requester_id=owner_id,
            inputs=dict(schedule.input_template or {}),
        )
        validate_schedule_expression(
            schedule.schedule_type,
            schedule.schedule_expression,
            schedule.timezone,
        )
        self._assert_window(start_at, schedule.end_at)

        # Preserve overdue next_run_at; only recompute when null (config edit).
        next_run = schedule.next_run_at
        if next_run is None:
            next_run = self._compute_next_run(
                schedule_type=schedule.schedule_type,
                schedule_expression=schedule.schedule_expression,
                timezone=schedule.timezone,
                after=now,
                start_at=start_at,
                end_at=schedule.end_at,
            )
            if (
                schedule.schedule_type == ScheduleType.ONCE.value
                and next_run is None
            ):
                raise AppError(
                    code="SCHEDULE_TIME_IN_PAST",
                    message="ONCE schedule time is in the past.",
                    status_code=status.HTTP_409_CONFLICT,
                )

        updated = await self._schedules.update_atomic(
            schedule_id,
            expected_lock_version=schedule.lock_version,
            updated_by=owner_id,
            status=ScheduleStatus.ACTIVE.value,
            start_at=start_at,
            next_run_at=next_run,
        )
        if updated is None:
            self._raise_version_conflict()
        await self._session.commit()
        await self._session.refresh(updated)
        return updated

    async def list_occurrences(
        self,
        schedule_id: uuid.UUID,
        *,
        owner_id: uuid.UUID,
        status_filter: str | None = None,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[ScheduleOccurrence], int]:
        await self._require_owned(schedule_id, owner_id)
        return await self._occurrences.list_for_schedule(
            schedule_id,
            status=status_filter,
            from_time=from_time,
            to_time=to_time,
            page=page,
            page_size=page_size,
        )
