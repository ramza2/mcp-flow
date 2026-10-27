"""AgentRequest Clarification + Execution create API (docs/06 §11)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Header, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.agent.parameter_builder import ParameterBuilderService
from app.agent.plan_generator import PlanGeneratorService
from app.agent.plan_validator import PlanValidatorService
from app.agent.request_analyzer import RequestAnalyzerService
from app.agent.tool_selector import ToolSelectorService
from app.api.dependencies import CurrentPrincipalDep, DbSessionDep
from app.core.errors import AppError
from app.model_provider.errors import ModelProviderError
from app.models.agent import Agent, AgentVersion
from app.models.conversation import AgentRequest
from app.models.mcp import MCPTool, MCPToolVersion
from app.repositories.agent import AgentRepository
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.clarification_request import ClarificationRequestRepository
from app.repositories.conversation import ConversationRepository
from app.repositories.parameter_build import ParameterBuildRepository
from app.repositories.plan_generation import PlanGenerationRepository
from app.repositories.plan_validation import PlanValidationRepository
from app.repositories.tool_selection import ToolSelectionRepository
from app.schemas.clarification import (
    ClarificationResponseResult,
    ClarificationResponseSubmit,
)
from app.schemas.execution import AgentRequestExecutionCreateResult
from app.schemas.tool_selection import SelectionDecision
from app.services.agent_request import AgentRequestService
from app.services.agent_run_chat_result import AgentRunChatResultService
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


class AgentRunSelectionView(BaseModel):
    id: uuid.UUID
    decision: SelectionDecision
    selected_tool_version_id: uuid.UUID | None
    selected_tool_name: str | None
    confidence: float | None
    reason_summary: str | None
    created_at: datetime


class AgentRunSelectionResult(BaseModel):
    request: AgentRunRequestView
    selection: AgentRunSelectionView | None


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



async def _selection_view(
    session: DbSessionDep, row: Any
) -> AgentRunSelectionView:
    tool_name: str | None = None
    if row.selected_tool_version_id is not None:
        name_row = (await session.execute(
            select(MCPTool.display_name, MCPTool.remote_name)
            .join(MCPToolVersion, MCPToolVersion.mcp_tool_id == MCPTool.id)
            .where(MCPToolVersion.id == row.selected_tool_version_id)
        )).first()
        if name_row is not None:
            tool_name = name_row[0] or name_row[1]
    return AgentRunSelectionView(
        id=row.id,
        decision=row.decision,
        selected_tool_version_id=row.selected_tool_version_id,
        selected_tool_name=tool_name,
        confidence=row.confidence,
        reason_summary=row.reason_summary,
        created_at=row.created_at,
    )


async def _selection_result(
    session: DbSessionDep, request: AgentRequest
) -> AgentRunSelectionResult:
    run = await ToolSelectionRepository(session).get_latest_for_agent_request(
        request.id
    )
    return AgentRunSelectionResult(
        request=_view(request),
        selection=await _selection_view(session, run) if run is not None else None,
    )


class AgentRunParameterView(BaseModel):
    id: uuid.UUID
    tool_version_id: uuid.UUID
    is_complete: bool
    missing_fields: list[str]
    created_at: datetime


class AgentRunPlanView(BaseModel):
    id: uuid.UUID
    plan_hash: str
    goal: str | None
    step_names: list[str]
    step_count: int
    created_at: datetime


class AgentRunValidationView(BaseModel):
    id: uuid.UUID
    decision: str
    confirmation_required: bool
    error_count: int
    warning_count: int
    created_at: datetime


class AgentRunQuestionFieldView(BaseModel):
    name: str
    value_type: str
    description: str | None
    required: bool


class AgentRunClarificationView(BaseModel):
    id: uuid.UUID
    request_type: str
    prompt_text: str
    fields: list[AgentRunQuestionFieldView]
    requested_at: datetime


class AgentRunProgressResult(BaseModel):
    request: AgentRunRequestView
    parameter_build: AgentRunParameterView | None
    plan: AgentRunPlanView | None
    validation: AgentRunValidationView | None
    open_clarification: AgentRunClarificationView | None
    # Server-computed: READY + single TOOL + READ_ONLY + no confirmation/approval.
    # Clients must not invent eligibility; never exposes policy_snapshot itself.
    safe_auto_execute: bool = False


class AgentRunChatDisplayItemView(BaseModel):
    name: str
    status: str


class AgentRunChatResultView(BaseModel):
    agent_request_id: uuid.UUID
    execution_id: uuid.UUID | None
    execution_status: str | None
    available: bool
    unavailable_reason: str | None
    tool_name: str | None
    display_items: list[AgentRunChatDisplayItemView]
    summary_text: str | None
    truncated: bool


def _safe_auto_execute(
    request: AgentRequest,
    plan_view: AgentRunPlanView | None,
    validation: Any | None,
) -> bool:
    """True only for the phase-1 chat safe path (single READ_ONLY Tool)."""
    if request.status != "READY":
        return False
    if validation is None or bool(validation.confirmation_required):
        return False
    if plan_view is None or plan_view.step_count != 1:
        return False
    snapshot = validation.policy_snapshot
    if not isinstance(snapshot, dict):
        return False
    tool_policy = snapshot.get("tool_policy")
    if not isinstance(tool_policy, dict):
        return False
    if tool_policy.get("risk_class") != "READ_ONLY":
        return False
    if tool_policy.get("requires_approval") is True:
        return False
    if tool_policy.get("requires_confirmation") is True:
        return False
    return True


def _question_fields(question_schema: Any) -> list[AgentRunQuestionFieldView]:
    """Allowlisted question-field projection; never return raw schema/secret values."""
    if not isinstance(question_schema, dict):
        return []
    properties = question_schema.get("properties")
    if not isinstance(properties, dict):
        return []
    required = question_schema.get("required")
    required_names = required if isinstance(required, list) else []
    fields: list[AgentRunQuestionFieldView] = []
    for name, schema in list(properties.items())[:50]:
        if not isinstance(name, str) or not isinstance(schema, dict):
            continue
        value_type = schema.get("type")
        if value_type not in {"string", "number", "integer", "boolean", "object", "array"}:
            value_type = "string"
        description = schema.get("description")
        fields.append(AgentRunQuestionFieldView(
            name=name,
            value_type=value_type,
            description=description[:250] if isinstance(description, str) else None,
            required=name in required_names,
        ))
    return fields


async def _progress_result(
    session: DbSessionDep, request: AgentRequest
) -> AgentRunProgressResult:
    parameter = await ParameterBuildRepository(session).get_latest_for_agent_request(
        request.id
    )
    plan = await PlanGenerationRepository(session).get_latest_for_agent_request(
        request.id
    )
    validation = await PlanValidationRepository(session).get_latest_for_agent_request(
        request.id
    )
    clarification = await ClarificationRequestRepository(session).get_open_for_agent_request(
        request.id
    )
    plan_view: AgentRunPlanView | None = None
    if plan is not None:
        snapshot = plan.plan_snapshot if isinstance(plan.plan_snapshot, dict) else {}
        steps = snapshot.get("steps")
        step_items = steps if isinstance(steps, list) else []
        names = [
            step["name"][:200]
            for step in step_items[:50]
            if isinstance(step, dict) and isinstance(step.get("name"), str)
        ]
        goal = snapshot.get("goal")
        plan_view = AgentRunPlanView(
            id=plan.id,
            plan_hash=plan.plan_hash,
            goal=goal[:500] if isinstance(goal, str) else None,
            step_names=names,
            step_count=len(step_items),
            created_at=plan.created_at,
        )
    validation_view = (
        AgentRunValidationView(
            id=validation.id,
            decision=validation.decision,
            confirmation_required=validation.confirmation_required,
            error_count=len(validation.errors or []),
            warning_count=len(validation.warnings or []),
            created_at=validation.created_at,
        )
        if validation is not None
        else None
    )
    return AgentRunProgressResult(
        request=_view(request),
        parameter_build=AgentRunParameterView(
            id=parameter.id,
            tool_version_id=parameter.tool_version_id,
            is_complete=parameter.is_complete,
            missing_fields=list(parameter.missing_fields or []),
            created_at=parameter.created_at,
        ) if parameter is not None else None,
        plan=plan_view,
        validation=validation_view,
        open_clarification=AgentRunClarificationView(
            id=clarification.id,
            request_type=clarification.request_type,
            prompt_text=clarification.prompt_text,
            fields=_question_fields(clarification.question_schema),
            requested_at=clarification.requested_at,
        ) if clarification is not None else None,
        safe_auto_execute=_safe_auto_execute(request, plan_view, validation),
    )


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




@router.get("/{request_id}/selection", response_model=AgentRunSelectionResult)
async def get_agent_run_selection(
    request_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> AgentRunSelectionResult:
    request = await _owned_request(session, request_id, principal.user_id)
    return await _selection_result(session, request)


@router.post("/{request_id}/select-tool", response_model=AgentRunSelectionResult)
async def select_agent_run_tool(
    request_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> AgentRunSelectionResult:
    """Explicit one-shot Tool retrieval / selection, never MCP execution."""
    request = await _owned_request(session, request_id, principal.user_id)
    if request.status != "RETRIEVING":
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Tool 검색·선택을 시작할 수 없는 요청 상태입니다.",
            status_code=409,
        )
    try:
        await ToolSelectorService(session).select(agent_request_id=request_id)
    except ModelProviderError as exc:
        raise AppError(
            code="MODEL_PROVIDER_UNAVAILABLE",
            message="Tool 검색·선택에 실패했습니다. 저장 상태를 확인해 주세요.",
            status_code=502,
            retryable=exc.retryable,
        ) from exc
    await session.refresh(request)
    return await _selection_result(session, request)



@router.get("/{request_id}/progress", response_model=AgentRunProgressResult)
async def get_agent_run_progress(
    request_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> AgentRunProgressResult:
    request = await _owned_request(session, request_id, principal.user_id)
    return await _progress_result(session, request)


@router.get("/{request_id}/chat-result", response_model=AgentRunChatResultView)
async def get_agent_run_chat_result(
    request_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> AgentRunChatResultView:
    """Owner-only Tool result projection for Agent Run chat (not Ops safe detail)."""
    result = await AgentRunChatResultService(session).get_for_owner(
        agent_request_id=request_id,
        requester_id=principal.user_id,
    )
    return AgentRunChatResultView(
        agent_request_id=result.agent_request_id,
        execution_id=result.execution_id,
        execution_status=result.execution_status,
        available=result.available,
        unavailable_reason=result.unavailable_reason,
        tool_name=result.tool_name,
        display_items=[
            AgentRunChatDisplayItemView(name=item.name, status=item.status)
            for item in result.display_items
        ],
        summary_text=result.summary_text,
        truncated=result.truncated,
    )


@router.post("/{request_id}/build-parameters", response_model=AgentRunProgressResult)
async def build_agent_run_parameters(
    request_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> AgentRunProgressResult:
    request = await _owned_request(session, request_id, principal.user_id)
    if request.status != "BUILDING_PARAMETERS":
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="파라미터를 구성할 수 없는 요청 상태입니다.",
            status_code=409,
        )
    await ParameterBuilderService(session).build(agent_request_id=request_id)
    await session.refresh(request)
    return await _progress_result(session, request)


@router.post("/{request_id}/generate-plan", response_model=AgentRunProgressResult)
async def generate_agent_run_plan(
    request_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> AgentRunProgressResult:
    request = await _owned_request(session, request_id, principal.user_id)
    if request.status != "PLANNING":
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="계획을 생성할 수 없는 요청 상태입니다.",
            status_code=409,
        )
    await PlanGeneratorService(session).generate(agent_request_id=request_id)
    await session.refresh(request)
    return await _progress_result(session, request)


@router.post("/{request_id}/validate-plan", response_model=AgentRunProgressResult)
async def validate_agent_run_plan(
    request_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> AgentRunProgressResult:
    request = await _owned_request(session, request_id, principal.user_id)
    if request.status != "VALIDATING":
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="계획을 검증할 수 없는 요청 상태입니다.",
            status_code=409,
        )
    await PlanValidatorService(session).validate(agent_request_id=request_id)
    await session.refresh(request)
    return await _progress_result(session, request)


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
