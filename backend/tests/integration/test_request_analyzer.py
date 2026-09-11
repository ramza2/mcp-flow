"""PostgreSQL integration tests for Request Analyzer foundation."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from app.agent.request_analyzer import RequestAnalyzerService
from app.core.errors import AppError
from app.domain.enums import (
    AgentRequestStatus,
    ConversationMessageRole,
    ParameterProvenance,
    RiskClass,
)
from app.model_provider.errors import TIMEOUT, ModelProviderError
from app.model_provider.openai_compatible import OPENAI_COMPATIBLE_PROVIDER
from app.repositories.agent import AgentRepository
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.llm_profile import LLMProfileRepository
from app.repositories.user import UserRepository
from app.services.agent_request import AgentRequestService
from app.services.conversation import ConversationService
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _structured(
    *,
    missing_inputs: list[str] | None = None,
    ambiguities: list[str] | None = None,
) -> dict[str, Any]:
    missing = list(missing_inputs or [])
    amb = list(ambiguities or [])
    needs = bool(missing or amb)
    return {
        "schema_version": "9.9",
        "request_text": "MODEL TEXT",
        "intent": "날씨 조회",
        "entities": []
        if needs
        else [
            {
                "name": "location",
                "value": "서울",
                "source": ParameterProvenance.USER_EXPLICIT.value,
            }
        ],
        "constraints": [],
        "expected_outputs": [] if needs else ["날씨"],
        "required_capabilities": [] if needs else ["weather.lookup"],
        "risk_hints": [] if needs else [RiskClass.READ_ONLY.value],
        "missing_inputs": missing,
        "ambiguities": amb,
        "needs_clarification": needs,
    }


class _MockProvider:
    def __init__(
        self,
        payload: dict[str, Any] | Exception,
        *,
        delay: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self.payload = payload
        self.calls = 0
        self.delay = delay
        self.release = release

    async def generate_json(self, target, *, messages, parameters=None):  # noqa: ANN001
        self.calls += 1
        if self.delay is not None:
            self.delay.set()
        if self.release is not None:
            await self.release.wait()
        if isinstance(self.payload, Exception):
            raise self.payload
        return dict(self.payload)

    async def aclose(self) -> None:
        return None


async def _seed_received(
    session: AsyncSession,
    *,
    raw_text: str = "서울 날씨 알려줘",
) -> uuid.UUID:
    user = await UserRepository(session).create(
        username=f"u-{uuid.uuid4().hex[:8]}",
        display_name="Owner",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        status="ACTIVE",
    )
    agent = await AgentRepository(session).create(
        code=f"agt-{uuid.uuid4().hex[:8]}",
        name="PG Analyzer Agent",
        owner_id=user.id,
    )
    profile = await LLMProfileRepository(session).create(
        code=f"llm-{uuid.uuid4().hex[:8]}",
        name="PG Analyzer LLM",
        provider=OPENAI_COMPATIBLE_PROVIDER,
        model="gpt-test",
        base_url="https://llm.test/v1",
    )
    version = await AgentVersionRepository(session).create(
        agent_id=agent.id,
        version_no=1,
        system_instruction="plan safely",
        llm_profile_id=profile.id,
        request_schema_version="1.0",
        plan_schema_version="1.0",
        selection_settings={},
        planning_settings={},
        response_settings={},
        content_hash=uuid.uuid4().hex,
    )
    conv = await ConversationService(session).create_conversation(
        owner_id=user.id, agent_id=agent.id, title="PG"
    )
    message = await ConversationService(session).append_message(
        conversation_id=conv.id,
        owner_id=user.id,
        role=ConversationMessageRole.USER,
        content={"text": raw_text},
        content_text=raw_text,
    )
    request = await AgentRequestService(session).create_received(
        conversation_id=conv.id,
        requester_id=user.id,
        agent_version_id=version.id,
        source_message_id=message.id,
    )
    await session.commit()
    return request.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_success_atomic_retrieving(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        request_id = await _seed_received(session, raw_text="서울 날씨 알려줘")

    provider = _MockProvider(_structured())
    async with integration_session_factory() as session:
        result = await RequestAnalyzerService(
            session, model_provider=provider  # type: ignore[arg-type]
        ).analyze(agent_request_id=request_id)
        assert result.request_text == "서울 날씨 알려줘"

    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(request_id)
        assert row is not None
        assert row.status == AgentRequestStatus.RETRIEVING.value
        assert row.structured_request is not None
        assert row.structured_request["request_text"] == "서울 날씨 알려줘"
        assert row.structured_request_version == "1.0"
        assert row.missing_fields == []
        assert row.analyzed_at is not None
        assert row.completed_at is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_waiting_input_atomic(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        request_id = await _seed_received(session, raw_text="날씨 알려줘")

    provider = _MockProvider(_structured(missing_inputs=["location"]))
    async with integration_session_factory() as session:
        await RequestAnalyzerService(
            session, model_provider=provider  # type: ignore[arg-type]
        ).analyze(agent_request_id=request_id)

    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(request_id)
        assert row is not None
        assert row.status == AgentRequestStatus.WAITING_INPUT.value
        assert row.missing_fields == ["location"]
        assert row.structured_request is not None
        assert row.analyzed_at is not None
        assert row.completed_at is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_cancel_race(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        request_id = await _seed_received(session)

    started = asyncio.Event()
    release = asyncio.Event()
    provider = _MockProvider(_structured(), delay=started, release=release)

    async def analyze() -> Exception | None:
        async with integration_session_factory() as session:
            try:
                await RequestAnalyzerService(
                    session, model_provider=provider  # type: ignore[arg-type]
                ).analyze(agent_request_id=request_id)
                return None
            except Exception as exc:  # noqa: BLE001
                return exc

    task = asyncio.create_task(analyze())
    await started.wait()
    async with integration_session_factory() as session:
        await AgentRequestService(session).compare_and_set_status(
            request_id,
            expected_statuses=[AgentRequestStatus.ANALYZING],
            new_status=AgentRequestStatus.CANCELLED,
            set_completed_at=True,
        )
        await session.commit()
    release.set()
    err = await task
    assert isinstance(err, AppError)
    assert err.code == "RESOURCE_CONFLICT"

    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(request_id)
        assert row is not None
        assert row.status == AgentRequestStatus.CANCELLED.value
        assert row.structured_request is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_concurrent_analyzers(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        request_id = await _seed_received(session)

    provider = _MockProvider(_structured())

    async def run() -> str | Exception:
        async with integration_session_factory() as session:
            try:
                await RequestAnalyzerService(
                    session, model_provider=provider  # type: ignore[arg-type]
                ).analyze(agent_request_id=request_id)
                return "ok"
            except Exception as exc:  # noqa: BLE001
                return exc

    results = await asyncio.gather(run(), run())
    oks = [r for r in results if r == "ok"]
    conflicts = [
        r
        for r in results
        if isinstance(r, AppError) and r.code == "RESOURCE_CONFLICT"
    ]
    assert len(oks) == 1
    assert len(conflicts) == 1
    assert provider.calls == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_provider_timeout_failed(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        request_id = await _seed_received(session)

    provider = _MockProvider(
        ModelProviderError(error_code=TIMEOUT, message="slow", retryable=True)
    )
    async with integration_session_factory() as session:
        with pytest.raises(ModelProviderError) as exc:
            await RequestAnalyzerService(
                session, model_provider=provider  # type: ignore[arg-type]
            ).analyze(agent_request_id=request_id)
        assert exc.value.error_code == TIMEOUT

    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(request_id)
        assert row is not None
        assert row.status == AgentRequestStatus.FAILED.value
        assert row.completed_at is not None
        assert row.structured_request is None
