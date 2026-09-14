"""Clarification / Confirmation resume service (docs/04 §4, docs/05 §10.4).

Answers OPEN ClarificationRequest and resumes AgentRequest. Does not call
Selector / ParameterBuilder / PlanValidator / LLM / MCP / SecretResolver.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import (
    AgentRequestStatus,
    ClarificationRequestStatus,
    ClarificationRequestType,
    ParameterProvenance,
)
from app.models.conversation import AgentRequest
from app.models.tool_selection import ClarificationRequest
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.clarification_request import ClarificationRequestRepository
from app.repositories.plan_generation import PlanGenerationRepository
from app.repositories.plan_validation import PlanValidationRepository
from app.repositories.tool_selection import ToolSelectionRepository
from app.schemas.clarification import (
    CONFIRMATION_QUESTION_SCHEMA,
    ClarificationResponseResult,
    ClarificationResponseSubmit,
)
from app.schemas.execution_plan import compute_plan_hash
from app.schemas.structured_request import (
    STRUCTURED_REQUEST_SCHEMA_VERSION,
    StructuredRequestEntity,
    StructuredRequestV1,
)
from app.services.agent_request import AgentRequestService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ClarificationResponseOutcome:
    """Internal outcome — mirrors ClarificationResponseResult fields."""

    agent_request_id: UUID
    clarification_id: UUID
    clarification_status: str
    agent_request_status: AgentRequestStatus


class ClarificationResponseService:
    """OPEN ClarificationRequest → ANSWERED + AgentRequest resume."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._requests = AgentRequestRepository(session)
        self._request_service = AgentRequestService(session)
        self._clarifications = ClarificationRequestRepository(session)
        self._selections = ToolSelectionRepository(session)
        self._plan_generations = PlanGenerationRepository(session)
        self._plan_validations = PlanValidationRepository(session)

    async def submit_response(
        self,
        *,
        agent_request_id: UUID,
        clarification_id: UUID,
        requester_id: UUID,
        body: ClarificationResponseSubmit,
    ) -> ClarificationResponseOutcome:
        request = await self._requests.get(agent_request_id)
        if request is None:
            raise AppError(
                code="NOT_FOUND",
                message="AgentRequest를 찾을 수 없습니다.",
                status_code=404,
            )
        if request.requester_id != requester_id:
            raise AppError(
                code="FORBIDDEN",
                message="AgentRequest requester만 Clarification에 응답할 수 있습니다.",
                status_code=403,
            )

        clarification = await self._clarifications.get(clarification_id)
        if clarification is None or clarification.agent_request_id != request.id:
            raise AppError(
                code="NOT_FOUND",
                message="ClarificationRequest를 찾을 수 없습니다.",
                status_code=404,
            )

        if clarification.status != ClarificationRequestStatus.OPEN.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="OPEN ClarificationRequest에만 응답할 수 있습니다.",
                status_code=409,
            )

        if clarification.expires_at is not None and clarification.expires_at <= datetime.now(
            UTC
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="만료된 ClarificationRequest에는 응답할 수 없습니다.",
                status_code=409,
            )

        open_row = await self._clarifications.get_open_for_agent_request(request.id)
        if open_row is None or open_row.id != clarification.id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="최신 OPEN ClarificationRequest와 일치하지 않습니다.",
                status_code=409,
            )

        request_type = clarification.request_type
        self._assert_status_type_consistency(request, request_type)

        payload = body.response_payload
        try:
            if request_type == ClarificationRequestType.MISSING_PARAMETER.value:
                return await self._answer_missing_parameter(
                    request=request,
                    clarification=clarification,
                    payload=payload,
                    requester_id=requester_id,
                )
            if request_type == ClarificationRequestType.TOOL_CONFIRMATION.value:
                return await self._answer_tool_confirmation(
                    request=request,
                    clarification=clarification,
                    payload=payload,
                    requester_id=requester_id,
                )
            if request_type == ClarificationRequestType.PLAN_CONFIRMATION.value:
                return await self._answer_plan_confirmation(
                    request=request,
                    clarification=clarification,
                    payload=payload,
                    requester_id=requester_id,
                )
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"지원하지 않는 clarification type={request_type!r}",
                status_code=409,
            )
        except AppError:
            await self._session.rollback()
            raise
        except Exception:
            await self._session.rollback()
            raise

    def _assert_status_type_consistency(
        self, request: AgentRequest, request_type: str
    ) -> None:
        status = request.status
        expected = {
            ClarificationRequestType.MISSING_PARAMETER.value: (
                AgentRequestStatus.WAITING_INPUT.value
            ),
            ClarificationRequestType.TOOL_CONFIRMATION.value: (
                AgentRequestStatus.WAITING_CONFIRMATION.value
            ),
            ClarificationRequestType.PLAN_CONFIRMATION.value: (
                AgentRequestStatus.WAITING_CONFIRMATION.value
            ),
        }.get(request_type)
        if expected is None or status != expected:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"Clarification type={request_type!r}와 "
                    f"AgentRequest status={status!r}가 일치하지 않습니다."
                ),
                status_code=409,
            )

    async def _answer_missing_parameter(
        self,
        *,
        request: AgentRequest,
        clarification: ClarificationRequest,
        payload: dict[str, Any],
        requester_id: UUID,
    ) -> ClarificationResponseOutcome:
        schema = clarification.question_schema
        if not isinstance(schema, dict):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Clarification question_schema가 손상되었습니다.",
                status_code=409,
            )
        validated = _validate_against_question_schema(schema, payload)

        if request.structured_request_version != STRUCTURED_REQUEST_SCHEMA_VERSION:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="structured_request_version이 1.0이 아닙니다.",
                status_code=409,
            )
        try:
            structured = StructuredRequestV1.model_validate(request.structured_request)
        except Exception as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="StructuredRequest 재검증에 실패했습니다.",
                status_code=409,
            ) from exc

        schema_required = schema.get("required", [])
        if not isinstance(schema_required, list) or not all(
            isinstance(x, str) for x in schema_required
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="question_schema.required가 유효하지 않습니다.",
                status_code=409,
            )
        req_missing = {_norm(x) for x in (request.missing_fields or [])}
        schema_missing = {_norm(x) for x in schema_required}
        if req_missing != schema_missing:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="AgentRequest.missing_fields와 question_schema.required가 "
                "일치하지 않습니다.",
                status_code=409,
            )

        properties = schema.get("properties") or {}
        if not isinstance(properties, dict):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="question_schema.properties가 object가 아닙니다.",
                status_code=409,
            )

        # Exact field names from properties keys (lookup by normalized name).
        exact_by_norm = {_norm(k): k for k in properties}
        for key in validated:
            if _norm(key) not in exact_by_norm:
                # validated keys already constrained by schema; keep defensive
                raise AppError(
                    code="VALIDATION_ERROR",
                    message=f"unknown field={key!r}",
                    status_code=400,
                )

        entities = list(structured.entities)
        for response_key, response_value in validated.items():
            exact_name = exact_by_norm[_norm(response_key)]
            matching = [
                e for e in entities if _norm(e.name) == _norm(exact_name)
            ]
            sources = {e.source for e in matching}
            if (
                ParameterProvenance.SECRET_REFERENCE in sources
                and len(sources) > 1
            ):
                raise AppError(
                    code="VALIDATION_ERROR",
                    message=(
                        f"field={exact_name!r}에 SECRET_REFERENCE와 "
                        "다른 provenance가 혼합되어 있습니다."
                    ),
                    status_code=400,
                )
            if (
                len(matching) == 1
                and matching[0].source == ParameterProvenance.SECRET_REFERENCE
            ):
                secret_id = _parse_secret_uuid(response_value, field=exact_name)
                replacement = StructuredRequestEntity(
                    name=exact_name,
                    value=str(secret_id),
                    source=ParameterProvenance.SECRET_REFERENCE,
                )
            else:
                replacement = StructuredRequestEntity(
                    name=exact_name,
                    value=response_value,
                    source=ParameterProvenance.CONVERSATION_CONFIRMED,
                )
            entities = [
                e for e in entities if _norm(e.name) != _norm(exact_name)
            ]
            entities.append(replacement)

        answered_norms = {_norm(k) for k in validated}
        new_missing = [
            m for m in structured.missing_inputs if _norm(m) not in answered_norms
        ]
        if new_missing or structured.ambiguities:
            raise AppError(
                code="VALIDATION_ERROR",
                message=(
                    "MISSING_PARAMETER 응답 후에도 unresolved missing_inputs/"
                    "ambiguities가 남아 있습니다."
                ),
                status_code=400,
            )

        updated = StructuredRequestV1.model_validate(
            {
                **structured.model_dump(mode="json"),
                "entities": [e.model_dump(mode="json") for e in entities],
                "missing_inputs": [],
                "ambiguities": [],
                "needs_clarification": False,
            }
        )

        try:
            await self._clarifications.answer_open(
                clarification_id=clarification.id,
                agent_request_id=request.id,
                response_payload=validated,
                answered_by=requester_id,
            )
            await self._request_service.compare_and_set_status(
                request.id,
                expected_statuses=[AgentRequestStatus.WAITING_INPUT],
                new_status=AgentRequestStatus.RETRIEVING,
                set_completed_at=False,
                completed_at=None,
                extra_values={
                    "structured_request": updated.model_dump(mode="json"),
                    "structured_request_version": STRUCTURED_REQUEST_SCHEMA_VERSION,
                    "missing_fields": [],
                },
            )
            await self._session.commit()
        except AppError:
            await self._session.rollback()
            raise

        logger.info(
            "clarification_answered agent_request_id=%s clarification_id=%s "
            "type=MISSING_PARAMETER next_status=RETRIEVING field_count=%s",
            request.id,
            clarification.id,
            len(validated),
        )
        return ClarificationResponseOutcome(
            agent_request_id=request.id,
            clarification_id=clarification.id,
            clarification_status=ClarificationRequestStatus.ANSWERED.value,
            agent_request_status=AgentRequestStatus.RETRIEVING,
        )

    async def _answer_tool_confirmation(
        self,
        *,
        request: AgentRequest,
        clarification: ClarificationRequest,
        payload: dict[str, Any],
        requester_id: UUID,
    ) -> ClarificationResponseOutcome:
        confirmed = _validate_confirmation_payload(
            clarification.question_schema, payload
        )
        selection = await self._selections.get_latest_for_agent_request(request.id)
        if (
            selection is None
            or selection.agent_request_id != request.id
            or selection.agent_version_id != request.agent_version_id
            or selection.decision != "CONFIRM"
            or selection.selected_tool_version_id is None
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="TOOL_CONFIRMATION에 대응하는 ToolSelectionRun이 유효하지 않습니다.",
                status_code=409,
            )
        candidates = await self._selections.list_candidates_for_run(selection.id)
        if not any(
            c.tool_version_id == selection.selected_tool_version_id for c in candidates
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="selected ToolVersion이 ToolSelection candidate evidence에 없습니다.",
                status_code=409,
            )

        response_payload = {"confirmed": confirmed}
        try:
            await self._clarifications.answer_open(
                clarification_id=clarification.id,
                agent_request_id=request.id,
                response_payload=response_payload,
                answered_by=requester_id,
            )
            if confirmed:
                await self._request_service.compare_and_set_status(
                    request.id,
                    expected_statuses=[AgentRequestStatus.WAITING_CONFIRMATION],
                    new_status=AgentRequestStatus.BUILDING_PARAMETERS,
                    set_completed_at=False,
                    completed_at=None,
                )
                next_status = AgentRequestStatus.BUILDING_PARAMETERS
            else:
                await self._request_service.compare_and_set_status(
                    request.id,
                    expected_statuses=[AgentRequestStatus.WAITING_CONFIRMATION],
                    new_status=AgentRequestStatus.CANCELLED,
                    set_completed_at=True,
                    completed_at=datetime.now(UTC),
                )
                next_status = AgentRequestStatus.CANCELLED
            await self._session.commit()
        except AppError:
            await self._session.rollback()
            raise

        logger.info(
            "clarification_answered agent_request_id=%s clarification_id=%s "
            "type=TOOL_CONFIRMATION next_status=%s",
            request.id,
            clarification.id,
            next_status.value,
        )
        return ClarificationResponseOutcome(
            agent_request_id=request.id,
            clarification_id=clarification.id,
            clarification_status=ClarificationRequestStatus.ANSWERED.value,
            agent_request_status=next_status,
        )

    async def _answer_plan_confirmation(
        self,
        *,
        request: AgentRequest,
        clarification: ClarificationRequest,
        payload: dict[str, Any],
        requester_id: UUID,
    ) -> ClarificationResponseOutcome:
        confirmed = _validate_confirmation_payload(
            clarification.question_schema, payload
        )
        response_payload = {"confirmed": confirmed}

        if confirmed:
            validation = await self._plan_validations.get_latest_for_agent_request(
                request.id
            )
            if (
                validation is None
                or validation.decision != "WAITING_CONFIRMATION"
                or not validation.confirmation_required
                or validation.clarification_request_id != clarification.id
                or validation.agent_request_id != request.id
            ):
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="PLAN_CONFIRMATION에 대응하는 PlanValidationRun이 유효하지 않습니다.",
                    status_code=409,
                )
            plan_run = await self._plan_generations.get_by_id(
                validation.plan_generation_run_id
            )
            latest_plan = await self._plan_generations.get_latest_for_agent_request(
                request.id
            )
            if (
                plan_run is None
                or latest_plan is None
                or latest_plan.id != validation.plan_generation_run_id
            ):
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="PLAN_CONFIRMATION PlanGenerationRun identity가 일치하지 않습니다.",
                    status_code=409,
                )
            recomputed = compute_plan_hash(plan_run.plan_snapshot)
            if (
                validation.plan_hash != plan_run.plan_hash
                or plan_run.plan_hash != recomputed
            ):
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="PLAN_CONFIRMATION plan_hash binding이 일치하지 않습니다.",
                    status_code=409,
                )

        try:
            await self._clarifications.answer_open(
                clarification_id=clarification.id,
                agent_request_id=request.id,
                response_payload=response_payload,
                answered_by=requester_id,
            )
            if confirmed:
                await self._request_service.compare_and_set_status(
                    request.id,
                    expected_statuses=[AgentRequestStatus.WAITING_CONFIRMATION],
                    new_status=AgentRequestStatus.VALIDATING,
                    set_completed_at=False,
                    completed_at=None,
                )
                next_status = AgentRequestStatus.VALIDATING
            else:
                await self._request_service.compare_and_set_status(
                    request.id,
                    expected_statuses=[AgentRequestStatus.WAITING_CONFIRMATION],
                    new_status=AgentRequestStatus.CANCELLED,
                    set_completed_at=True,
                    completed_at=datetime.now(UTC),
                )
                next_status = AgentRequestStatus.CANCELLED
            await self._session.commit()
        except AppError:
            await self._session.rollback()
            raise

        logger.info(
            "clarification_answered agent_request_id=%s clarification_id=%s "
            "type=PLAN_CONFIRMATION next_status=%s",
            request.id,
            clarification.id,
            next_status.value,
        )
        return ClarificationResponseOutcome(
            agent_request_id=request.id,
            clarification_id=clarification.id,
            clarification_status=ClarificationRequestStatus.ANSWERED.value,
            agent_request_status=next_status,
        )


