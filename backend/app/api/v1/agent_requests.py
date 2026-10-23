"""AgentRequest Clarification + Execution create API (docs/06 §11)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Header, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.agent.request_analyzer import RequestAnalyzerService
from app.api.dependencies import CurrentPrincipalDep, DbSessionDep
from app.core.errors import AppError
from app.model_provider.errors import ModelProviderError
from app.models.agent import Agent, AgentVersion
from app.models.conversation import AgentRequest
from app.repositories.agent import AgentRepository
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.conversation import ConversationRepository
from app.schemas.clarification import (
    ClarificationResponseResult,
    ClarificationResponseSubmit,
)
from app.schemas.execution import AgentRequestExecutionCreateResult
from app.services.agent_request import AgentRequestService
from app.services.clarification_response import (
    ClarificationResponseService,
    to_api_result,
)
from app.services.conversation import ConversationService
from app.services.execution_creation import ExecutionCreationService

router = APIRouter(prefix="/agent-requests", tags=["agent-requests"])


class RunnableAgentView(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    agent_version_id: uuid.UUID
    version_no: int


class RunnableAgentsView(BaseModel):
    items: list[RunnableAgentView]


@router.get("/available-agents", response_model=RunnableAgentsView)
async def available_agents(
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> RunnableAgentsView:
    # Do not expose DRAFT, unversioned, or other owners' PRIVATE/RESTRICTED agents.
    rows = (await session.execute(
        select(Agent, AgentVersion)
        .join(AgentVersion, Agent.current_version_id == AgentVersion.id)
        .where(
            Agent.deleted_at.is_(None),
            Agent.status == "ACTIVE",
            AgentVersion.status == "PUBLISHED",
            AgentVersion.request_schema_version == "1.0",
        )
        .order_by(Agent.name, Agent.id)
    )).all()
    items = [
        RunnableAgentView(
            id=agent.id, name=agent.name, description=agent.description,
            agent_version_id=version.id, version_no=version.version_no,
        )
        for agent, version in rows
        if version.agent_id == agent.id
        and (agent.visibility == "INTERNAL" or agent.owner_id == principal.user_id)
    ]
    return RunnableAgentsView(items=items)


class AgentRunSubmit(BaseModel):
    """Phase 1: persist a real user request; execution is a separate action."""

    model_config = ConfigDict(extra="forbid")

    agent_id: uuid.UUID
    request_text: str = Field(min_length=1, max_length=4000)


class AgentRunRequestView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    conversation_id: uuid.UUID
    agent_version_id: uuid.UUID
    status: str
    raw_request_text: str
    structured_request: dict[str, Any] | None
    rejection_code: str | None
    created_at: datetime
    analyzed_at: datetime | None


def _view(row: Any) -> AgentRunRequestView:
    return AgentRunRequestView.model_validate({
        "id": row.id,
        "conversation_id": row.conversation_id,
        "agent_version_id": row.agent_version_id,
        "status": row.status,
        "raw_request_text": row.raw_request_text,
        "structured_request": row.structured_request,
        "rejection_code": row.rejection_code,
        "created_at": row.created_at,
        "analyzed_at": row.analyzed_at,
    })


async def _owned_request(session: DbSessionDep, request_id: uuid.UUID, user_id: uuid.UUID):
    request = await AgentRequestRepository(session).get(request_id)
    if request is None or request.requester_id != user_id:
        # Owner-only even for a guessed UUID: no cross-user request details.
        raise AppError(code="NOT_FOUND", message="요청을 찾을 수 없습니다.", status_code=404)
    return request


@router.post("", response_model=AgentRunRequestView, status_code=status.HTTP_201_CREATED)
async def submit_agent_run(
    body: AgentRunSubmit,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> AgentRunRequestView:
    """Atomically create Conversation → USER message → RECEIVED request.

    Does NOT call models, select Tools, create Executions, or auto-trigger writes.
    """
    text = body.request_text.strip()
    if not text:
        raise AppError(code="VALIDATION_ERROR", message="지시문을 입력해 주세요.", status_code=422)
    agents = AgentRepository(session)
    agent = await agents.get(body.agent_id)
    if agent is None or agent.status != "ACTIVE" or agent.current_version_id is None:
        raise AppError(code="NOT_FOUND", message="사용 가능한 Agent가 아닙니다.", status_code=404)
    if agent.visibility != "INTERNAL" and agent.owner_id != principal.user_id:
        raise AppError(code="NOT_FOUND", message="사용 가능한 Agent가 아닙니다.", status_code=404)
    version = await AgentVersionRepository(session).get(agent.current_version_id)
    if (
        version is None
        or version.agent_id != agent.id
        or version.status != "PUBLISHED"
        or version.request_schema_version != "1.0"
    ):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="게시된 Agent 버전이 유효하지 않습니다.",
            status_code=409,
        )

    conversations = ConversationService(session)
    conversation = await conversations.create_conversation(
        owner_id=principal.user_id,
        agent_id=agent.id,
        title=text[:255],
    )
    message = await conversations.append_message(
        conversation_id=conversation.id,
        owner_id=principal.user_id,
        role="USER",
        content={"text": text},
        content_text=text,
    )
    request = await AgentRequestService(session).create_received(
        conversation_id=conversation.id,
        requester_id=principal.user_id,
        agent_version_id=version.id,
        source_message_id=message.id,
    )
    await session.commit()
    return _view(request)


@router.get("/conversations/{conversation_id}/latest", response_model=AgentRunRequestView)
async def latest_agent_run(
    conversation_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> AgentRunRequestView:
    conversation = await ConversationRepository(session).get(conversation_id)
    if conversation is None or conversation.owner_id != principal.user_id:
        raise AppError(code="NOT_FOUND", message="대화를 찾을 수 없습니다.", status_code=404)
    row = (await session.execute(
        select(AgentRequest)
        .where(AgentRequest.conversation_id == conversation_id)
        .order_by(AgentRequest.created_at.desc(), AgentRequest.id.desc())
        .limit(1)
    )).scalar_one_or_none()
    if row is None:
        raise AppError(code="NOT_FOUND", message="대화 내 요청이 없습니다.", status_code=404)
    return _view(row)


@router.get("/{request_id}", response_model=AgentRunRequestView)
async def get_agent_run(
    request_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> AgentRunRequestView:
    return _view(await _owned_request(session, request_id, principal.user_id))


@router.post("/{request_id}/analyze", response_model=AgentRunRequestView)
async def analyze_agent_run(
    request_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> AgentRunRequestView:
    """Explicit single-shot Analyzer invocation — no MCP selection/execution.

    The Analyzer persists ANALYZING before its outbound request, and persists
    RETRIEVING / WAITING_INPUT or FAILED. No in-request automatic retry.
    """
    request = await _owned_request(session, request_id, principal.user_id)
    if request.status != "RECEIVED":
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="이미 분석을 시작한 요청입니다.",
            status_code=409,
        )
    try:
        await RequestAnalyzerService(session).analyze(agent_request_id=request_id)
    except ModelProviderError as exc:
        # Never return raw remote errors or provider secrets.
        raise AppError(
            code="MODEL_PROVIDER_UNAVAILABLE",
            message="모델 분석에 실패했습니다. 요청 상태를 새로고침해 확인해 주세요.",
            status_code=502,
            retryable=exc.retryable,
        ) from exc
    await session.refresh(request)
    return _view(request)



@router.post(
    "/{request_id}/clarifications/{clarification_id}/responses",
    response_model=ClarificationResponseResult,
)
async def submit_clarification_response(
    request_id: uuid.UUID,
    clarification_id: uuid.UUID,
    body: ClarificationResponseSubmit,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> ClarificationResponseResult:
    outcome = await ClarificationResponseService(session).submit_response(
        agent_request_id=request_id,
        clarification_id=clarification_id,
        requester_id=principal.user_id,
        body=body,
    )
    return to_api_result(outcome)


@router.post(
    "/{request_id}/executions",
    response_model=AgentRequestExecutionCreateResult,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_request_execution(
    request_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> AgentRequestExecutionCreateResult:
    if idempotency_key is None or not idempotency_key.strip():
        raise AppError(
            code="VALIDATION_ERROR",
            message="Idempotency-Key header is required.",
            status_code=400,
        )
    key = idempotency_key.strip()
    if len(key) > 128:
        raise AppError(
            code="VALIDATION_ERROR",
            message="Idempotency-Key must be at most 128 characters.",
            status_code=400,
        )
    outcome = await ExecutionCreationService(session).create_from_agent_request(
        agent_request_id=request_id,
        requester_id=principal.user_id,
        idempotency_key=key,
    )
    response.status_code = outcome.http_status
    return outcome.result
