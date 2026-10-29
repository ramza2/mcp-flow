"""AGENT_VERSION Schedule ONCE → AgentRequest pipeline → SCHEDULE_OCCURRENCE Execution.

Scheduler Schedule FOR UPDATE must never call LLM/MCP. This service is invoked
from the dedicated ``agent-schedule-planner`` process after a durable
``AGENT_SCHEDULE_PLAN`` row is created under the Schedule lock.

Reprocessing is lease-safe and idempotent:
* each event is claimed individually (fresh lease) immediately before LLM work
* claim carries ``worker_id`` + ``claim_token``; lost ownership cannot mutate
* ``agent_request_id`` on the Outbox payload resumes interrupted pipelines
* at most one Execution per ``schedule_occurrence_id``
* exhausted attempts fail-close the Occurrence and publish the Outbox row
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from app.model_provider.errors import ModelProviderError
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
_DEFAULT_MAX_ATTEMPTS = 8
_DEFAULT_LEASE_SECONDS = 120
_DEFAULT_PLAN_BATCH_LIMIT = 5
_CLAIM_KEY = "claim"


class ClaimLostError(RuntimeError):
    """Active Outbox claim no longer belongs to this worker/token."""


@dataclass(frozen=True, slots=True)
class AgentScheduleClaim:
    event_id: uuid.UUID
    worker_id: uuid.UUID
    claim_token: uuid.UUID
    lease_expires_at: datetime


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
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
        except IntegrityError:
            existing = (
                await self._session.execute(
                    select(OutboxEvent).where(OutboxEvent.dedupe_key == dedupe_key)
                )
            ).scalar_one_or_none()
            if existing is None:
                raise
            occurrence.decision_reason = reasons.AGENT_PLAN_QUEUED
            return existing
        occurrence.decision_reason = reasons.AGENT_PLAN_QUEUED
        return row

    @classmethod
    async def process_isolated_batch(
        cls,
        *,
        limit: int = _DEFAULT_PLAN_BATCH_LIMIT,
        now: datetime | None = None,
        worker_id: uuid.UUID | None = None,
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        session_scope_factory: Callable[[], Any] | None = None,
    ) -> AgentSchedulePlanBatchResult:
        """Claim **one event at a time**, then process (dedicated planner).

        Never pre-claims a multi-event batch: each LLM run starts with a fresh
        lease so later events cannot expire while earlier ones are planning.
        """
        from app.db.session import session_scope as default_scope

        scope = session_scope_factory or default_scope
        wid = worker_id or uuid.uuid4()
        bounded = max(1, min(int(limit), _DEFAULT_PLAN_BATCH_LIMIT))

        executed = 0
        failed = 0
        skipped = 0
        selected = 0
        for _ in range(bounded):
            ts = now or datetime.now(UTC)
            async with scope() as session:
                claim = await cls(session).claim_next(
                    now=ts,
                    worker_id=wid,
                    lease_seconds=lease_seconds,
                    max_attempts=max_attempts,
                )
                await session.commit()
            if claim is None:
                break
            selected += 1
            try:
                async with scope() as session:
                    outcome = await cls(session).process_claimed(
                        claim.event_id,
                        now=ts,
                        worker_id=claim.worker_id,
                        claim_token=claim.claim_token,
                        lease_seconds=lease_seconds,
                        max_attempts=max_attempts,
                    )
                    await session.commit()
            except Exception:
                logger.warning(
                    "AGENT_SCHEDULE_PLAN process failed event_id=%s",
                    claim.event_id,
                    exc_info=True,
                )
                # Keep lease until expiry so this tick cannot thrash-reclaim.
                try:
                    async with scope() as session:
                        await cls(session).record_transient_failure(
                            claim.event_id,
                            now=datetime.now(UTC),
                            worker_id=claim.worker_id,
                            claim_token=claim.claim_token,
                            error_code="PLAN_TRANSIENT_FAILURE",
                        )
                        await session.commit()
                except Exception:
                    logger.warning(
                        "AGENT_SCHEDULE_PLAN transient record failed event_id=%s",
                        claim.event_id,
                        exc_info=True,
                    )
                failed += 1
                continue
            if outcome == "EXECUTED":
                executed += 1
            elif outcome == "FAILED":
                failed += 1
            else:
                skipped += 1
        return AgentSchedulePlanBatchResult(
            selected=selected,
            executed=executed,
            failed=failed,
            skipped=skipped,
        )

    async def process_unpublished_batch(
        self,
        *,
        limit: int = 20,
        now: datetime | None = None,
        worker_id: uuid.UUID | None = None,
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    ) -> AgentSchedulePlanBatchResult:
        """Per-event claim+process in one session (unit tests / single worker)."""
        wid = worker_id or uuid.uuid4()
        bounded = max(1, min(int(limit), 200))
        executed = 0
        failed = 0
        skipped = 0
        selected = 0
        for _ in range(bounded):
            ts = now or datetime.now(UTC)
            claim = await self.claim_next(
                now=ts,
                worker_id=wid,
                lease_seconds=lease_seconds,
                max_attempts=max_attempts,
            )
            if claim is None:
                break
            selected += 1
            try:
                outcome = await self.process_claimed(
                    claim.event_id,
                    now=ts,
                    worker_id=claim.worker_id,
                    claim_token=claim.claim_token,
                    lease_seconds=lease_seconds,
                    max_attempts=max_attempts,
                )
            except Exception:
                logger.warning(
                    "AGENT_SCHEDULE_PLAN process failed event_id=%s",
                    claim.event_id,
                    exc_info=True,
                )
                await self.record_transient_failure(
                    claim.event_id,
                    now=datetime.now(UTC),
                    worker_id=claim.worker_id,
                    claim_token=claim.claim_token,
                    error_code="PLAN_TRANSIENT_FAILURE",
                )
                failed += 1
                continue
            if outcome == "EXECUTED":
                executed += 1
            elif outcome == "FAILED":
                failed += 1
            else:
                skipped += 1
        return AgentSchedulePlanBatchResult(
            selected=selected,
            executed=executed,
            failed=failed,
            skipped=skipped,
        )

    async def claim_next(
        self,
        *,
        now: datetime,
        worker_id: uuid.UUID,
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    ) -> AgentScheduleClaim | None:
        """Claim at most one unpublished AGENT_SCHEDULE_PLAN with a fresh lease."""
        claimed = await self.claim_lease_batch(
            limit=1,
            now=now,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            max_attempts=max_attempts,
        )
        if not claimed:
            return None
        row = claimed[0]
        payload = dict(row.payload) if isinstance(row.payload, dict) else {}
        claim = payload.get(_CLAIM_KEY)
        if not isinstance(claim, dict):
            return None
        token = uuid.UUID(str(claim["claim_token"]))
        expires = self._parse_lease_expiry(claim.get("lease_expires_at"))
        assert expires is not None
        return AgentScheduleClaim(
            event_id=row.id,
            worker_id=worker_id,
            claim_token=token,
            lease_expires_at=expires,
        )

    async def claim_lease_batch(
        self,
        *,
        limit: int,
        now: datetime,
        worker_id: uuid.UUID,
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    ) -> list[OutboxEvent]:
        """Durable claim under FOR UPDATE; prefer ``claim_next`` for planners."""
        bounded = max(1, min(int(limit), 200))
        # Over-fetch so active foreign leases / exhaustions can be skipped.
        stmt = (
            select(OutboxEvent)
            .where(
                OutboxEvent.event_type == _EVENT_TYPE,
                OutboxEvent.published_at.is_(None),
            )
            .order_by(OutboxEvent.created_at.asc(), OutboxEvent.id.asc())
            .limit(max(bounded * 3, bounded))
            .with_for_update(skip_locked=True)
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        claimed: list[OutboxEvent] = []
        for row in rows:
            if len(claimed) >= bounded:
                break
            payload = dict(row.payload) if isinstance(row.payload, dict) else {}
            # Any active lease (including this worker) blocks reclaim — prevents
            # same-tick thrash after transient failure while lease still holds.
            if self._has_active_lease(payload, now=now):
                continue
            if int(row.publish_attempt_count or 0) >= max_attempts:
                await self._exhaust_attempts(row, payload=payload, now=now)
                continue
            claim_token = uuid.uuid4()
            expires = now + timedelta(seconds=max(1, int(lease_seconds)))
            row.publish_attempt_count = int(row.publish_attempt_count or 0) + 1
            row.last_attempt_at = now
            row.last_error_code = None
            payload[_CLAIM_KEY] = {
                "worker_id": str(worker_id),
                "claim_token": str(claim_token),
                "lease_expires_at": expires.astimezone(UTC)
                .isoformat()
                .replace("+00:00", "Z"),
            }
            row.payload = payload
            row.lock_version = int(row.lock_version or 1) + 1
            claimed.append(row)
        await self._session.flush()
        return claimed

    async def process_claimed(
        self,
        event_id: uuid.UUID,
        *,
        now: datetime,
        worker_id: uuid.UUID,
        claim_token: uuid.UUID,
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    ) -> str:
        row = await self._lock_owned_outbox(
            event_id,
            worker_id=worker_id,
            claim_token=claim_token,
            now=now,
            renew_lease_seconds=lease_seconds,
        )
        if row is None:
            return "SKIPPED"
        try:
            return await self._process_one(
                row,
                now=now,
                worker_id=worker_id,
                claim_token=claim_token,
                lease_seconds=lease_seconds,
                max_attempts=max_attempts,
            )
        except ClaimLostError:
            logger.info(
                "AGENT_SCHEDULE_PLAN claim lost event_id=%s worker_id=%s",
                event_id,
                worker_id,
            )
            return "SKIPPED"

    async def clear_claim_after_failure(
        self,
        event_id: uuid.UUID,
        *,
        now: datetime,
        error_code: str,
        worker_id: uuid.UUID,
        claim_token: uuid.UUID,
    ) -> bool:
        """Clear claim only when worker_id + claim_token still own the row."""
        row = (
            await self._session.execute(
                select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or row.published_at is not None:
            return False
        payload = dict(row.payload) if isinstance(row.payload, dict) else {}
        if not self._owns_claim(
            payload, worker_id=worker_id, claim_token=claim_token, now=now
        ):
            return False
        payload.pop(_CLAIM_KEY, None)
        row.payload = payload
        row.last_attempt_at = now
        row.last_error_code = error_code
        row.lock_version = int(row.lock_version or 1) + 1
        await self._session.flush()
        return True

    async def record_transient_failure(
        self,
        event_id: uuid.UUID,
        *,
        now: datetime,
        worker_id: uuid.UUID,
        claim_token: uuid.UUID,
        error_code: str,
    ) -> bool:
        """Record error without releasing lease (owner+token required)."""
        row = (
            await self._session.execute(
                select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or row.published_at is not None:
            return False
        payload = dict(row.payload) if isinstance(row.payload, dict) else {}
        if not self._owns_claim(
            payload, worker_id=worker_id, claim_token=claim_token, now=now
        ):
            return False
        row.last_attempt_at = now
        row.last_error_code = error_code
        row.lock_version = int(row.lock_version or 1) + 1
        await self._session.flush()
        return True

    async def _exhaust_attempts(
        self,
        row: OutboxEvent,
        *,
        payload: dict[str, Any],
        now: datetime,
    ) -> None:
        occurrence_id = self._payload_uuid(payload, "schedule_occurrence_id")
        if occurrence_id is not None:
            occurrence = (
                await self._session.execute(
                    select(ScheduleOccurrence)
                    .where(ScheduleOccurrence.id == occurrence_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if occurrence is not None:
                await self._fail_occurrence(
                    occurrence,
                    reason=reasons.TARGET_PRECONDITION_FAILED,
                    now=now,
                )
        await self._mark_published(
            row,
            now=now,
            bump_attempt=False,
            last_error_code="PLAN_ATTEMPTS_EXHAUSTED",
        )

    async def _process_one(
        self,
        row: OutboxEvent,
        *,
        now: datetime,
        worker_id: uuid.UUID,
        claim_token: uuid.UUID,
        lease_seconds: int,
        max_attempts: int,
    ) -> str:
        del max_attempts  # attempt budget enforced at claim time
        payload = row.payload if isinstance(row.payload, dict) else {}
        try:
            occurrence_id = uuid.UUID(str(payload["schedule_occurrence_id"]))
            schedule_id = uuid.UUID(str(payload["schedule_id"]))
            agent_version_id = uuid.UUID(str(payload["agent_version_id"]))
            requester_id = uuid.UUID(str(payload["requester_id"]))
            request_text = str(payload["request_text"])
            trigger_type = str(payload.get("trigger_type") or _TRIGGER_SCHEDULE)
        except (KeyError, ValueError, TypeError):
            await self._owned_mark_published(
                row.id,
                now=now,
                worker_id=worker_id,
                claim_token=claim_token,
                last_error_code="PAYLOAD_INVALID",
            )
            return "FAILED"

        existing_exec = await self._execution_for_occurrence(occurrence_id)
        if existing_exec is not None:
            await self._owned_mark_published(
                row.id, now=now, worker_id=worker_id, claim_token=claim_token
            )
            return "SKIPPED"

        occurrence = (
            await self._session.execute(
                select(ScheduleOccurrence)
                .where(ScheduleOccurrence.id == occurrence_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if occurrence is None:
            await self._owned_mark_published(
                row.id, now=now, worker_id=worker_id, claim_token=claim_token
            )
            return "SKIPPED"
        if occurrence.status not in {
            OccurrenceStatus.PLANNED.value,
        }:
            await self._owned_mark_published(
                row.id, now=now, worker_id=worker_id, claim_token=claim_token
            )
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
            await self._owned_fail_and_publish(
                occurrence,
                outbox_id=row.id,
                reason=reasons.TARGET_PRECONDITION_FAILED,
                now=now,
                worker_id=worker_id,
                claim_token=claim_token,
            )
            return "FAILED"

        try:
            execution = await self._plan_and_materialize(
                schedule=schedule,
                occurrence=occurrence,
                agent_version_id=agent_version_id,
                requester_id=requester_id,
                request_text=request_text,
                trigger_type=trigger_type,
                outbox_id=row.id,
                now=now,
                worker_id=worker_id,
                claim_token=claim_token,
                lease_seconds=lease_seconds,
            )
        except ClaimLostError:
            raise
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
            await self._owned_fail_and_publish(
                occurrence,
                outbox_id=row.id,
                reason=reason,
                now=now,
                worker_id=worker_id,
                claim_token=claim_token,
            )
            return "FAILED"
        except Exception:
            # Leave unpublished + keep lease until expiry (no same-tick reclaim).
            await self.record_transient_failure(
                row.id,
                now=datetime.now(UTC),
                worker_id=worker_id,
                claim_token=claim_token,
                error_code="PLAN_TRANSIENT_FAILURE",
            )
            raise

        assert execution is not None
        await self._owned_mark_published(
            row.id, now=now, worker_id=worker_id, claim_token=claim_token
        )
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
        outbox_id: uuid.UUID,
        now: datetime,
        worker_id: uuid.UUID,
        claim_token: uuid.UUID,
        lease_seconds: int,
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

        outbox_row = await self._require_owned_outbox(
            outbox_id,
            worker_id=worker_id,
            claim_token=claim_token,
            now=now,
            renew_lease_seconds=lease_seconds,
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
            # Re-lock after writes; Analyzer may commit and release earlier locks.
            outbox_row = await self._require_owned_outbox(
                outbox_id,
                worker_id=worker_id,
                claim_token=claim_token,
                now=datetime.now(UTC),
                renew_lease_seconds=lease_seconds,
            )
            payload = dict(outbox_row.payload or {})
            payload["agent_request_id"] = str(agent_request_id)
            outbox_row.payload = payload
            outbox_row.lock_version = int(outbox_row.lock_version or 1) + 1
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
        # Pipeline services commit internally — re-assert claim before side effects.
        await self._require_owned_outbox(
            outbox_id,
            worker_id=worker_id,
            claim_token=claim_token,
            now=datetime.now(UTC),
            renew_lease_seconds=lease_seconds,
        )
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
            policy_snapshot=(None if validation is None else validation.policy_snapshot),
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

        # Final ownership gate immediately before Execution create.
        await self._require_owned_outbox(
            outbox_id,
            worker_id=worker_id,
            claim_token=claim_token,
            now=datetime.now(UTC),
            renew_lease_seconds=lease_seconds,
        )

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
            try:
                await RequestAnalyzerService(self._session).analyze(
                    agent_request_id=agent_request_id,
                    allow_retryable_provider_retry=True,
                )
            except ModelProviderError as exc:
                # Retryable provider errors release AgentRequest to RECEIVED
                # so the same Outbox claim/retry can re-enter Analyzer. Non-
                # retryable errors stay fail-closed (FAILED) and terminalize
                # the Occurrence via AppError below.
                if exc.retryable:
                    raise
                raise AppError(
                    code="EXECUTION_PRECONDITION_FAILED",
                    message=(
                        "AGENT_VERSION Schedule Analyzer failed with a "
                        f"non-retryable provider error ({exc.error_code})."
                    ),
                    status_code=409,
                ) from exc
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

    async def _owned_fail_and_publish(
        self,
        occurrence: ScheduleOccurrence,
        *,
        outbox_id: uuid.UUID,
        reason: str,
        now: datetime,
        worker_id: uuid.UUID,
        claim_token: uuid.UUID,
    ) -> None:
        await self._require_owned_outbox(
            outbox_id, worker_id=worker_id, claim_token=claim_token, now=now
        )
        await self._fail_occurrence(occurrence, reason=reason, now=now)
        await self._owned_mark_published(
            outbox_id, now=now, worker_id=worker_id, claim_token=claim_token
        )

    async def _owned_mark_published(
        self,
        outbox_id: uuid.UUID,
        *,
        now: datetime,
        worker_id: uuid.UUID,
        claim_token: uuid.UUID,
        last_error_code: str | None = None,
    ) -> None:
        row = await self._require_owned_outbox(
            outbox_id, worker_id=worker_id, claim_token=claim_token, now=now
        )
        await self._mark_published(
            row, now=now, bump_attempt=False, last_error_code=last_error_code
        )

    async def _mark_published(
        self,
        row: OutboxEvent,
        *,
        now: datetime,
        bump_attempt: bool = True,
        last_error_code: str | None = None,
    ) -> None:
        if bump_attempt:
            row.publish_attempt_count = int(row.publish_attempt_count or 0) + 1
        row.last_attempt_at = now
        row.published_at = now
        row.last_error_code = last_error_code
        payload = dict(row.payload) if isinstance(row.payload, dict) else {}
        payload.pop(_CLAIM_KEY, None)
        row.payload = payload
        row.lock_version = int(row.lock_version or 1) + 1
        await self._session.flush()

    async def _lock_owned_outbox(
        self,
        event_id: uuid.UUID,
        *,
        worker_id: uuid.UUID,
        claim_token: uuid.UUID,
        now: datetime,
        renew_lease_seconds: int | None = None,
    ) -> OutboxEvent | None:
        try:
            return await self._require_owned_outbox(
                event_id,
                worker_id=worker_id,
                claim_token=claim_token,
                now=now,
                renew_lease_seconds=renew_lease_seconds,
            )
        except ClaimLostError:
            return None

    async def _require_owned_outbox(
        self,
        event_id: uuid.UUID,
        *,
        worker_id: uuid.UUID,
        claim_token: uuid.UUID,
        now: datetime,
        renew_lease_seconds: int | None = None,
    ) -> OutboxEvent:
        row = (
            await self._session.execute(
                select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or row.published_at is not None:
            raise ClaimLostError(f"outbox {event_id} missing or published")
        payload = dict(row.payload) if isinstance(row.payload, dict) else {}
        if not self._owns_claim(
            payload, worker_id=worker_id, claim_token=claim_token, now=now
        ):
            raise ClaimLostError(f"outbox {event_id} claim lost")
        if renew_lease_seconds is not None:
            expires = now + timedelta(seconds=max(1, int(renew_lease_seconds)))
            claim = dict(payload.get(_CLAIM_KEY) or {})
            claim["worker_id"] = str(worker_id)
            claim["claim_token"] = str(claim_token)
            claim["lease_expires_at"] = (
                expires.astimezone(UTC).isoformat().replace("+00:00", "Z")
            )
            payload[_CLAIM_KEY] = claim
            row.payload = payload
            row.last_attempt_at = now
            row.lock_version = int(row.lock_version or 1) + 1
            await self._session.flush()
        return row

    @classmethod
    def _owns_claim(
        cls,
        payload: dict[str, Any],
        *,
        worker_id: uuid.UUID,
        claim_token: uuid.UUID,
        now: datetime,
    ) -> bool:
        claim = payload.get(_CLAIM_KEY)
        if not isinstance(claim, dict):
            return False
        if claim.get("worker_id") != str(worker_id):
            return False
        if claim.get("claim_token") != str(claim_token):
            return False
        expires = cls._parse_lease_expiry(claim.get("lease_expires_at"))
        if expires is None:
            return False
        return expires > now

    @staticmethod
    def _parse_lease_expiry(raw: Any) -> datetime | None:
        if raw is None:
            return None
        try:
            expires = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return expires

    @staticmethod
    def _has_active_lease(payload: dict[str, Any], *, now: datetime) -> bool:
        claim = payload.get(_CLAIM_KEY)
        if not isinstance(claim, dict):
            return False
        if not claim.get("worker_id") or not claim.get("claim_token"):
            return False
        expires = AgentScheduleExecutionService._parse_lease_expiry(
            claim.get("lease_expires_at")
        )
        if expires is None:
            return False
        return expires > now

    @staticmethod
    def _has_active_foreign_lease(
        payload: dict[str, Any],
        *,
        worker_id: uuid.UUID,
        now: datetime,
    ) -> bool:
        claim = payload.get(_CLAIM_KEY)
        if not isinstance(claim, dict):
            return False
        claim_worker = claim.get("worker_id")
        expires = AgentScheduleExecutionService._parse_lease_expiry(
            claim.get("lease_expires_at")
        )
        if not claim_worker or expires is None:
            return False
        if str(claim_worker) == str(worker_id):
            return False
        return expires > now

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