def _norm(value: str) -> str:
    return value.strip().casefold()


def _parse_secret_uuid(value: Any, *, field: str) -> uuid.UUID:
    if not isinstance(value, str):
        raise AppError(
            code="VALIDATION_ERROR",
            message=f"SECRET_REFERENCE field={field!r}는 UUID string이어야 합니다.",
            status_code=400,
        )
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError) as exc:
        raise AppError(
            code="VALIDATION_ERROR",
            message=f"SECRET_REFERENCE field={field!r}는 유효한 UUID여야 합니다.",
            status_code=400,
        ) from exc


def _validate_confirmation_payload(
    question_schema: Any, payload: dict[str, Any]
) -> bool:
    if question_schema != CONFIRMATION_QUESTION_SCHEMA:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Confirmation question_schema가 canonical contract와 불일치합니다.",
            status_code=409,
        )
    if set(payload.keys()) != {"confirmed"}:
        raise AppError(
            code="VALIDATION_ERROR",
            message="confirmation response는 confirmed boolean field만 허용합니다.",
            status_code=400,
        )
    confirmed = payload["confirmed"]
    if not isinstance(confirmed, bool):
        raise AppError(
            code="VALIDATION_ERROR",
            message="confirmed는 boolean이어야 합니다.",
            status_code=400,
        )
    return confirmed


