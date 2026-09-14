"""Deterministic Parameter Builder for AgentRequest BUILDING_PARAMETERS.

Internal Agent Runtime only. Does not call Plan Generator, Execution,
MCP tools/call, LLM, or SecretResolver.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import (
    AgentRequestStatus,
    AgentToolGrantEffect,
    BindingKind,
    ClarificationRequestType,
    ParameterProvenance,
    ToolVersionValidationStatus,
)
from app.models.conversation import AgentRequest
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.agent_tool_grant import AgentToolGrantRepository
from app.repositories.clarification_request import ClarificationRequestRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.parameter_build import ParameterBuildRepository
from app.repositories.tool_selection import ToolSelectionRepository
from app.schemas.parameter_binding import (
    LiteralBindingValue,
    ParameterBinding,
    SecretRefBindingValue,
)
from app.schemas.structured_request import (
    STRUCTURED_REQUEST_SCHEMA_VERSION,
    StructuredRequestEntity,
    StructuredRequestV1,
)
from app.services.agent_request import AgentRequestService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ParameterBuildOutcome:
    """Internal builder outcome — not a Domain enum."""

    is_complete: bool
    tool_version_id: UUID | None
    bindings: dict[str, ParameterBinding]
    missing_fields: list[str]
    agent_request_status: AgentRequestStatus


class ParameterBuilderService:
    """BUILDING_PARAMETERS → PLANNING | WAITING_INPUT | FAILED."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._requests = AgentRequestRepository(session)
        self._request_service = AgentRequestService(session)
        self._selections = ToolSelectionRepository(session)
        self._tools = MCPToolRepository(session)
        self._grants = AgentToolGrantRepository(session)
        self._parameter_builds = ParameterBuildRepository(session)
        self._clarifications = ClarificationRequestRepository(session)

    async def build(self, *, agent_request_id: UUID) -> ParameterBuildOutcome:
        request = await self._requests.get(agent_request_id)
        if request is None:
            raise AppError(
                code="NOT_FOUND",
                message="AgentRequest를 찾을 수 없습니다.",
                status_code=404,
            )
        if request.status != AgentRequestStatus.BUILDING_PARAMETERS.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Parameter Builder는 BUILDING_PARAMETERS 상태에서만 시작할 수 있습니다.",
                status_code=409,
            )

        try:
            return await self._build_locked(request)
        except AppError:
            raise
        except Exception:
            await self._fail_from(
                agent_request_id,
                expected=AgentRequestStatus.BUILDING_PARAMETERS,
            )
            raise

    async def _build_locked(self, request: AgentRequest) -> ParameterBuildOutcome:
        open_clarification = await self._clarifications.get_open_for_agent_request(
            request.id
        )
        if (
            open_clarification is not None
            and open_clarification.request_type
            == ClarificationRequestType.MISSING_PARAMETER.value
        ):
            await self._fail_and_raise(
                request.id,
                message=(
                    "BUILDING_PARAMETERS 상태에 기존 OPEN MISSING_PARAMETER "
                    "clarification이 남아 있습니다."
                ),
            )

        run = await self._selections.get_latest_for_agent_request(request.id)
        if (
            run is None
            or run.agent_request_id != request.id
            or run.agent_version_id != request.agent_version_id
            or run.selected_tool_version_id is None
        ):
            await self._fail_and_raise(
                request.id,
                message=(
                    "latest ToolSelectionRun 또는 selected_tool_version_id를 "
                    "복구할 수 없습니다."
                ),
            )

        candidates = await self._selections.list_candidates_for_run(run.id)
        if not any(
            c.tool_version_id == run.selected_tool_version_id for c in candidates
        ):
            await self._fail_and_raise(
                request.id,
                message=(
                    "selected_tool_version_id가 ToolSelection candidate "
                    "evidence에 없습니다."
                ),
            )

        tool_version = await self._tools.get_version(run.selected_tool_version_id)
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

        if grant.parameter_constraints is not None:
            await self._fail_and_raise(
                request.id,
                message=(
                    "parameter_constraints semantics가 정의되기 전까지 "
                    "non-null constraint는 fail closed합니다."
                ),
            )

        try:
            structured = self._load_structured_request(request)
        except AppError:
            await self._fail_and_raise(
                request.id,
                message="structured_request 재검증에 실패했습니다.",
            )

        try:
            properties, required = _validate_input_schema(tool_version.input_schema)
        except ValueError:
            await self._fail_and_raise(
                request.id,
                message="Tool input_schema가 손상되었습니다.",
            )

        bindings, missing_fields = _map_entities_to_bindings(
            entities=list(structured.entities),
            properties=properties,
            required=required,
        )
        is_complete = len(missing_fields) == 0
        bindings_snapshot = {
            name: binding.model_dump(mode="json") for name, binding in bindings.items()
        }
        input_schema_snapshot = (
            dict(tool_version.input_schema)
            if isinstance(tool_version.input_schema, dict)
            else tool_version.input_schema
        )

        agent_request_id = request.id
        try:
            if is_complete:
                await self._parameter_builds.create(
                    agent_request_id=agent_request_id,
                    tool_selection_run_id=run.id,
                    tool_version_id=tool_version.id,
                    input_schema_snapshot=input_schema_snapshot,
                    parameter_constraints_snapshot=None,
                    bindings_snapshot=bindings_snapshot,
                    missing_fields=[],
                    is_complete=True,
                )
                await self._request_service.compare_and_set_status(
                    agent_request_id,
                    expected_statuses=[AgentRequestStatus.BUILDING_PARAMETERS],
                    new_status=AgentRequestStatus.PLANNING,
                    set_completed_at=False,
                    completed_at=None,
                    extra_values={"missing_fields": []},
                )
                await self._session.commit()
                logger.info(
                    "parameter_builder complete agent_request_id=%s tool_version_id=%s "
                    "parameter_count=%s missing_count=0 is_complete=true",
                    agent_request_id,
                    tool_version.id,
                    len(bindings),
                )
                return ParameterBuildOutcome(
                    is_complete=True,
                    tool_version_id=tool_version.id,
                    bindings=bindings,
                    missing_fields=[],
                    agent_request_status=AgentRequestStatus.PLANNING,
                )

            question_schema = _project_missing_question_schema(
                properties, missing_fields
            )
            prompt_text = f"추가 입력이 필요합니다: {', '.join(missing_fields)}"
            await self._parameter_builds.create(
                agent_request_id=agent_request_id,
                tool_selection_run_id=run.id,
                tool_version_id=tool_version.id,
                input_schema_snapshot=input_schema_snapshot,
                parameter_constraints_snapshot=None,
                bindings_snapshot=bindings_snapshot,
                missing_fields=list(missing_fields),
                is_complete=False,
            )
            await self._clarifications.create_open(
                agent_request_id=agent_request_id,
                request_type=ClarificationRequestType.MISSING_PARAMETER.value,
                question_schema=question_schema,
                prompt_text=prompt_text,
                expires_at=None,
            )
            await self._request_service.compare_and_set_status(
                agent_request_id,
                expected_statuses=[AgentRequestStatus.BUILDING_PARAMETERS],
                new_status=AgentRequestStatus.WAITING_INPUT,
                set_completed_at=False,
                completed_at=None,
                extra_values={"missing_fields": list(missing_fields)},
            )
            await self._session.commit()
            logger.info(
                "parameter_builder waiting_input agent_request_id=%s tool_version_id=%s "
                "parameter_count=%s missing_count=%s is_complete=false",
                agent_request_id,
                tool_version.id,
                len(bindings),
                len(missing_fields),
            )
            return ParameterBuildOutcome(
                is_complete=False,
                tool_version_id=tool_version.id,
                bindings=bindings,
                missing_fields=list(missing_fields),
                agent_request_status=AgentRequestStatus.WAITING_INPUT,
            )
        except AppError as exc:
            await self._session.rollback()
            if exc.code == "RESOURCE_CONFLICT":
                logger.info(
                    "parameter_builder commit_cas_miss agent_request_id=%s",
                    agent_request_id,
                )
            raise
        except Exception:
            await self._session.rollback()
            raise

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
            "parameter_builder failed agent_request_id=%s reason=%s",
            agent_request_id,
            message,
        )
        await self._fail_from(
            agent_request_id,
            expected=AgentRequestStatus.BUILDING_PARAMETERS,
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
                    "parameter_builder fail_cas_miss agent_request_id=%s expected=%s",
                    agent_request_id,
                    expected.value,
                )
                try:
                    await self._session.rollback()
                except Exception:
                    logger.exception(
                        "parameter_builder fail_cas_miss rollback failed "
                        "agent_request_id=%s",
                        agent_request_id,
                    )
                return
            logger.exception(
                "parameter_builder fail_from app_error agent_request_id=%s code=%s",
                agent_request_id,
                exc.code,
            )
            try:
                await self._session.rollback()
            except Exception:
                logger.exception(
                    "parameter_builder fail_from rollback failed agent_request_id=%s",
                    agent_request_id,
                )
        except Exception:
            logger.exception(
                "parameter_builder fail_from unexpected agent_request_id=%s",
                agent_request_id,
            )
            try:
                await self._session.rollback()
            except Exception:
                logger.exception(
                    "parameter_builder fail_from rollback failed agent_request_id=%s",
                    agent_request_id,
                )


