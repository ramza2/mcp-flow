"""Deterministic Plan Generator for AgentRequest PLANNING.

Internal Agent Runtime only. Does not call Plan Validator, Execution,
MCP tools/call, LLM, or SecretResolver.

AgentRequest foundation produces a single TOOL step from durable
ParameterBuildRun evidence (docs/04 §9), or an optional sequential
READ_ONLY Tool pair (tool_1 → tool_2 with STEP_OUTPUT) when a unique
safe companion candidate exists (chat phase-2).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.sequential_readonly_pair import (
    DETERMINISTIC_TOOL_STEP_ID_2,
    build_sequential_pair_steps,
    discover_id_handoffs,
    is_safe_readonly_policy,
    requests_sequential_detail_lookup,
    resolve_companion_id_bindings,
)
from app.core.errors import AppError
from app.domain.enums import (
    AgentRequestStatus,
    AgentToolGrantEffect,
    AuthorableStepType,
    ClarificationRequestStatus,
    ClarificationRequestType,
    ToolVersionValidationStatus,
)
from app.models.conversation import AgentRequest
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.agent_tool_grant import AgentToolGrantRepository
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.clarification_request import ClarificationRequestRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.repositories.parameter_build import ParameterBuildRepository
from app.repositories.plan_generation import PlanGenerationRepository
from app.repositories.tool_selection import ToolSelectionRepository
from app.schemas.agent import CANONICAL_PLAN_SCHEMA_VERSION
from app.schemas.execution_plan import (
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    DETERMINISTIC_TOOL_STEP_ID,
    DETERMINISTIC_TOOL_STEP_NAME,
    EXECUTION_PLAN_SCHEMA_VERSION,
    ExecutionPlanStep,
    ExecutionPlanV1,
    PlanCompletion,
    PlanSourceAgent,
    ToolStepConfigV1,
    compute_plan_hash,
    default_plan_limits,
)
from app.schemas.parameter_binding import ParameterBuildSnapshot
from app.schemas.structured_request import (
    STRUCTURED_REQUEST_SCHEMA_VERSION,
    StructuredRequestV1,
)
from app.services.agent_request import AgentRequestService
from app.services.mcp_server import is_factory_imported

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PlanGenerationOutcome:
    """Internal generator outcome — not a Domain enum."""

    plan_generation_run_id: UUID
    plan_hash: str
    plan: ExecutionPlanV1
    agent_request_status: AgentRequestStatus


class PlanGeneratorService:
    """PLANNING → VALIDATING | FAILED (single TOOL or sequential READ_ONLY pair)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._requests = AgentRequestRepository(session)
        self._request_service = AgentRequestService(session)
        self._parameter_builds = ParameterBuildRepository(session)
        self._selections = ToolSelectionRepository(session)
        self._tools = MCPToolRepository(session)
        self._servers = MCPServerRepository(session)
        self._policies = MCPToolPolicyRepository(session)
        self._grants = AgentToolGrantRepository(session)
        self._versions = AgentVersionRepository(session)
        self._plans = PlanGenerationRepository(session)
        self._clarifications = ClarificationRequestRepository(session)

    async def generate(self, *, agent_request_id: UUID) -> PlanGenerationOutcome:
        request = await self._requests.get(agent_request_id)
        if request is None:
            raise AppError(
                code="NOT_FOUND",
                message="AgentRequest를 찾을 수 없습니다.",
                status_code=404,
            )
        if request.status != AgentRequestStatus.PLANNING.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Plan Generator는 PLANNING 상태에서만 시작할 수 있습니다.",
                status_code=409,
            )

        try:
            return await self._generate_locked(request)
        except AppError:
            raise
        except Exception:
            await self._fail_from(
                agent_request_id,
                expected=AgentRequestStatus.PLANNING,
            )
            raise

    async def _generate_locked(self, request: AgentRequest) -> PlanGenerationOutcome:
        build_run = await self._parameter_builds.get_latest_for_agent_request(
            request.id
        )
        if build_run is None or build_run.agent_request_id != request.id:
            await self._fail_and_raise(
                request.id,
                message="ParameterBuildRun이 없습니다.",
            )
        if not build_run.is_complete or list(build_run.missing_fields or []):
            await self._fail_and_raise(
                request.id,
                message=(
                    "latest ParameterBuildRun이 complete가 아닙니다. "
                    "과거 complete run을 조용히 선택하지 않습니다."
                ),
            )
        if build_run.parameter_constraints_snapshot is not None:
            await self._fail_and_raise(
                request.id,
                message=(
                    "complete ParameterBuildRun의 "
                    "parameter_constraints_snapshot이 null이 아닙니다."
                ),
            )

        try:
            snapshot = ParameterBuildSnapshot.model_validate(
                build_run.bindings_snapshot
            )
        except Exception:
            await self._fail_and_raise(
                request.id,
                message="ParameterBuildSnapshot 재검증에 실패했습니다.",
            )

        selection = await self._selections.get(build_run.tool_selection_run_id)
        if (
            selection is None
            or selection.agent_request_id != request.id
            or selection.agent_version_id != request.agent_version_id
            or selection.selected_tool_version_id != build_run.tool_version_id
        ):
            await self._fail_and_raise(
                request.id,
                message="ParameterBuildRun과 ToolSelectionRun evidence가 불일치합니다.",
            )

        tool_version = await self._tools.get_version(build_run.tool_version_id)
        if tool_version is None:
            await self._fail_and_raise(
                request.id,
                message="selected ToolVersion을 찾을 수 없습니다.",
            )
        if tool_version.validation_status != ToolVersionValidationStatus.VALID.value:
            await self._fail_and_raise(
                request.id,
                message="selected ToolVersion이 VALID가 아닙니다.",
            )

        if build_run.input_schema_snapshot != tool_version.input_schema:
            await self._fail_and_raise(
                request.id,
                message=(
                    "ParameterBuildRun.input_schema_snapshot이 "
                    "ToolVersion.input_schema와 불일치합니다."
                ),
            )

        agent_version = await self._versions.get(request.agent_version_id)
        if agent_version is None:
            await self._fail_and_raise(
                request.id,
                message="AgentVersion을 찾을 수 없습니다.",
            )
        if agent_version.plan_schema_version != CANONICAL_PLAN_SCHEMA_VERSION:
            await self._fail_and_raise(
                request.id,
                message=(
                    f"지원하지 않는 plan_schema_version="
                    f"{agent_version.plan_schema_version!r} 입니다."
                ),
            )
        # Fail closed: only exact JSON object {} is allowed. Do not coerce
        # null / [] / "" / false / 0 into {}.
        planning_settings = agent_version.planning_settings
        if not isinstance(planning_settings, dict) or planning_settings != {}:
            await self._fail_and_raise(
                request.id,
                message=(
                    "planning_settings semantics가 정의되기 전까지 "
                    "exact empty object {}만 허용합니다."
                ),
            )
        planning_settings_snapshot: dict[str, Any] = {}

        grants = await self._grants.list_for_version(request.agent_version_id)
        grant = next(
            (g for g in grants if g.mcp_tool_id == tool_version.mcp_tool_id),
            None,
        )
        if grant is None or grant.effect != AgentToolGrantEffect.ALLOW.value:
            await self._fail_and_raise(
                request.id,
                message="selected Tool에 대한 ALLOW AgentToolGrant가 없습니다.",
                code="FORBIDDEN",
                status_code=403,
            )

        try:
            structured = self._load_structured_request(request)
        except AppError:
            await self._fail_and_raise(
                request.id,
                message="structured_request 재검증에 실패했습니다.",
            )

        policy = await self._policies.get_by_tool_id(tool_version.mcp_tool_id)
        if policy is not None and policy.timeout_ms > 0:
            timeout_seconds = max(1, math.ceil(policy.timeout_ms / 1000))
        else:
            # Draft generation fallback only — does NOT mean ToolPolicy validation passed.
            timeout_seconds = DEFAULT_TOOL_TIMEOUT_SECONDS

        bindings_config: dict[str, Any] = {}
        for name, parameter_binding in snapshot.root.items():
            bindings_config[name] = parameter_binding.binding.model_dump(mode="json")

        pair_refs: list[dict[str, Any]] = [
            {
                "step_key": DETERMINISTIC_TOOL_STEP_ID,
                "mcp_tool_version_id": tool_version.id,
            }
        ]
        response_step_ids = [DETERMINISTIC_TOOL_STEP_ID]
        steps_payload: list[dict[str, Any]]

        sequential = await self._try_sequential_readonly_pair(
            request=request,
            structured=structured,
            selection=selection,
            primary_version=tool_version,
            primary_policy=policy,
            primary_bindings=bindings_config,
            primary_timeout_seconds=timeout_seconds,
            grants=grants,
        )
        if sequential is not None:
            steps_payload, companion_version_id, companion_timeout = sequential
            pair_refs.append(
                {
                    "step_key": DETERMINISTIC_TOOL_STEP_ID_2,
                    "mcp_tool_version_id": companion_version_id,
                }
            )
            response_step_ids = [DETERMINISTIC_TOOL_STEP_ID_2]
            _ = companion_timeout
        else:
            try:
                tool_config = ToolStepConfigV1.model_validate(
                    {
                        "tool_version_id": tool_version.id,
                        "bindings": bindings_config,
                    }
                )
                tool_config_dump = tool_config.model_dump(mode="json")
            except Exception:
                await self._fail_and_raise(
                    request.id,
                    message="ExecutionPlanV1 생성/검증에 실패했습니다.",
                )
            steps_payload = [
                ExecutionPlanStep(
                    id=DETERMINISTIC_TOOL_STEP_ID,
                    name=DETERMINISTIC_TOOL_STEP_NAME,
                    type=AuthorableStepType.TOOL,
                    required=True,
                    depends_on=[],
                    when=None,
                    timeout_seconds=timeout_seconds,
                    on_error="FAIL_EXECUTION",
                    config=tool_config_dump,
                ).model_dump(mode="json")
            ]

        try:
            plan = ExecutionPlanV1.model_validate(
                {
                    "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
                    "goal": structured.intent,
                    "source": PlanSourceAgent(
                        type="AGENT",
                        agent_version_id=request.agent_version_id,
                    ).model_dump(mode="json"),
                    "inputs": {},
                    "limits": default_plan_limits().model_dump(mode="json"),
                    "steps": steps_payload,
                    "completion": PlanCompletion(
                        success_policy="ALL_REQUIRED",
                        response_step_ids=response_step_ids,
                    ).model_dump(mode="json"),
                }
            )
            plan_snapshot = plan.model_dump(mode="json")
            plan_hash = compute_plan_hash(plan_snapshot)
        except Exception:
            await self._fail_and_raise(
                request.id,
                message="ExecutionPlanV1 생성/검증에 실패했습니다.",
            )

        agent_request_id = request.id
        try:
            run = await self._plans.create_run(
                agent_request_id=agent_request_id,
                parameter_build_run_id=build_run.id,
                agent_version_id=request.agent_version_id,
                plan_schema_version=EXECUTION_PLAN_SCHEMA_VERSION,
                plan_snapshot=plan_snapshot,
                plan_hash=plan_hash,
                planning_settings_snapshot=planning_settings_snapshot,
            )
            await self._plans.add_tool_refs(
                plan_generation_run_id=run.id,
                refs=pair_refs,
            )
            await self._request_service.compare_and_set_status(
                agent_request_id,
                expected_statuses=[AgentRequestStatus.PLANNING],
                new_status=AgentRequestStatus.VALIDATING,
                set_completed_at=False,
                completed_at=None,
            )
            await self._session.commit()
            logger.info(
                "plan_generator complete agent_request_id=%s "
                "plan_generation_run_id=%s plan_hash=%s tool_version_id=%s",
                agent_request_id,
                run.id,
                plan_hash,
                tool_version.id,
            )
            return PlanGenerationOutcome(
                plan_generation_run_id=run.id,
                plan_hash=plan_hash,
                plan=plan,
                agent_request_status=AgentRequestStatus.VALIDATING,
            )
        except AppError as exc:
            await self._session.rollback()
            if exc.code == "RESOURCE_CONFLICT":
                logger.info(
                    "plan_generator commit_cas_miss agent_request_id=%s",
                    agent_request_id,
                )
            raise
        except Exception:
            await self._session.rollback()
            raise

    async def _try_sequential_readonly_pair(
        self,
        *,
        request: AgentRequest,
        structured: StructuredRequestV1,
        selection: Any,
        primary_version: Any,
        primary_policy: Any,
        primary_bindings: dict[str, Any],
        primary_timeout_seconds: int,
        grants: list[Any],
    ) -> tuple[list[dict[str, Any]], UUID, int] | None:
        """Optionally build tool_1 → tool_2 when detail intent + unique companion.

        List-only requests return None (single TOOL). Composite/detail intent that
        cannot safely form a pair fail-closes — never silently shrinks to one TOOL.
        """
        # List-only / unclear: keep single TOOL.
        if not requests_sequential_detail_lookup(structured):
            return None

        # Detail/composite requested — blockers fail closed (no silent shrink).
        if selection.decision == "AUTO_SELECT":
            pass
        elif selection.decision == "CONFIRM":
            if not await self._has_answered_tool_confirmation(request):
                await self._fail_and_raise(
                    request.id,
                    code="EXECUTION_PRECONDITION_FAILED",
                    status_code=409,
                    message=(
                        "순차 READ_ONLY pair는 CONFIRM selection에 대해 "
                        "ANSWERED TOOL_CONFIRMATION confirmed=true가 필요합니다."
                    ),
                )
        else:
            await self._fail_and_raise(
                request.id,
                code="EXECUTION_PRECONDITION_FAILED",
                status_code=409,
                message=(
                    "순차 READ_ONLY pair는 AUTO_SELECT 또는 확인된 CONFIRM "
                    f"selection만 허용합니다 (decision={selection.decision!r})."
                ),
            )

        if not is_safe_readonly_policy(primary_policy):
            await self._fail_and_raise(
                request.id,
                code="EXECUTION_PRECONDITION_FAILED",
                status_code=409,
                message=(
                    "순차 READ_ONLY pair primary ToolPolicy가 안전하지 않아 "
                    "단일 TOOL로 축소하지 않습니다."
                ),
            )

        primary_tool = await self._tools.get(primary_version.mcp_tool_id)
        if primary_tool is None:
            await self._fail_and_raise(
                request.id,
                code="EXECUTION_PRECONDITION_FAILED",
                status_code=409,
                message="순차 pair primary Tool을 찾지 못했습니다.",
            )
        server = await self._servers.get(primary_tool.mcp_server_id)
        uses_factory = bool(server is not None and is_factory_imported(server))

        candidates = await self._selections.list_candidates_for_run(selection.id)
        allow_ids = {
            g.mcp_tool_id
            for g in grants
            if g.effect == AgentToolGrantEffect.ALLOW.value
        }

        matches: list[tuple[Any, Any, list[Any], int]] = []
        for candidate in candidates:
            if candidate.tool_version_id == primary_version.id:
                continue
            if candidate.risk_class != "READ_ONLY":
                continue
            companion = await self._tools.get_version(candidate.tool_version_id)
            if companion is None:
                continue
            if companion.validation_status != ToolVersionValidationStatus.VALID.value:
                continue
            if companion.mcp_tool_id not in allow_ids:
                continue
            companion_policy = await self._policies.get_by_tool_id(companion.mcp_tool_id)
            if not is_safe_readonly_policy(companion_policy):
                continue
            handoffs = discover_id_handoffs(
                primary_output_schema=primary_version.output_schema,
                companion_input_schema=companion.input_schema,
                uses_factory_envelope=uses_factory,
            )
            if not handoffs:
                continue
            if companion_policy is not None and companion_policy.timeout_ms > 0:
                companion_timeout = max(1, math.ceil(companion_policy.timeout_ms / 1000))
            else:
                companion_timeout = DEFAULT_TOOL_TIMEOUT_SECONDS
            matches.append((companion, companion_policy, handoffs, companion_timeout))

        # Exactly one compatible companion — never pick arbitrarily among many.
        if len(matches) != 1:
            logger.info(
                "plan_generator sequential_pair fail_closed agent_request_id=%s "
                "reason=companion_count count=%s",
                request.id,
                len(matches),
            )
            await self._fail_and_raise(
                request.id,
                code="EXECUTION_PRECONDITION_FAILED",
                status_code=409,
                message=(
                    "상세/복합 요청에 대해 schema-compatible companion이 "
                    f"정확히 1개가 아니어서 순차 pair를 만들지 않습니다 "
                    f"(count={len(matches)})."
                ),
            )

        companion, _policy, handoffs, companion_timeout = matches[0]
        companion_bindings = resolve_companion_id_bindings(
            structured=structured,
            handoffs=handoffs,
        )
        if companion_bindings is None:
            await self._fail_and_raise(
                request.id,
                code="EXECUTION_PRECONDITION_FAILED",
                status_code=409,
                message=(
                    "상세/복합 요청에 명시적 id entity도 first-item 신호도 없어 "
                    "tool_2 id를 추측하지 않습니다."
                ),
            )
        steps = build_sequential_pair_steps(
            primary_tool_version_id=primary_version.id,
            primary_bindings=primary_bindings,
            primary_timeout_seconds=primary_timeout_seconds,
            companion_tool_version_id=companion.id,
            companion_timeout_seconds=companion_timeout,
            handoffs=handoffs,
            companion_bindings=companion_bindings,
        )
        logger.info(
            "plan_generator sequential_pair agent_request_id=%s "
            "primary=%s companion=%s handoffs=%s binding_kinds=%s",
            request.id,
            primary_version.id,
            companion.id,
            [h.target_param for h in handoffs],
            {
                name: binding.get("kind")
                for name, binding in companion_bindings.items()
                if isinstance(binding, dict)
            },
        )
        return steps, companion.id, companion_timeout

    async def _has_answered_tool_confirmation(self, request: AgentRequest) -> bool:
        """True when same-request TOOL_CONFIRMATION is ANSWERED confirmed=true.

        Rejects OPEN / EXPIRED / CANCELLED / confirmed=false / answer-after-expiry.
        """
        clarification = (
            await self._clarifications.get_latest_answered_tool_confirmation(
                request.id
            )
        )
        if clarification is None:
            return False
        if (
            clarification.request_type
            != ClarificationRequestType.TOOL_CONFIRMATION.value
        ):
            return False
        if clarification.status != ClarificationRequestStatus.ANSWERED.value:
            return False
        if clarification.response_payload != {"confirmed": True}:
            return False
        if clarification.expires_at is not None and clarification.answered_at is not None:
            expires = clarification.expires_at
            answered = clarification.answered_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if answered.tzinfo is None:
                answered = answered.replace(tzinfo=UTC)
            if answered > expires:
                return False
        return True

    def _load_structured_request(self, request: AgentRequest) -> StructuredRequestV1:
        if request.structured_request_version != STRUCTURED_REQUEST_SCHEMA_VERSION:
            raise AppError(
                code="INTERNAL_ERROR",
                message="structured_request_version이 지원되지 않습니다.",
                status_code=500,
            )
        if not isinstance(request.structured_request, dict):
            raise AppError(
                code="INTERNAL_ERROR",
                message="structured_request가 없습니다.",
                status_code=500,
            )
        try:
            return StructuredRequestV1.model_validate(request.structured_request)
        except Exception as exc:
            raise AppError(
                code="INTERNAL_ERROR",
                message="structured_request 재검증에 실패했습니다.",
                status_code=500,
            ) from exc

    async def _fail_and_raise(
        self,
        agent_request_id: UUID,
        *,
        message: str,
        code: str = "INTERNAL_ERROR",
        status_code: int = 500,
    ) -> None:
        logger.warning(
            "plan_generator failed agent_request_id=%s reason=%s",
            agent_request_id,
            message,
        )
        await self._fail_from(
            agent_request_id,
            expected=AgentRequestStatus.PLANNING,
        )
        raise AppError(code=code, message=message, status_code=status_code)

    async def _fail_from(
        self,
        agent_request_id: UUID,
        *,
        expected: AgentRequestStatus,
    ) -> None:
        """Best-effort FAILED transition. Never masks the caller's original exception."""
        try:
            await self._request_service.compare_and_set_status(
                agent_request_id,
                expected_statuses=[expected],
                new_status=AgentRequestStatus.FAILED,
                set_completed_at=True,
                completed_at=datetime.now(UTC),
            )
            await self._session.commit()
        except AppError as exc:
            if exc.code == "RESOURCE_CONFLICT":
                logger.info(
                    "plan_generator fail_cas_miss agent_request_id=%s expected=%s",
                    agent_request_id,
                    expected.value,
                )
                try:
                    await self._session.rollback()
                except Exception:
                    logger.exception(
                        "plan_generator fail_cas_miss rollback failed "
                        "agent_request_id=%s",
                        agent_request_id,
                    )
                return
            logger.exception(
                "plan_generator fail_from app_error agent_request_id=%s code=%s",
                agent_request_id,
                exc.code,
            )
            try:
                await self._session.rollback()
            except Exception:
                logger.exception(
                    "plan_generator fail_from rollback failed agent_request_id=%s",
                    agent_request_id,
                )
        except Exception:
            logger.exception(
                "plan_generator fail_from unexpected agent_request_id=%s",
                agent_request_id,
            )
            try:
                await self._session.rollback()
            except Exception:
                logger.exception(
                    "plan_generator fail_from rollback failed agent_request_id=%s",
                    agent_request_id,
                )