def _validate_against_question_schema(
    schema: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    if schema.get("type") != "object":
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="question_schema.type은 object여야 합니다.",
            status_code=409,
        )
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="question_schema.properties가 object가 아닙니다.",
            status_code=409,
        )
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="question_schema.required는 string[]이어야 합니다.",
            status_code=409,
        )

    # Map payload keys to property keys via normalized lookup, then re-key to exact names.
    prop_by_norm = {_norm(k): k for k in properties}
    exact_payload: dict[str, Any] = {}
    for key, value in payload.items():
        exact = prop_by_norm.get(_norm(key))
        if exact is None:
            if schema.get("additionalProperties") is False:
                raise AppError(
                    code="VALIDATION_ERROR",
                    message=f"unknown field={key!r}",
                    status_code=400,
                )
            raise AppError(
                code="VALIDATION_ERROR",
                message=f"unknown field={key!r}",
                status_code=400,
            )
        exact_payload[exact] = value

    for req in required:
        if req not in exact_payload:
            raise AppError(
                code="VALIDATION_ERROR",
                message=f"required field={req!r}가 없습니다.",
                status_code=400,
            )

    if schema.get("additionalProperties") is False:
        unknown = set(exact_payload) - set(properties)
        if unknown:
            raise AppError(
                code="VALIDATION_ERROR",
                message=f"unknown fields={sorted(unknown)!r}",
                status_code=400,
            )

    for key, value in exact_payload.items():
        prop = properties.get(key)
        if not isinstance(prop, dict):
            continue
        schema_type = prop.get("type")
        if isinstance(schema_type, str) and not _literal_matches_type(value, schema_type):
            raise AppError(
                code="VALIDATION_ERROR",
                message=(
                    f"field={key!r} 값이 schema type={schema_type!r}과 일치하지 않습니다."
                ),
                status_code=400,
            )
    return exact_payload


def _literal_matches_type(value: Any, schema_type: str) -> bool:
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "object":
        return isinstance(value, dict)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "null":
        return value is None
    return True


def to_api_result(outcome: ClarificationResponseOutcome) -> ClarificationResponseResult:
    return ClarificationResponseResult(
        agent_request_id=outcome.agent_request_id,
        clarification_id=outcome.clarification_id,
        clarification_status=outcome.clarification_status,
        agent_request_status=outcome.agent_request_status.value,
    )
