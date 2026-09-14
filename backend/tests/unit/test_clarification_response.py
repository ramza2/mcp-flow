"""Unit tests for ClarificationResponseService resume foundation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.agent.parameter_builder import ParameterBuilderService
from app.agent.plan_validator import PlanValidatorService
from app.agent.tool_selector import ToolSelectorService
from app.core.errors import AppError
from app.domain.enums import (
    AgentRequestStatus,
    ClarificationRequestStatus,
    ClarificationRequestType,
    ParameterProvenance,
)
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.clarification_request import ClarificationRequestRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.repositories.plan_generation import PlanGenerationRepository
from app.repositories.plan_validation import PlanValidationRepository
from app.repositories.tool_selection import ToolSelectionRepository
from app.schemas.clarification import (
    CONFIRMATION_QUESTION_SCHEMA,
    ClarificationResponseSubmit,
)
from app.schemas.execution_plan import compute_plan_hash
from app.schemas.structured_request import StructuredRequestV1
from app.services.agent_request import AgentRequestService
from app.services.clarification_response import (
    ClarificationResponseService,
    _literal_matches_type,
    _validate_against_question_schema,
    _validate_confirmation_payload,
)
from sqlalchemy.ext.asyncio import AsyncSession

from tests.unit.test_parameter_builder import _seed_building_parameters
from tests.unit.test_plan_validator import _seed_validating
from tests.unit.test_tool_selector import (
    _candidate,
    _descriptor,
    _MockLLM,
    _MockRetrieval,
    _rerank_payload,
    _seed_retrieving,
)


def _submit(payload: dict[str, Any]) -> ClarificationResponseSubmit:
    return ClarificationResponseSubmit(response_payload=payload)


def test_schema_validation_missing_required() -> None:
    schema = {
        "type": "object",
        "properties": {"location": {"type": "string"}},
        "required": ["location"],
        "additionalProperties": False,
    }
    with pytest.raises(AppError) as exc:
        _validate_against_question_schema(schema, {})
    assert exc.value.code == "VALIDATION_ERROR"
    assert exc.value.status_code == 400


def test_schema_validation_unknown_extra() -> None:
    schema = {
        "type": "object",
        "properties": {"location": {"type": "string"}},
        "required": ["location"],
        "additionalProperties": False,
    }
    with pytest.raises(AppError) as exc:
        _validate_against_question_schema(
            schema, {"location": "서울", "extra": "x"}
        )
    assert exc.value.code == "VALIDATION_ERROR"


def test_schema_validation_wrong_primitive_type() -> None:
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
        "additionalProperties": False,
    }
    with pytest.raises(AppError) as exc:
        _validate_against_question_schema(schema, {"count": "1"})
    assert exc.value.code == "VALIDATION_ERROR"


def test_bool_not_accepted_as_integer_or_number() -> None:
    assert _literal_matches_type(True, "integer") is False
    assert _literal_matches_type(False, "number") is False
    assert _literal_matches_type(1, "integer") is True
    assert _literal_matches_type(1.5, "number") is True


def test_confirmation_bool_pass_and_reject() -> None:
    assert _validate_confirmation_payload(
        CONFIRMATION_QUESTION_SCHEMA, {"confirmed": True}
    )
    assert (
        _validate_confirmation_payload(
            CONFIRMATION_QUESTION_SCHEMA, {"confirmed": False}
        )
        is False
    )
    with pytest.raises(AppError):
        _validate_confirmation_payload(
            CONFIRMATION_QUESTION_SCHEMA, {"confirmed": "true"}
        )
    with pytest.raises(AppError):
        _validate_confirmation_payload(
            CONFIRMATION_QUESTION_SCHEMA, {"confirmed": True, "extra": 1}
        )
    with pytest.raises(AppError):
        _validate_confirmation_payload(
            {"type": "object", "properties": {}}, {"confirmed": True}
        )


async def _seed_missing_parameter(
    session: AsyncSession,
    *,
    entities: list[dict[str, Any]] | None = None,
    required: list[str] | None = None,
    missing_inputs: list[str] | None = None,
    ambiguities: list[str] | None = None,
) -> dict[str, Any]:
    seeded = await _seed_building_parameters(
        session,
        required=required or ["location"],
        entities=entities if entities is not None else [],
    )
    request = await AgentRequestRepository(session).get(seeded["request_id"])
    assert request is not None
    structured = StructuredRequestV1.model_validate(request.structured_request)
    updated = StructuredRequestV1.model_validate(
        {
            **structured.model_dump(mode="json"),
            "missing_inputs": list(missing_inputs)
            if missing_inputs is not None
            else list(required or ["location"]),
            "ambiguities": list(ambiguities or []),
            "needs_clarification": True,
        }
    )
    await AgentRequestService(session).compare_and_set_status(
        request.id,
        expected_statuses=[AgentRequestStatus.BUILDING_PARAMETERS],
        new_status=AgentRequestStatus.BUILDING_PARAMETERS,
        extra_values={
            "structured_request": updated.model_dump(mode="json"),
            "structured_request_version": "1.0",
        },
    )
    await session.commit()
    outcome = await ParameterBuilderService(session).build(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.agent_request_status == AgentRequestStatus.WAITING_INPUT
    clarification = await ClarificationRequestRepository(
        session
    ).get_open_for_agent_request(seeded["request_id"])
    assert clarification is not None
    request = await AgentRequestRepository(session).get(seeded["request_id"])
    assert request is not None
    return {
        **seeded,
        "clarification_id": clarification.id,
        "requester_id": request.requester_id,
    }


@pytest.mark.asyncio
async def test_missing_parameter_merge_and_retrieving(
    db_session: AsyncSession,
) -> None:
    seeded = await _seed_missing_parameter(db_session, entities=[])
    request = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert request is not None
    structured = StructuredRequestV1.model_validate(request.structured_request)
    patched = StructuredRequestV1.model_validate(
        {
            **structured.model_dump(mode="json"),
            "entities": [
                {
                    "name": "Location",
                    "value": "부산",
                    "source": ParameterProvenance.USER_EXPLICIT.value,
                },
                {
                    "name": "unrelated",
                    "value": "keep",
                    "source": ParameterProvenance.MODEL_DERIVED.value,
                },
            ],
            "missing_inputs": ["location"],
            "ambiguities": [],
            "needs_clarification": True,
        }
    )
    request.structured_request = patched.model_dump(mode="json")
    await db_session.commit()

    outcome = await ClarificationResponseService(db_session).submit_response(
        agent_request_id=seeded["request_id"],
        clarification_id=seeded["clarification_id"],
        requester_id=seeded["requester_id"],
        body=_submit({"location": "서울"}),
    )
    assert outcome.agent_request_status == AgentRequestStatus.RETRIEVING
    assert outcome.clarification_status == ClarificationRequestStatus.ANSWERED.value

    request = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert request is not None
    assert request.status == AgentRequestStatus.RETRIEVING.value
    assert request.missing_fields == []
    structured = StructuredRequestV1.model_validate(request.structured_request)
    assert structured.missing_inputs == []
    assert structured.ambiguities == []
    assert structured.needs_clarification is False
    location_entities = [e for e in structured.entities if e.name == "location"]
    assert len(location_entities) == 1
    assert location_entities[0].value == "서울"
    assert location_entities[0].source == ParameterProvenance.CONVERSATION_CONFIRMED
    assert any(e.name == "unrelated" and e.value == "keep" for e in structured.entities)

    clarification = await ClarificationRequestRepository(db_session).get(
        seeded["clarification_id"]
    )
    assert clarification is not None
    assert clarification.status == ClarificationRequestStatus.ANSWERED.value
    assert clarification.response_payload == {"location": "서울"}
    assert clarification.answered_by == seeded["requester_id"]


@pytest.mark.asyncio
async def test_missing_parameter_duplicate_entities_replaced(
    db_session: AsyncSession,
) -> None:
    seeded = await _seed_missing_parameter(db_session, entities=[])
    request = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert request is not None
    structured = StructuredRequestV1.model_validate(request.structured_request)
    patched = StructuredRequestV1.model_validate(
        {
            **structured.model_dump(mode="json"),
            "entities": [
                {
                    "name": "location",
                    "value": "a",
                    "source": ParameterProvenance.USER_EXPLICIT.value,
                },
                {
                    "name": " LOCATION ",
                    "value": "b",
                    "source": ParameterProvenance.MODEL_DERIVED.value,
                },
            ],
            "missing_inputs": ["location"],
            "needs_clarification": True,
        }
    )
    request.structured_request = patched.model_dump(mode="json")
    await db_session.commit()

    await ClarificationResponseService(db_session).submit_response(
        agent_request_id=seeded["request_id"],
        clarification_id=seeded["clarification_id"],
        requester_id=seeded["requester_id"],
        body=_submit({"location": "서울"}),
    )
    request = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert request is not None
    structured = StructuredRequestV1.model_validate(request.structured_request)
    matching = [e for e in structured.entities if e.name.strip().casefold() == "location"]
    assert len(matching) == 1
    assert matching[0].name == "location"
    assert matching[0].source == ParameterProvenance.CONVERSATION_CONFIRMED


@pytest.mark.asyncio
async def test_secret_reference_preserved_with_uuid(
    db_session: AsyncSession,
) -> None:
    secret_id = uuid.uuid4()
    seeded = await _seed_missing_parameter(
        db_session,
        required=["credential"],
        entities=[],
        missing_inputs=["credential"],
    )
    request = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert request is not None
    structured = StructuredRequestV1.model_validate(request.structured_request)
    patched = StructuredRequestV1.model_validate(
        {
            **structured.model_dump(mode="json"),
            "entities": [
                {
                    "name": "credential",
                    "value": str(uuid.uuid4()),
                    "source": ParameterProvenance.SECRET_REFERENCE.value,
                }
            ],
            "missing_inputs": ["credential"],
            "needs_clarification": True,
        }
    )
    request.structured_request = patched.model_dump(mode="json")
    await db_session.commit()

    outcome = await ClarificationResponseService(db_session).submit_response(
        agent_request_id=seeded["request_id"],
        clarification_id=seeded["clarification_id"],
        requester_id=seeded["requester_id"],
        body=_submit({"credential": str(secret_id)}),
    )
    assert outcome.agent_request_status == AgentRequestStatus.RETRIEVING
    request = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert request is not None
    structured = StructuredRequestV1.model_validate(request.structured_request)
    entity = next(e for e in structured.entities if e.name == "credential")
    assert entity.source == ParameterProvenance.SECRET_REFERENCE
    assert entity.value == str(secret_id)


@pytest.mark.asyncio
async def test_secret_plaintext_rejected_keeps_open(
    db_session: AsyncSession,
) -> None:
    seeded = await _seed_building_parameters(
        db_session,
        required=["credential"],
        entities=[
            {
                "name": "credential",
                "value": "plain-password",
                "source": ParameterProvenance.SECRET_REFERENCE.value,
            }
        ],
    )
    outcome = await ParameterBuilderService(db_session).build(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.agent_request_status == AgentRequestStatus.WAITING_INPUT
    clarification = await ClarificationRequestRepository(
        db_session
    ).get_open_for_agent_request(seeded["request_id"])
    assert clarification is not None
    clarification_id = clarification.id
    request = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert request is not None
    requester_id = request.requester_id

    with pytest.raises(AppError) as exc:
        await ClarificationResponseService(db_session).submit_response(
            agent_request_id=seeded["request_id"],
            clarification_id=clarification_id,
            requester_id=requester_id,
            body=_submit({"credential": "plain-password"}),
        )
    assert exc.value.code == "VALIDATION_ERROR"
    assert exc.value.status_code == 400

    clarification = await ClarificationRequestRepository(db_session).get(
        clarification_id
    )
    assert clarification is not None
    assert clarification.status == ClarificationRequestStatus.OPEN.value
    assert clarification.response_payload is None
    request = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert request is not None
    assert request.status == AgentRequestStatus.WAITING_INPUT.value


@pytest.mark.asyncio
async def test_mixed_secret_non_secret_duplicates_rejected(
    db_session: AsyncSession,
) -> None:
    seeded = await _seed_missing_parameter(
        db_session,
        required=["credential"],
        entities=[],
        missing_inputs=["credential"],
    )
    request = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert request is not None
    structured = StructuredRequestV1.model_validate(request.structured_request)
    patched = StructuredRequestV1.model_validate(
        {
            **structured.model_dump(mode="json"),
            "entities": [
                {
                    "name": "credential",
                    "value": str(uuid.uuid4()),
                    "source": ParameterProvenance.SECRET_REFERENCE.value,
                },
                {
                    "name": "credential",
                    "value": "other",
                    "source": ParameterProvenance.USER_EXPLICIT.value,
                },
            ],
            "missing_inputs": ["credential"],
            "needs_clarification": True,
        }
    )
    request.structured_request = patched.model_dump(mode="json")
    await db_session.commit()

    with pytest.raises(AppError) as exc:
        await ClarificationResponseService(db_session).submit_response(
            agent_request_id=seeded["request_id"],
            clarification_id=seeded["clarification_id"],
            requester_id=seeded["requester_id"],
            body=_submit({"credential": str(uuid.uuid4())}),
        )
    assert exc.value.code == "VALIDATION_ERROR"
    clarification = await ClarificationRequestRepository(db_session).get(
        seeded["clarification_id"]
    )
    assert clarification is not None
    assert clarification.status == ClarificationRequestStatus.OPEN.value


@pytest.mark.asyncio
async def test_ambiguities_remaining_rejected(db_session: AsyncSession) -> None:
    seeded = await _seed_missing_parameter(
        db_session,
        ambiguities=["어느 도시인지 불명확"],
    )
    with pytest.raises(AppError) as exc:
        await ClarificationResponseService(db_session).submit_response(
            agent_request_id=seeded["request_id"],
            clarification_id=seeded["clarification_id"],
            requester_id=seeded["requester_id"],
            body=_submit({"location": "서울"}),
        )
    assert exc.value.code == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_owner_forbidden(db_session: AsyncSession) -> None:
    seeded = await _seed_missing_parameter(db_session)
    with pytest.raises(AppError) as exc:
        await ClarificationResponseService(db_session).submit_response(
            agent_request_id=seeded["request_id"],
            clarification_id=seeded["clarification_id"],
            requester_id=uuid.uuid4(),
            body=_submit({"location": "서울"}),
        )
    assert exc.value.code == "FORBIDDEN"
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_replay_answered_conflict(db_session: AsyncSession) -> None:
    seeded = await _seed_missing_parameter(db_session)
    await ClarificationResponseService(db_session).submit_response(
        agent_request_id=seeded["request_id"],
        clarification_id=seeded["clarification_id"],
        requester_id=seeded["requester_id"],
        body=_submit({"location": "서울"}),
    )
    with pytest.raises(AppError) as exc:
        await ClarificationResponseService(db_session).submit_response(
            agent_request_id=seeded["request_id"],
            clarification_id=seeded["clarification_id"],
            requester_id=seeded["requester_id"],
            body=_submit({"location": "인천"}),
        )
    assert exc.value.code == "RESOURCE_CONFLICT"
    clarification = await ClarificationRequestRepository(db_session).get(
        seeded["clarification_id"]
    )
    assert clarification is not None
    assert clarification.response_payload == {"location": "서울"}


@pytest.mark.asyncio
async def test_expired_clarification_conflict(db_session: AsyncSession) -> None:
    seeded = await _seed_missing_parameter(db_session)
    clarification = await ClarificationRequestRepository(db_session).get(
        seeded["clarification_id"]
    )
    assert clarification is not None
    clarification.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await ClarificationResponseService(db_session).submit_response(
            agent_request_id=seeded["request_id"],
            clarification_id=seeded["clarification_id"],
            requester_id=seeded["requester_id"],
            body=_submit({"location": "서울"}),
        )
    assert exc.value.code == "RESOURCE_CONFLICT"


async def _seed_tool_confirmation(session: AsyncSession) -> dict[str, Any]:
    request_id, _, _ = await _seed_retrieving(session)
    candidates = [
        _candidate(
            _descriptor(retrieval_score=0.95),
            allow_auto_select=True,
            policy_present=False,
        )
    ]
    outcome = await ToolSelectorService(
        session,
        model_provider=_MockLLM(_rerank_payload(candidates)),  # type: ignore[arg-type]
        tool_retrieval=_MockRetrieval(candidates),  # type: ignore[arg-type]
    ).select(agent_request_id=request_id)
    assert outcome.decision == "CONFIRM"
    request = await AgentRequestRepository(session).get(request_id)
    assert request is not None
    clarification = await ClarificationRequestRepository(
        session
    ).get_open_for_agent_request(request_id)
    assert clarification is not None
    assert clarification.request_type == ClarificationRequestType.TOOL_CONFIRMATION.value
    return {
        "request_id": request_id,
        "clarification_id": clarification.id,
        "requester_id": request.requester_id,
    }


@pytest.mark.asyncio
async def test_tool_confirmation_true_building_parameters(
    db_session: AsyncSession,
) -> None:
    seeded = await _seed_tool_confirmation(db_session)
    outcome = await ClarificationResponseService(db_session).submit_response(
        agent_request_id=seeded["request_id"],
        clarification_id=seeded["clarification_id"],
        requester_id=seeded["requester_id"],
        body=_submit({"confirmed": True}),
    )
    assert outcome.agent_request_status == AgentRequestStatus.BUILDING_PARAMETERS
    request = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert request is not None
    assert request.status == AgentRequestStatus.BUILDING_PARAMETERS.value
    assert request.completed_at is None
    run = await ToolSelectionRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run is not None
    assert run.decision == "CONFIRM"


@pytest.mark.asyncio
async def test_tool_confirmation_false_cancelled(db_session: AsyncSession) -> None:
    seeded = await _seed_tool_confirmation(db_session)
    outcome = await ClarificationResponseService(db_session).submit_response(
        agent_request_id=seeded["request_id"],
        clarification_id=seeded["clarification_id"],
        requester_id=seeded["requester_id"],
        body=_submit({"confirmed": False}),
    )
    assert outcome.agent_request_status == AgentRequestStatus.CANCELLED
    request = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert request is not None
    assert request.status == AgentRequestStatus.CANCELLED.value
    assert request.completed_at is not None


async def _seed_plan_confirmation(session: AsyncSession) -> dict[str, Any]:
    seeded = await _seed_validating(session, policy_requires_confirmation=True)
    outcome = await PlanValidatorService(session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "WAITING_CONFIRMATION"
    request = await AgentRequestRepository(session).get(seeded["request_id"])
    assert request is not None
    clarification = await ClarificationRequestRepository(
        session
    ).get_open_for_agent_request(seeded["request_id"])
    assert clarification is not None
    validation = await PlanValidationRepository(session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert validation is not None
    return {
        **seeded,
        "clarification_id": clarification.id,
        "requester_id": request.requester_id,
        "validation_run_id": validation.id,
        "plan_generation_run_id": validation.plan_generation_run_id,
        "plan_hash": validation.plan_hash,
    }


@pytest.mark.asyncio
async def test_plan_confirmation_true_validating(db_session: AsyncSession) -> None:
    seeded = await _seed_plan_confirmation(db_session)
    outcome = await ClarificationResponseService(db_session).submit_response(
        agent_request_id=seeded["request_id"],
        clarification_id=seeded["clarification_id"],
        requester_id=seeded["requester_id"],
        body=_submit({"confirmed": True}),
    )
    assert outcome.agent_request_status == AgentRequestStatus.VALIDATING
    request = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert request is not None
    assert request.status == AgentRequestStatus.VALIDATING.value
    assert request.completed_at is None


@pytest.mark.asyncio
async def test_plan_confirmation_false_cancelled(db_session: AsyncSession) -> None:
    seeded = await _seed_plan_confirmation(db_session)
    outcome = await ClarificationResponseService(db_session).submit_response(
        agent_request_id=seeded["request_id"],
        clarification_id=seeded["clarification_id"],
        requester_id=seeded["requester_id"],
        body=_submit({"confirmed": False}),
    )
    assert outcome.agent_request_status == AgentRequestStatus.CANCELLED
    request = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert request is not None
    assert request.status == AgentRequestStatus.CANCELLED.value
    assert request.completed_at is not None
    assert (
        await PlanValidationRepository(db_session).get_latest_for_agent_request(
            seeded["request_id"]
        )
        is not None
    )
    assert (
        await PlanGenerationRepository(db_session).get_latest_for_agent_request(
            seeded["request_id"]
        )
        is not None
    )


@pytest.mark.asyncio
async def test_plan_confirmation_stale_hash_conflict(
    db_session: AsyncSession,
) -> None:
    seeded = await _seed_plan_confirmation(db_session)
    validation = await PlanValidationRepository(db_session).get_by_id(
        seeded["validation_run_id"]
    )
    assert validation is not None
    validation.plan_hash = "0" * 64
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await ClarificationResponseService(db_session).submit_response(
            agent_request_id=seeded["request_id"],
            clarification_id=seeded["clarification_id"],
            requester_id=seeded["requester_id"],
            body=_submit({"confirmed": True}),
        )
    assert exc.value.code == "RESOURCE_CONFLICT"
    clarification = await ClarificationRequestRepository(db_session).get(
        seeded["clarification_id"]
    )
    assert clarification is not None
    assert clarification.status == ClarificationRequestStatus.OPEN.value
    request = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert request is not None
    assert request.status == AgentRequestStatus.WAITING_CONFIRMATION.value


@pytest.mark.asyncio
async def test_plan_confirmation_snapshot_hash_mismatch(
    db_session: AsyncSession,
) -> None:
    seeded = await _seed_plan_confirmation(db_session)
    plan_run = await PlanGenerationRepository(db_session).get_by_id(
        seeded["plan_generation_run_id"]
    )
    assert plan_run is not None
    mutated = dict(plan_run.plan_snapshot)
    mutated["inputs"] = {"tampered": True}
    plan_run.plan_snapshot = mutated
    # Keep stored plan_hash so recomputed hash diverges.
    assert compute_plan_hash(plan_run.plan_snapshot) != plan_run.plan_hash
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await ClarificationResponseService(db_session).submit_response(
            agent_request_id=seeded["request_id"],
            clarification_id=seeded["clarification_id"],
            requester_id=seeded["requester_id"],
            body=_submit({"confirmed": True}),
        )
    assert exc.value.code == "RESOURCE_CONFLICT"


@pytest.mark.asyncio
async def test_plan_validator_satisfied_confirmation_ready(
    db_session: AsyncSession,
) -> None:
    seeded = await _seed_plan_confirmation(db_session)
    await ClarificationResponseService(db_session).submit_response(
        agent_request_id=seeded["request_id"],
        clarification_id=seeded["clarification_id"],
        requester_id=seeded["requester_id"],
        body=_submit({"confirmed": True}),
    )
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "READY"
    assert outcome.agent_request_status == AgentRequestStatus.READY
    open_row = await ClarificationRequestRepository(
        db_session
    ).get_open_for_agent_request(seeded["request_id"])
    assert open_row is None
    answered = await ClarificationRequestRepository(db_session).get(
        seeded["clarification_id"]
    )
    assert answered is not None
    assert answered.status == ClarificationRequestStatus.ANSWERED.value


@pytest.mark.asyncio
async def test_plan_validator_policy_changed_reconfirm(
    db_session: AsyncSession,
) -> None:
    seeded = await _seed_plan_confirmation(db_session)
    await ClarificationResponseService(db_session).submit_response(
        agent_request_id=seeded["request_id"],
        clarification_id=seeded["clarification_id"],
        requester_id=seeded["requester_id"],
        body=_submit({"confirmed": True}),
    )
    policy = await MCPToolPolicyRepository(db_session).get_by_tool_id(seeded["tool_id"])
    assert policy is not None
    policy.max_attempts = 3
    await db_session.commit()

    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "WAITING_CONFIRMATION"
    open_rows = await ClarificationRequestRepository(
        db_session
    ).get_open_for_agent_request(seeded["request_id"])
    assert open_rows is not None
    assert open_rows.id != seeded["clarification_id"]
    assert open_rows.request_type == ClarificationRequestType.PLAN_CONFIRMATION.value
    old = await ClarificationRequestRepository(db_session).get(seeded["clarification_id"])
    assert old is not None
    assert old.status == ClarificationRequestStatus.ANSWERED.value
    assert old.response_payload == {"confirmed": True}


@pytest.mark.asyncio
async def test_plan_validator_auth_removed_after_confirm_rejected(
    db_session: AsyncSession,
) -> None:
    from app.models.auth import ResourceGrant
    from sqlalchemy import delete

    seeded = await _seed_plan_confirmation(db_session)
    await ClarificationResponseService(db_session).submit_response(
        agent_request_id=seeded["request_id"],
        clarification_id=seeded["clarification_id"],
        requester_id=seeded["requester_id"],
        body=_submit({"confirmed": True}),
    )
    await db_session.execute(
        delete(ResourceGrant).where(ResourceGrant.user_id == seeded["requester_id"])
    )
    await db_session.commit()

    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "REJECTED"
    request = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert request is not None
    assert request.status == AgentRequestStatus.REJECTED.value


@pytest.mark.asyncio
async def test_cancel_race_rolls_back_answer(db_session: AsyncSession) -> None:
    seeded = await _seed_missing_parameter(db_session)
    await AgentRequestService(db_session).compare_and_set_status(
        seeded["request_id"],
        expected_statuses=[AgentRequestStatus.WAITING_INPUT],
        new_status=AgentRequestStatus.CANCELLED,
        set_completed_at=True,
        completed_at=datetime.now(UTC),
    )
    await db_session.commit()

    with pytest.raises(AppError) as exc:
        await ClarificationResponseService(db_session).submit_response(
            agent_request_id=seeded["request_id"],
            clarification_id=seeded["clarification_id"],
            requester_id=seeded["requester_id"],
            body=_submit({"location": "서울"}),
        )
    assert exc.value.code == "RESOURCE_CONFLICT"

    clarification = await ClarificationRequestRepository(db_session).get(
        seeded["clarification_id"]
    )
    assert clarification is not None
    assert clarification.status == ClarificationRequestStatus.OPEN.value
    request = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert request is not None
    assert request.status == AgentRequestStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_smoke_missing_then_selector_building(
    db_session: AsyncSession,
) -> None:
    seeded = await _seed_missing_parameter(db_session)
    await ClarificationResponseService(db_session).submit_response(
        agent_request_id=seeded["request_id"],
        clarification_id=seeded["clarification_id"],
        requester_id=seeded["requester_id"],
        body=_submit({"location": "서울"}),
    )
    request = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert request is not None
    assert request.status == AgentRequestStatus.RETRIEVING.value
    structured = StructuredRequestV1.model_validate(request.structured_request)
    assert any(
        e.source == ParameterProvenance.CONVERSATION_CONFIRMED and e.value == "서울"
        for e in structured.entities
    )

    # Explicit downstream dispatch (not auto-chained by response service).
    await AgentRequestService(db_session).compare_and_set_status(
        seeded["request_id"],
        expected_statuses=[AgentRequestStatus.RETRIEVING],
        new_status=AgentRequestStatus.SELECTING,
    )
    await db_session.commit()
    # Leave as evidence that status resumed to RETRIEVING for selector re-entry.
    assert (
        await AgentRequestRepository(db_session).get(seeded["request_id"])
    ).status == AgentRequestStatus.SELECTING.value  # type: ignore[union-attr]
