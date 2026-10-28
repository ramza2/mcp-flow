"""AGENT_VERSION Schedule ONCE → AgentRequest pipeline → SCHEDULE_OCCURRENCE Execution.

Scheduler Schedule FOR UPDATE must never call LLM/MCP. This service is invoked
from the outbox process after a durable ``AGENT_SCHEDULE_PLAN`` row is created
under the Schedule lock. Reprocessing the same occurrence is idempotent:
at most one Execution per ``schedule_occurrence_id``; AgentRequest id is
persisted on the Outbox payload after first create.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.parameter_builder import ParameterBuilderService
from app.agent.plan_generator import PlanGeneratorService
from app.agent.plan_validator import PlanValidatorService
from app.agent.request_analyzer import RequestAnalyzerService
from app.agent.safe_auto_execute import is_safe_auto_execute
from app.agent.tool_selector import ToolSelectorService
from app.core.errors import AppError
from app.domain.enums import (
    AgentRequestStatus,
    AgentStatus,
    AgentVersionStatus,
    ConversationMessageRole,
    ExecutionSourceType,
    OccurrenceStatus,
    ScheduleTargetType,
    ScheduleType,
)
from app.execution.materialize import (
    ExecutionMaterializeParams,
    ExecutionPlanMaterializer,
)
from app.models.execution import Execution
from app.models.outbox import OutboxEvent
from app.models.schedule import Schedule, ScheduleOccurrence
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.outbox import OutboxRepository
from app.repositories.plan_generation import PlanGenerationRepository
from app.repositories.plan_validation import PlanValidationRepository
from app.repositories.schedule import ScheduleRepository
from app.repositories.schedule_occurrence import ScheduleOccurrenceRepository
from app.scheduler import decision_reasons as reasons
from app.services.agent_request import AgentRequestService
from app.services.conversation import ConversationService
from app.services.execution_creation import ExecutionCreationService

logger = logging.getLogger(__name__)

_EVENT_TYPE = "AGENT_SCHEDULE_PLAN"
_AGGREGATE_TYPE = "SCHEDULE_OCCURRENCE"
_TRIGGER_SCHEDULE = "SCHEDULE"
_REQUEST_TEXT_MAX = 4000
_UNSAFE_TERMINAL = frozenset(
    {
        AgentRequestStatus.WAITING_INPUT.value,
        AgentRequestStatus.WAITING_CONFIRMATION.value,
        AgentRequestStatus.REJECTED.value,
        AgentRequestStatus.FAILED.value,
        AgentRequestStatus.CANCELLED.value,
    }
)


@dataclass(frozen=True, slots=True)
class AgentSchedulePlanBatchResult:
    selected: int
    executed: int
    failed: int
    skipped: int


def extract_agent_schedule_request_text(inputs: Any) -> str:
    """Require non-empty ``inputs.request_text`` (≤4000)."""
    if not isinstance(inputs, dict):
        raise AppError(
            code="VALIDATION_ERROR",
            message="AGENT_VERSION Schedule inputs must be a JSON object.",
            status_code=400,
        )
    text = inputs.get("request_text")
    if not isinstance(text, str) or not text.strip():
        raise AppError(
            code="VALIDATION_ERROR",
            message="AGENT_VERSION Schedule inputs.request_text is required.",
            status_code=400,
        )
    cleaned = text.strip()
    if len(cleaned) > _REQUEST_TEXT_MAX:
        raise AppError(
            code="VALIDATION_ERROR",
            message=(
                f"AGENT_VERSION Schedule inputs.request_text must be at most "
                f"{_REQUEST_TEXT_MAX} characters."
            ),
            status_code=400,
        )
    return cleaned


class AgentScheduleExecutionService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._schedules = ScheduleRepository(session)
        self._occurrences = ScheduleOccurrenceRepository(session)
        self._executions = ExecutionRepository(session)
        self._outbox = OutboxRepository(session)
        self._agents_versions = AgentVersionRepository(session)
        self._requests = AgentRequestRepository(session)
        self._plans = PlanGenerationRepository(session)
        self._validations = PlanValidationRepository(session)
        self._conversations = ConversationService(session)
        self._agent_requests = AgentRequestService(session)
        self._execution_creation = ExecutionCreationService(session)

    async def enqueue_plan_outbox(
        self,
        *,
        schedule: Schedule,
        occurrence: ScheduleOccurrence,
        now: datetime,
    ) -> OutboxEvent:
        """Durable handoff under Schedule lock — no LLM/MCP."""
        if schedule.target_type != ScheduleTargetType.AGENT_VERSION.value:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="enqueue_plan_outbox requires AGENT_VERSION target.",
                status_code=409,
            )
        if schedule.schedule_type != ScheduleType.ONCE.value:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="AGENT_VERSION Schedule execution supports ONCE only.",
                status_code=409,
            )
        if schedule.agent_version_id is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="Schedule.agent_version_id is required.",
                status_code=409,
            )
        request_text = extract_agent_schedule_request_text(schedule.input_template)
        dedupe_key = f"agent_schedule_plan:{occurrence.id}"
        existing = (
            await self._session.execute(
                select(OutboxEvent).where(OutboxEvent.dedupe_key == dedupe_key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        row = OutboxEvent(
            id=uuid.uuid4(),
            event_type=_EVENT_TYPE,
            aggregate_type=_AGGREGATE_TYPE,
            aggregate_id=occurrence.id,
            dedupe_key=dedupe_key,
            payload={
                "schedule_id": str(schedule.id),
                "schedule_occurrence_id": str(occurrence.id),
                "agent_version_id": str(schedule.agent_version_id),
                "requester_id": str(schedule.owner_id),
                "request_text": request_text,
                "trigger_type": _TRIGGER_SCHEDULE,
            },
            created_at=now,
            last_attempt_at=None,
            published_at=None,
            publish_attempt_count=0,
            last_error_code=None,
            lock_version=1,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError:
            await self._session.flush()
            existing = (
                await self._session.execute(
                    select(OutboxEvent).where(OutboxEvent.dedupe_key == dedupe_key)
                )
            ).scalar_one_or_none()
            if existing is None:
                raise
            return existing
        occurrence.decision_reason = reasons.AGENT_PLAN_QUEUED
        return row

    async def process_unpublished_batch(
        self,
        *,
        limit: int = 20,
        now: datetime | None = None,
    ) -> AgentSchedulePlanBatchResult:
        """Consume AGENT_SCHEDULE_PLAN outbox rows (LLM OK; no Schedule FOR UPDATE)."""
        ts = now or datetime.now(UTC)
        bounded = max(1, min(int(limit), 200))
        stmt = (
            select(OutboxEvent)
            .where(
                OutboxEvent.event_type == _EVENT_TYPE,
                OutboxEvent.published_at.is_(None),
            )
            .order_by(OutboxEvent.created_at.asc(), OutboxEvent.id.asc())
            .limit(bounded)
            .with_for_update(skip_locked=True)
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        executed = 0
        failed = 0
        skipped = 0
        for row in rows:
            outcome = await self._process_one(row, now=ts)
            if outcome == "EXECUTED":
                executed += 1
            elif outcome == "FAILED":
                failed += 1
            else:
                skipped += 1
        return AgentSchedulePlanBatchResult(
            selected=len(rows),
            executed=executed,
            failed=failed,
            skipped=skipped,
        )

    async def _process_one(self, row: OutboxEvent, *, now: datetime) -> str:
        payload = row.payload if isinstance(row.payload, dict) else {}
        try:
            occurrence_id = uuid.UUID(str(payload["schedule_occurrence_id"]))
            schedule_id = uuid.UUID(str(payload["schedule_id"]))
            agent_version_id = uuid.UUID(str(payload["agent_version_id"]))
            requester_id = uuid.UUID(str(payload["requester_id"]))
            request_text = str(payload["request_text"])
            trigger_type = str(payload.get("trigger_type") or _TRIGGER_SCHEDULE)
        except (KeyError, ValueError, TypeError) as exc:
            row.publish_attempt_count += 1
            row.last_attempt_at = now
            row.last_error_code = "PAYLOAD_INVALID"
            row.lock_version += 1
            await self._session.flush()
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="AGENT_SCHEDULE_PLAN payload is invalid.",
                status_code=409,
            ) from exc

        existing_exec = await self._execution_for_occurrence(occurrence_id)
        if existing_exec is not None:
            await self._mark_published(row, now=now)
            return "SKIPPED"

        occurrence = (
            await self._session.execute(
                select(ScheduleOccurrence)
                .where(ScheduleOccurrence.id == occurrence_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if occurrence is None:
            await self._mark_published(row, now=now)
            return "SKIPPED"
        if occurrence.status not in {
            OccurrenceStatus.PLANNED.value,
        }:
            await self._mark_published(row, now=now)
            return "SKIPPED"

        # Read Schedule without FOR UPDATE — planning must not invert
        # Schedule → Execution lock order used by REPLACE.
        schedule = await self._schedules.get(schedule_id)
        if (
            schedule is None
            or schedule.target_type != ScheduleTargetType.AGENT_VERSION.value
            or schedule.agent_version_id != agent_version_id
            or schedule.owner_id != requester_id
        ):
            await self._fail_occurrence(
                occurrence,
                reason=reasons.TARGET_PRECONDITION_FAILED,
                now=now,
            )
            await self._mark_published(row, now=now)
            return "FAILED"

        try:
            execution = await self._plan_and_materialize(
                schedule=schedule,
                occurrence=occurrence,
                agent_version_id=agent_version_id,
                requester_id=requester_id,
                request_text=request_text,
                trigger_type=trigger_type,
                outbox_row=row,
                now=now,
            )
        except AppError as exc:
            logger.info(
                "agent schedule plan failed occurrence_id=%s code=%s",
                occurrence_id,
                exc.code,
            )
            reason = (
                reasons.AUTHORIZATION_REVOKED
                if exc.code in {"FORBIDDEN", "AUTH_FORBIDDEN"}
                else reasons.TARGET_PRECONDITION_FAILED
            )
            await self._fail_occurrence(occurrence, reason=reason, now=now)
            await self._mark_published(row, now=now)
            return "FAILED"
        except Exception:
            # Leave outbox unpublished for durable retry (transient LLM/infra).
            row.publish_attempt_count += 1
            row.last_attempt_at = now
            row.last_error_code = "PLAN_TRANSIENT_FAILURE"
            row.lock_version += 1
            await self._session.flush()
            raise

        assert execution is not None
        await self._mark_published(row, now=now)
        return "EXECUTED"

    async def _plan_and_materialize(
        self,
        *,
        schedule: Schedule,
        occurrence: ScheduleOccurrence,
        agent_version_id: uuid.UUID,
        requester_id: uuid.UUID,
        request_text: str,
        trigger_type: str,
        outbox_row: OutboxEvent,
        now: datetime,
    ) -> Execution:
        version = await self._agents_versions.get(agent_version_id)
        if version is None or version.status != AgentVersionStatus.PUBLISHED.value:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="Pinned AgentVersion must be PUBLISHED.",
                status_code=409,
            )
        from app.repositories.agent import AgentRepository

        agent = await AgentRepository(self._session).get(version.agent_id)
        if agent is None or agent.status != AgentStatus.ACTIVE.value:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="Agent must be ACTIVE.",
                status_code=409,
            )
        if schedule.agent_version_id != version.id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Schedule.agent_version_id pin mismatch.",
                status_code=409,
            )

        agent_request_id = self._payload_uuid(outbox_row.payload, "agent_request_id")
        if agent_request_id is None:
            conversation = await self._conversations.create_conversation(
                owner_id=requester_id,
                agent_id=agent.id,
                title=f"Schedule {schedule.id}",
            )
            message = await self._conversations.append_message(
                conversation_id=conversation.id,
                owner_id=requester_id,
                role=ConversationMessageRole.USER.value,
                content={"text": request_text},
                content_text=request_text,
            )
            request = await self._agent_requests.create_received(
                conversation_id=conversation.id,
                requester_id=requester_id,
                agent_version_id=version.id,
                source_message_id=message.id,
            )
            agent_request_id = request.id
            payload = dict(outbox_row.payload or {})
            payload["agent_request_id"] = str(agent_request_id)
            outbox_row.payload = payload
            outbox_row.lock_version += 1
            await self._session.flush()
        else:
            request = await self._requests.get(agent_request_id)
            if request is None:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="Persisted schedule AgentRequest missing.",
                    status_code=409,
                )
            if request.agent_version_id != version.id:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="AgentRequest.agent_version_id pin mismatch.",
                    status_code=409,
                )
            if request.requester_id != requester_id:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="AgentRequest.requester_id mismatch.",
                    status_code=409,
                )

        await self._advance_pipeline(request.id)
        request = await self._requests.get(request.id)
        assert request is not None

        if request.status in _UNSAFE_TERMINAL:
            raise AppError(
                code="EXECUTION_NOT_READY",
                message=(
                    f"AgentRequest status {request.status} is not auto-executable "
                    "for AGENT_VERSION Schedule."
                ),
                status_code=409,
            )
        if request.status != AgentRequestStatus.READY.value:
            raise AppError(
                code="EXECUTION_NOT_READY",
                message=(
                    f"AgentRequest status {request.status} is not READY "
                    "for AGENT_VERSION Schedule auto-execute."
                ),
                status_code=409,
            )

        validation = await self._validations.get_latest_for_agent_request(request.id)
        plan_run = await self._plans.get_latest_for_agent_request(request.id)
        plan_step_count = None
        if plan_run is not None and isinstance(plan_run.plan_snapshot, dict):
            steps = plan_run.plan_snapshot.get("steps")
            if isinstance(steps, list):
                plan_step_count = len(steps)
        if not is_safe_auto_execute(
            request_status=request.status,
            plan_step_count=plan_step_count,
            confirmation_required=(
                None if validation is None else bool(validation.confirmation_required)
            ),
            policy_snapshot=(
                None if validation is None else validation.policy_snapshot
            ),
        ):
            raise AppError(
                code="EXECUTION_NOT_READY",
                message=(
                    "AGENT_VERSION Schedule auto-execute requires READY + "
                    "READ_ONLY tools without confirmation/approval."
                ),
                status_code=409,
            )

        # Reuse AgentRequest ExecutionCreation preflight (authz at create time).
        ctx = await self._execution_creation._preflight(request)  # noqa: SLF001
        policy_snapshot = ctx["policy_snapshot"]
        plan_run = ctx["plan_run"]
        validation = ctx["validation"]

        raced = await self._execution_for_occurrence(occurrence.id)
        if raced is not None:
            return raced

        materialized = await ExecutionPlanMaterializer(self._session).materialize(
            ExecutionMaterializeParams(
                source_type=ExecutionSourceType.SCHEDULE_OCCURRENCE.value,
                trigger_type=trigger_type,
                requester_id=requester_id,
                agent_request_id=request.id,
                agent_version_id=request.agent_version_id,
                workflow_version_id=None,
                schedule_occurrence_id=occurrence.id,
                plan_validation_run_id=validation.id,
                plan_snapshot=dict(plan_run.plan_snapshot),
                plan_hash=plan_run.plan_hash,
                input_snapshot={"request_text": request_text},
                policy_snapshot=dict(policy_snapshot),
                trace_id=request.trace_id,
                requested_at=now,
            )
        )
        execution = materialized.execution
        if execution.agent_version_id != schedule.agent_version_id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Created Execution.agent_version_id must equal "
                    "Schedule.agent_version_id."
                ),
                status_code=409,
            )

        from app.audit.writer import ACTION_EXECUTION_CREATE, AuditWriter
        from app.domain.enums import AuditResult

        await AuditWriter(self._session).append_system(
            actor_id="scheduler",
            action=ACTION_EXECUTION_CREATE,
            result=AuditResult.SUCCESS,
            resource_type="EXECUTION",
            resource_id=str(execution.id),
            execution_id=execution.id,
            change_set={
                "source_type": execution.source_type,
                "trigger_type": execution.trigger_type,
                "agent_request_id": str(execution.agent_request_id),
                "agent_version_id": str(execution.agent_version_id),
                "workflow_version_id": None,
                "schedule_occurrence_id": str(occurrence.id),
            },
            occurred_at=now,
        )
        # Keep occurrence PLANNED until queue staging → ENQUEUED.
        if occurrence.decision_reason == reasons.AGENT_PLAN_QUEUED:
            occurrence.decision_reason = reasons.DUE
        await self._session.flush()
        return execution

    async def _advance_pipeline(self, agent_request_id: uuid.UUID) -> None:
        request = await self._requests.get(agent_request_id)
        assert request is not None
        if request.status == AgentRequestStatus.RECEIVED.value:
            await RequestAnalyzerService(self._session).analyze(
                agent_request_id=agent_request_id
            )
            request = await self._requests.get(agent_request_id)
            assert request is not None
            if request.status != AgentRequestStatus.RETRIEVING.value:
                return

        if request.status == AgentRequestStatus.RETRIEVING.value:
            await ToolSelectorService(self._session).select(
                agent_request_id=agent_request_id
            )
            request = await self._requests.get(agent_request_id)
            assert request is not None
            if request.status != AgentRequestStatus.BUILDING_PARAMETERS.value:
                return

        if request.status == AgentRequestStatus.BUILDING_PARAMETERS.value:
            await ParameterBuilderService(self._session).build(
                agent_request_id=agent_request_id
            )
            request = await self._requests.get(agent_request_id)
            assert request is not None
            if request.status != AgentRequestStatus.PLANNING.value:
                return

        if request.status == AgentRequestStatus.PLANNING.value:
            await PlanGeneratorService(self._session).generate(
                agent_request_id=agent_request_id
            )
            request = await self._requests.get(agent_request_id)
            assert request is not None
            if request.status != AgentRequestStatus.VALIDATING.value:
                return

        if request.status == AgentRequestStatus.VALIDATING.value:
            await PlanValidatorService(self._session).validate(
                agent_request_id=agent_request_id
            )

    async def _execution_for_occurrence(
        self, occurrence_id: uuid.UUID
    ) -> Execution | None:
        stmt = select(Execution).where(
            Execution.schedule_occurrence_id == occurrence_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _fail_occurrence(
        self,
        occurrence: ScheduleOccurrence,
        *,
        reason: str,
        now: datetime,
    ) -> None:
        if occurrence.status in {
            OccurrenceStatus.COMPLETED.value,
            OccurrenceStatus.FAILED.value,
            OccurrenceStatus.SKIPPED.value,
        }:
            return
        occurrence.status = OccurrenceStatus.FAILED.value
        occurrence.decision_reason = reason
        occurrence.finished_at = now
        await self._session.flush()

    async def _mark_published(self, row: OutboxEvent, *, now: datetime) -> None:
        row.publish_attempt_count += 1
        row.last_attempt_at = now
        row.published_at = now
        row.last_error_code = None
        row.lock_version += 1
        await self._session.flush()

    @staticmethod
    def _payload_uuid(payload: Any, key: str) -> uuid.UUID | None:
        if not isinstance(payload, dict):
            return None
        raw = payload.get(key)
        if raw is None:
            return None
        try:
            return uuid.UUID(str(raw))
        except (ValueError, TypeError):
            return None