def _validate_input_schema(input_schema: Any) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(input_schema, dict):
        raise ValueError("input_schema must be object")
    schema_type = input_schema.get("type")
    if schema_type is not None and schema_type != "object":
        raise ValueError("input_schema.type must be object when present")

    properties_raw = input_schema.get("properties")
    if properties_raw is None:
        properties: dict[str, Any] = {}
    elif isinstance(properties_raw, dict):
        properties = properties_raw
    else:
        raise ValueError("input_schema.properties must be object when present")

    # Fail closed on normalized property-name collisions before entity matching.
    # Normalize is lookup-only; exact Tool property names are preserved as keys.
    normalized_properties: dict[str, str] = {}
    for prop_name in properties:
        if not isinstance(prop_name, str):
            raise ValueError("input_schema property names must be strings")
        normalized = _normalize_name(prop_name)
        if not normalized:
            raise ValueError(
                f"input_schema property name {prop_name!r} is empty after normalize"
            )
        previous = normalized_properties.get(normalized)
        if previous is not None and previous != prop_name:
            raise ValueError(
                "input_schema properties collide after normalize: "
                f"{previous!r} and {prop_name!r}"
            )
        normalized_properties[normalized] = prop_name

    required_raw = input_schema.get("required")
    if required_raw is None:
        required: list[str] = []
    elif isinstance(required_raw, list):
        required = []
        seen: set[str] = set()
        for item in required_raw:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("required items must be non-empty strings")
            if item in seen:
                raise ValueError("required items must be unique")
            seen.add(item)
            required.append(item)
    else:
        raise ValueError("input_schema.required must be string array when present")

    for name in required:
        if name not in properties:
            raise ValueError(f"required field {name!r} missing from properties")

    return properties, required


