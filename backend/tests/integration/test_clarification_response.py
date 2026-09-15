"""PostgreSQL integration tests for Clarification / Confirmation resume."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from app.agent.parameter_builder import ParameterBuilderService
from app.agent.plan_validator import PlanValidatorService
from app.core.errors import AppError
from app.domain.enums import (
    AgentRequestStatus,
    ClarificationRequestStatus,
    ClarificationRequestType,
    ParameterProvenance,
)
from app.models.auth import ResourceGrant
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.clarification_request import ClarificationRequestRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.repositories.plan_generation import PlanGenerationRepository
from app.repositories.plan_validation import PlanValidationRepository
from app.repositories.tool_selection import ToolSelectionRepository
from app.schemas.clarification import (
    CONFIRMATION_QUESTION_SCHEMA,
    TOOL_CONFIRMATION_PROMPT_TEXT,
    ClarificationResponseSubmit,
)
from app.schemas.structured_request import StructuredRequestV1
from app.services.agent_request import AgentRequestService
from app.services.clarification_response import ClarificationResponseService
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.test_parameter_builder import _seed_building
from tests.integration.test_plan_validator import _seed_validating


def _submit(payload: dict[str, Any]) -> ClarificationResponseSubmit:
    return ClarificationResponseSubmit(response_payload=payload)


async def _seed_missing(
    session: AsyncSession,
) -> dict[str, Any]:
    seeded = await _seed_building(
        session,
        required=["location"],
        entities=[],
    )
    request = await AgentRequestRepository(session).get(seeded["request_id"])
    assert request is not None
    structured = StructuredRequestV1.model_validate(request.structured_request)
    patched = StructuredRequestV1.model_validate(
        {
            **structured.model_dump(mode="json"),
            "missing_inputs": ["location"],
            "ambiguities": [],
            "needs_clarification": True,
        }
    )
    await AgentRequestService(session).compare_and_set_status(
        request.id,
        expected_statuses=[AgentRequestStatus.BUILDING_PARAMETERS],
        new_status=AgentRequestStatus.BUILDING_PARAMETERS,
        extra_values={
            "structured_request": patched.model_dump(mode="json"),
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


async def _seed_tool_confirmation_waiting(
    session: AsyncSession,
) -> dict[str, Any]:
    """Durable TOOL_CONFIRMATION without invoking ToolSelector LLM path."""
    seeded = await _seed_building(session)
    request = await AgentRequestRepository(session).get(seeded["request_id"])
    assert request is not None
    run = await ToolSelectionRepository(session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run is not None
    run.decision = "CONFIRM"
    clarification = await ClarificationRequestRepository(session).create_open(
        agent_request_id=seeded["request_id"],
        request_type=ClarificationRequestType.TOOL_CONFIRMATION.value,
        question_schema=CONFIRMATION_QUESTION_SCHEMA,
        prompt_text=TOOL_CONFIRMATION_PROMPT_TEXT,
        expires_at=None,
    )
    await AgentRequestService(session).compare_and_set_status(
        seeded["request_id"],
        expected_statuses=[AgentRequestStatus.BUILDING_PARAMETERS],
        new_status=AgentRequestStatus.WAITING_CONFIRMATION,
    )
    await session.commit()
    return {
        **seeded,
        "clarification_id": clarification.id,
        "requester_id": request.requester_id,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_missing_parameter_response(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_missing(session)

    async with integration_session_factory() as session:
        outcome = await ClarificationResponseService(session).submit_response(
            agent_request_id=seeded["request_id"],
            clarification_id=seeded["clarification_id"],
            requester_id=seeded["requester_id"],
            body=_submit({"location": "서울"}),
        )
        assert outcome.agent_request_status == AgentRequestStatus.RETRIEVING

    async with integration_session_factory() as session:
        clarification = await ClarificationRequestRepository(session).get(
            seeded["clarification_id"]
        )
        assert clarification is not None
        assert clarification.status == ClarificationRequestStatus.ANSWERED.value
        assert clarification.answered_by == seeded["requester_id"]
        assert clarification.response_payload == {"location": "서울"}
        request = await AgentRequestRepository(session).get(seeded["request_id"])
        assert request is not None
        assert request.status == AgentRequestStatus.RETRIEVING.value
        structured = StructuredRequestV1.model_validate(request.structured_request)
        entity = next(e for e in structured.entities if e.name == "location")
        assert entity.value == "서울"
        assert entity.source == ParameterProvenance.CONVERSATION_CONFIRMED


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_tool_confirmation_true_false(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        true_seed = await _seed_tool_confirmation_waiting(session)

    async with integration_session_factory() as session:
        outcome = await ClarificationResponseService(session).submit_response(
            agent_request_id=true_seed["request_id"],
            clarification_id=true_seed["clarification_id"],
            requester_id=true_seed["requester_id"],
            body=_submit({"confirmed": True}),
        )
        assert outcome.agent_request_status == AgentRequestStatus.BUILDING_PARAMETERS

    async with integration_session_factory() as session:
        request = await AgentRequestRepository(session).get(true_seed["request_id"])
        assert request is not None
        assert request.status == AgentRequestStatus.BUILDING_PARAMETERS.value
        assert request.completed_at is None
        run = await ToolSelectionRepository(session).get_latest_for_agent_request(
            true_seed["request_id"]
        )
        assert run is not None
        assert run.decision == "CONFIRM"

    async with integration_session_factory() as session:
        false_seed = await _seed_tool_confirmation_waiting(session)

    async with integration_session_factory() as session:
        outcome = await ClarificationResponseService(session).submit_response(
            agent_request_id=false_seed["request_id"],
            clarification_id=false_seed["clarification_id"],
            requester_id=false_seed["requester_id"],
            body=_submit({"confirmed": False}),
        )
        assert outcome.agent_request_status == AgentRequestStatus.CANCELLED

    async with integration_session_factory() as session:
        request = await AgentRequestRepository(session).get(false_seed["request_id"])
        assert request is not None
        assert request.status == AgentRequestStatus.CANCELLED.value
        assert request.completed_at is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_plan_confirmation_true_ready(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
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
        seeded = {
            **seeded,
            "clarification_id": clarification.id,
            "requester_id": request.requester_id,
        }

    async with integration_session_factory() as session:
        outcome = await ClarificationResponseService(session).submit_response(
            agent_request_id=seeded["request_id"],
            clarification_id=seeded["clarification_id"],
            requester_id=seeded["requester_id"],
            body=_submit({"confirmed": True}),
        )
        assert outcome.agent_request_status == AgentRequestStatus.VALIDATING

    async with integration_session_factory() as session:
        outcome = await PlanValidatorService(session).validate(
            agent_request_id=seeded["request_id"]
        )
        assert outcome.decision == "READY"

    async with integration_session_factory() as session:
        request = await AgentRequestRepository(session).get(seeded["request_id"])
        assert request is not None
        assert request.status == AgentRequestStatus.READY.value
        open_row = await ClarificationRequestRepository(
            session
        ).get_open_for_agent_request(seeded["request_id"])
        assert open_row is None
        answered = await ClarificationRequestRepository(session).get(
            seeded["clarification_id"]
        )
        assert answered is not None
        assert answered.status == ClarificationRequestStatus.ANSWERED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_plan_confirmation_false_cancelled(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_validating(session, policy_requires_confirmation=True)
        await PlanValidatorService(session).validate(
            agent_request_id=seeded["request_id"]
        )
        request = await AgentRequestRepository(session).get(seeded["request_id"])
        assert request is not None
        clarification = await ClarificationRequestRepository(
            session
        ).get_open_for_agent_request(seeded["request_id"])
        assert clarification is not None
        seeded = {
            **seeded,
            "clarification_id": clarification.id,
            "requester_id": request.requester_id,
        }

    async with integration_session_factory() as session:
        outcome = await ClarificationResponseService(session).submit_response(
            agent_request_id=seeded["request_id"],
            clarification_id=seeded["clarification_id"],
            requester_id=seeded["requester_id"],
            body=_submit({"confirmed": False}),
        )
        assert outcome.agent_request_status == AgentRequestStatus.CANCELLED

    async with integration_session_factory() as session:
        request = await AgentRequestRepository(session).get(seeded["request_id"])
        assert request is not None
        assert request.status == AgentRequestStatus.CANCELLED.value
        assert (
            await PlanValidationRepository(session).get_latest_for_agent_request(
                seeded["request_id"]
            )
            is not None
        )
        assert (
            await PlanGenerationRepository(session).get_latest_for_agent_request(
                seeded["request_id"]
            )
            is not None
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_policy_reconfirm(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_validating(session, policy_requires_confirmation=True)
        await PlanValidatorService(session).validate(
            agent_request_id=seeded["request_id"]
        )
        request = await AgentRequestRepository(session).get(seeded["request_id"])
        assert request is not None
        clarification = await ClarificationRequestRepository(
            session
        ).get_open_for_agent_request(seeded["request_id"])
        assert clarification is not None
        seeded = {
            **seeded,
            "clarification_id": clarification.id,
            "requester_id": request.requester_id,
        }

    async with integration_session_factory() as session:
        await ClarificationResponseService(session).submit_response(
            agent_request_id=seeded["request_id"],
            clarification_id=seeded["clarification_id"],
            requester_id=seeded["requester_id"],
            body=_submit({"confirmed": True}),
        )

    async with integration_session_factory() as session:
        policy = await MCPToolPolicyRepository(session).get_by_tool_id(
            seeded["tool_id"]
        )
        assert policy is not None
        policy.max_attempts = 5
        await session.commit()

    async with integration_session_factory() as session:
        outcome = await PlanValidatorService(session).validate(
            agent_request_id=seeded["request_id"]
        )
        assert outcome.decision == "WAITING_CONFIRMATION"
        open_row = await ClarificationRequestRepository(
            session
        ).get_open_for_agent_request(seeded["request_id"])
        assert open_row is not None
        assert open_row.id != seeded["clarification_id"]
        old = await ClarificationRequestRepository(session).get(
            seeded["clarification_id"]
        )
        assert old is not None
        assert old.status == ClarificationRequestStatus.ANSWERED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_auth_removed_after_confirm_rejected(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_validating(session, policy_requires_confirmation=True)
        await PlanValidatorService(session).validate(
            agent_request_id=seeded["request_id"]
        )
        request = await AgentRequestRepository(session).get(seeded["request_id"])
        assert request is not None
        clarification = await ClarificationRequestRepository(
            session
        ).get_open_for_agent_request(seeded["request_id"])
        assert clarification is not None
        seeded = {
            **seeded,
            "clarification_id": clarification.id,
            "requester_id": request.requester_id,
        }

    async with integration_session_factory() as session:
        await ClarificationResponseService(session).submit_response(
            agent_request_id=seeded["request_id"],
            clarification_id=seeded["clarification_id"],
            requester_id=seeded["requester_id"],
            body=_submit({"confirmed": True}),
        )

    async with integration_session_factory() as session:
        await session.execute(
            delete(ResourceGrant).where(
                ResourceGrant.user_id == seeded["requester_id"]
            )
        )
        await session.commit()

    async with integration_session_factory() as session:
        outcome = await PlanValidatorService(session).validate(
            agent_request_id=seeded["request_id"]
        )
        assert outcome.decision == "REJECTED"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_double_response(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_missing(session)

    async def _run() -> Any:
        async with integration_session_factory() as session:
            try:
                return await ClarificationResponseService(session).submit_response(
                    agent_request_id=seeded["request_id"],
                    clarification_id=seeded["clarification_id"],
                    requester_id=seeded["requester_id"],
                    body=_submit({"location": "서울"}),
                )
            except AppError as exc:
                return exc

    results = await asyncio.gather(_run(), _run())
    successes = [r for r in results if not isinstance(r, AppError)]
    conflicts = [
        r for r in results if isinstance(r, AppError) and r.code == "RESOURCE_CONFLICT"
    ]
    assert len(successes) == 1
    assert len(conflicts) == 1

    async with integration_session_factory() as session:
        clarification = await ClarificationRequestRepository(session).get(
            seeded["clarification_id"]
        )
        assert clarification is not None
        assert clarification.status == ClarificationRequestStatus.ANSWERED.value
        request = await AgentRequestRepository(session).get(seeded["request_id"])
        assert request is not None
        assert request.status == AgentRequestStatus.RETRIEVING.value
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM clarification_requests "
                    "WHERE agent_request_id = :id AND status = 'ANSWERED'"
                ),
                {"id": seeded["request_id"]},
            )
        ).scalar_one()
        assert int(count) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_cancel_race(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_missing(session)

    async with integration_session_factory() as session:
        await AgentRequestService(session).compare_and_set_status(
            seeded["request_id"],
            expected_statuses=[AgentRequestStatus.WAITING_INPUT],
            new_status=AgentRequestStatus.CANCELLED,
            set_completed_at=True,
            completed_at=datetime.now(UTC),
        )
        await session.commit()

    async with integration_session_factory() as session:
        with pytest.raises(AppError) as exc:
            await ClarificationResponseService(session).submit_response(
                agent_request_id=seeded["request_id"],
                clarification_id=seeded["clarification_id"],
                requester_id=seeded["requester_id"],
                body=_submit({"location": "서울"}),
            )
        assert exc.value.code == "RESOURCE_CONFLICT"

    async with integration_session_factory() as session:
        clarification = await ClarificationRequestRepository(session).get(
            seeded["clarification_id"]
        )
        assert clarification is not None
        assert clarification.status == ClarificationRequestStatus.OPEN.value
        request = await AgentRequestRepository(session).get(seeded["request_id"])
        assert request is not None
        assert request.status == AgentRequestStatus.CANCELLED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_restart_recovery(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        missing = await _seed_missing(session)
        await ClarificationResponseService(session).submit_response(
            agent_request_id=missing["request_id"],
            clarification_id=missing["clarification_id"],
            requester_id=missing["requester_id"],
            body=_submit({"location": "서울"}),
        )

    async with integration_session_factory() as session:
        request = await AgentRequestRepository(session).get(missing["request_id"])
        assert request is not None
        assert request.status == AgentRequestStatus.RETRIEVING.value
        clarification = await ClarificationRequestRepository(session).get(
            missing["clarification_id"]
        )
        assert clarification is not None
        assert clarification.status == ClarificationRequestStatus.ANSWERED.value
        structured = StructuredRequestV1.model_validate(request.structured_request)
        assert any(
            e.name == "location"
            and e.source == ParameterProvenance.CONVERSATION_CONFIRMED
            for e in structured.entities
        )

    async with integration_session_factory() as session:
        seeded = await _seed_validating(session, policy_requires_confirmation=True)
        await PlanValidatorService(session).validate(
            agent_request_id=seeded["request_id"]
        )
        request = await AgentRequestRepository(session).get(seeded["request_id"])
        assert request is not None
        clarification = await ClarificationRequestRepository(
            session
        ).get_open_for_agent_request(seeded["request_id"])
        assert clarification is not None
        plan_seed = {
            **seeded,
            "clarification_id": clarification.id,
            "requester_id": request.requester_id,
        }
        await ClarificationResponseService(session).submit_response(
            agent_request_id=plan_seed["request_id"],
            clarification_id=plan_seed["clarification_id"],
            requester_id=plan_seed["requester_id"],
            body=_submit({"confirmed": True}),
        )

    async with integration_session_factory() as session:
        request = await AgentRequestRepository(session).get(plan_seed["request_id"])
        assert request is not None
        assert request.status == AgentRequestStatus.VALIDATING.value
        prev = await PlanValidationRepository(session).get_latest_for_agent_request(
            plan_seed["request_id"]
        )
        assert prev is not None
        assert prev.decision == "WAITING_CONFIRMATION"
        answered = await ClarificationRequestRepository(session).get(
            plan_seed["clarification_id"]
        )
        assert answered is not None
        assert answered.status == ClarificationRequestStatus.ANSWERED.value
        assert answered.response_payload == {"confirmed": True}

        outcome = await PlanValidatorService(session).validate(
            agent_request_id=plan_seed["request_id"]
        )
        assert outcome.decision == "READY"


async def _seed_missing_credential(
    session: AsyncSession,
) -> dict[str, Any]:
    seeded = await _seed_building(
        session,
        required=["credential"],
        entities=[],
    )
    request = await AgentRequestRepository(session).get(seeded["request_id"])
    assert request is not None
    structured = StructuredRequestV1.model_validate(request.structured_request)
    patched = StructuredRequestV1.model_validate(
        {
            **structured.model_dump(mode="json"),
            "missing_inputs": ["credential"],
            "ambiguities": [],
            "needs_clarification": True,
        }
    )
    await AgentRequestService(session).compare_and_set_status(
        request.id,
        expected_statuses=[AgentRequestStatus.BUILDING_PARAMETERS],
        new_status=AgentRequestStatus.BUILDING_PARAMETERS,
        extra_values={
            "structured_request": patched.model_dump(mode="json"),
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


async def _inject_duplicate_secrets(
    session: AsyncSession, *, request_id: UUID
) -> dict[str, Any]:
    request = await AgentRequestRepository(session).get(request_id)
    assert request is not None
    structured = StructuredRequestV1.model_validate(request.structured_request)
    uuid_a = str(uuid.uuid4())
    uuid_b = str(uuid.uuid4())
    patched = StructuredRequestV1.model_validate(
        {
            **structured.model_dump(mode="json"),
            "entities": [
                {
                    "name": "credential",
                    "value": uuid_a,
                    "source": ParameterProvenance.SECRET_REFERENCE.value,
                },
                {
                    "name": "CREDENTIAL",
                    "value": uuid_b,
                    "source": ParameterProvenance.SECRET_REFERENCE.value,
                },
            ],
            "missing_inputs": ["credential"],
            "needs_clarification": True,
        }
    )
    request.structured_request = patched.model_dump(mode="json")
    await session.commit()
    return {
        "uuid_a": uuid_a,
        "uuid_b": uuid_b,
        "structured": patched.model_dump(mode="json"),
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_duplicate_secret_valid_uuid(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    new_secret = uuid.uuid4()
    async with integration_session_factory() as session:
        seeded = await _seed_missing_credential(session)
        await _inject_duplicate_secrets(session, request_id=seeded["request_id"])

    async with integration_session_factory() as session:
        outcome = await ClarificationResponseService(session).submit_response(
            agent_request_id=seeded["request_id"],
            clarification_id=seeded["clarification_id"],
            requester_id=seeded["requester_id"],
            body=_submit({"credential": str(new_secret)}),
        )
        assert outcome.agent_request_status == AgentRequestStatus.RETRIEVING

    async with integration_session_factory() as session:
        clarification = await ClarificationRequestRepository(session).get(
            seeded["clarification_id"]
        )
        assert clarification is not None
        assert clarification.status == ClarificationRequestStatus.ANSWERED.value
        request = await AgentRequestRepository(session).get(seeded["request_id"])
        assert request is not None
        assert request.status == AgentRequestStatus.RETRIEVING.value
        structured = StructuredRequestV1.model_validate(request.structured_request)
        matching = [
            e
            for e in structured.entities
            if e.name.strip().casefold() == "credential"
        ]
        assert len(matching) == 1
        assert matching[0].name == "credential"
        assert matching[0].source == ParameterProvenance.SECRET_REFERENCE
        assert matching[0].value == str(new_secret)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_duplicate_secret_plaintext_not_persisted(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_missing_credential(session)
        injected = await _inject_duplicate_secrets(
            session, request_id=seeded["request_id"]
        )

    async with integration_session_factory() as session:
        with pytest.raises(AppError) as exc:
            await ClarificationResponseService(session).submit_response(
                agent_request_id=seeded["request_id"],
                clarification_id=seeded["clarification_id"],
                requester_id=seeded["requester_id"],
                body=_submit({"credential": "plain-password"}),
            )
        assert exc.value.code == "VALIDATION_ERROR"
        assert exc.value.status_code == 400

    async with integration_session_factory() as session:
        clarification = await ClarificationRequestRepository(session).get(
            seeded["clarification_id"]
        )
        assert clarification is not None
        assert clarification.status == ClarificationRequestStatus.OPEN.value
        assert clarification.response_payload is None
        request = await AgentRequestRepository(session).get(seeded["request_id"])
        assert request is not None
        assert request.status == AgentRequestStatus.WAITING_INPUT.value
        assert request.structured_request == injected["structured"]
        assert "plain-password" not in str(request.structured_request)
        assert "plain-password" not in str(clarification.response_payload)