def _normalize_name(name: str) -> str:
    return name.strip().casefold()


def _map_entities_to_bindings(
    *,
    entities: list[StructuredRequestEntity],
    properties: dict[str, Any],
    required: list[str],
) -> tuple[dict[str, ParameterBinding], list[str]]:
    # Safe: uniqueness already enforced in _validate_input_schema.
    property_by_norm: dict[str, str] = {
        _normalize_name(prop_name): prop_name for prop_name in properties
    }

    buckets: dict[str, list[StructuredRequestEntity]] = {
        name: [] for name in properties
    }
    for entity in entities:
        matched = property_by_norm.get(_normalize_name(entity.name))
        if matched is None:
            continue
        buckets[matched].append(entity)

    bindings: dict[str, ParameterBinding] = {}
    missing: list[str] = []

    for prop_name in properties:
        matched_entities = buckets[prop_name]
        if not matched_entities:
            if prop_name in required:
                missing.append(prop_name)
            continue

        if len(matched_entities) > 1:
            missing.append(prop_name)
            continue

        entity = matched_entities[0]
        provenance = ParameterProvenance(entity.source)
        if provenance == ParameterProvenance.SECRET_REFERENCE:
            secret_id = _parse_secret_id(entity.value)
            if secret_id is None:
                missing.append(prop_name)
                continue
            bindings[prop_name] = ParameterBinding(
                provenance=provenance,
                binding=SecretRefBindingValue(
                    kind=BindingKind.SECRET_REF,
                    secret_id=secret_id,
                ),
            )
        else:
            bindings[prop_name] = ParameterBinding(
                provenance=provenance,
                binding=LiteralBindingValue(
                    kind=BindingKind.LITERAL,
                    value=entity.value,
                ),
            )

    deduped: list[str] = []
    seen_missing: set[str] = set()
    for name in missing:
        if name not in seen_missing:
            seen_missing.add(name)
            deduped.append(name)
    return bindings, deduped


def _parse_secret_id(value: Any) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


def _project_missing_question_schema(
    properties: dict[str, Any],
    missing_fields: list[str],
) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for name in missing_fields:
        prop_schema = properties.get(name)
        if isinstance(prop_schema, dict):
            projected[name] = prop_schema
        else:
            projected[name] = {"type": "string"}
    return {
        "type": "object",
        "properties": projected,
        "required": list(missing_fields),
        "additionalProperties": False,
    }
