"""AgentVersion lifecycle and Tool Grant service (docs/05–06)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import (
    AgentToolGrantEffect,
    AgentVersionStatus,
    AgentVersionValidationStatus,
)
from app.models.agent import Agent, AgentToolGrant, AgentVersion
from app.repositories.agent import AgentRepository
from app.repositories.agent_tool_grant import AgentToolGrantRepository
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.schemas.agent import (
    CANONICAL_PLAN_SCHEMA_VERSION,
    CANONICAL_REQUEST_SCHEMA_VERSION,
    AgentToolGrantPut,
    AgentVersionCreate,
    SelectionSettings,
)
from app.services.agent_content import agent_version_content_hash


class AgentVersionService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._agents = AgentRepository(session)
        self._versions = AgentVersionRepository(session)
        self._grants = AgentToolGrantRepository(session)
        self._tools = MCPToolRepository(session)

    async def _require_agent(self, agent_id: uuid.UUID) -> Agent:
        agent = await self._agents.get(agent_id)
        if agent is None:
            raise AppError(
                code="NOT_FOUND",
                message="Agent not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return agent

    async def _require_version(
        self, agent_id: uuid.UUID, version_id: uuid.UUID
    ) -> AgentVersion:
        version = await self._versions.get_for_agent(agent_id, version_id)
        if version is None:
            raise AppError(
                code="NOT_FOUND",
                message="Agent version not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return version

    async def list_versions(
        self,
        agent_id: uuid.UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        sort: str = "-version_no",
    ) -> tuple[list[AgentVersion], int]:
        await self._require_agent(agent_id)
        return await self._versions.list_for_agent(
            agent_id, page=page, page_size=page_size, sort=sort
        )

    async def get_version(
        self, agent_id: uuid.UUID, version_id: uuid.UUID
    ) -> AgentVersion:
        await self._require_agent(agent_id)
        return await self._require_version(agent_id, version_id)

    async def create_version(
        self, agent_id: uuid.UUID, data: AgentVersionCreate
    ) -> AgentVersion:
        agent = await self._agents.lock_for_update(agent_id)
        if agent is None:
            raise AppError(
                code="NOT_FOUND",
                message="Agent not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        if data.source_version_id is not None:
            source = await self._versions.get_for_agent(agent_id, data.source_version_id)
            if source is None:
                raise AppError(
                    code="NOT_FOUND",
                    message="source_version_id must reference a version of this Agent.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            system_instruction = data.system_instruction or source.system_instruction
            llm_profile_id = (
                data.llm_profile_id
                if data.llm_profile_id is not None
                else source.llm_profile_id
            )
            request_schema_version = (
                data.request_schema_version or source.request_schema_version
            )
            plan_schema_version = data.plan_schema_version or source.plan_schema_version
            selection_settings = (
                data.selection_settings.model_dump()
                if data.selection_settings is not None
                else dict(source.selection_settings or {})
            )
            planning_settings = (
                dict(data.planning_settings)
                if data.planning_settings is not None
                else dict(source.planning_settings or {})
            )
            response_settings = (
                dict(data.response_settings)
                if data.response_settings is not None
                else dict(source.response_settings or {})
            )
            change_summary = data.change_summary
            copy_grants_from = source.id
        else:
            assert data.system_instruction is not None
            assert data.llm_profile_id is not None
            assert data.selection_settings is not None
            system_instruction = data.system_instruction
            llm_profile_id = data.llm_profile_id
            request_schema_version = (
                data.request_schema_version or CANONICAL_REQUEST_SCHEMA_VERSION
            )
            plan_schema_version = (
                data.plan_schema_version or CANONICAL_PLAN_SCHEMA_VERSION
            )
            selection_settings = data.selection_settings.model_dump()
            planning_settings = dict(data.planning_settings or {})
            response_settings = dict(data.response_settings or {})
            change_summary = data.change_summary
            copy_grants_from = None

        version_no = await self._versions.next_version_no(agent_id)
        content_hash = agent_version_content_hash(
            system_instruction=system_instruction,
            llm_profile_id=llm_profile_id,
            request_schema_version=request_schema_version,
            plan_schema_version=plan_schema_version,
            selection_settings=selection_settings,
            planning_settings=planning_settings,
            response_settings=response_settings,
        )

        try:
            version = await self._versions.create(
                agent_id=agent_id,
                version_no=version_no,
                system_instruction=system_instruction,
                llm_profile_id=llm_profile_id,
                request_schema_version=request_schema_version,
                plan_schema_version=plan_schema_version,
                selection_settings=selection_settings,
                planning_settings=planning_settings,
                response_settings=response_settings,
                content_hash=content_hash,
                change_summary=change_summary,
            )
            if copy_grants_from is not None:
                await self._grants.copy_from_version(
                    source_version_id=copy_grants_from,
                    target_version_id=version.id,
                )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Concurrent AgentVersion create conflict; retry.",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc

        await self._session.refresh(version)
        return version

    async def validate(
        self, agent_id: uuid.UUID, version_id: uuid.UUID
    ) -> AgentVersion:
        await self._require_agent(agent_id)
        version = await self._versions.lock_for_update(agent_id, version_id)
        if version is None:
            raise AppError(
                code="NOT_FOUND",
                message="Agent version not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if version.status != AgentVersionStatus.DRAFT:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Only DRAFT AgentVersion can be validated.",
                status_code=status.HTTP_409_CONFLICT,
            )

        errors: list[dict[str, str]] = []
        if not (version.system_instruction or "").strip():
            errors.append(
                {
                    "code": "SYSTEM_INSTRUCTION_EMPTY",
                    "message": "system_instruction must be non-empty.",
                }
            )
        if version.request_schema_version != CANONICAL_REQUEST_SCHEMA_VERSION:
            errors.append(
                {
                    "code": "REQUEST_SCHEMA_VERSION",
                    "message": (
                        f"request_schema_version must be "
                        f"{CANONICAL_REQUEST_SCHEMA_VERSION}."
                    ),
                }
            )
        if version.plan_schema_version != CANONICAL_PLAN_SCHEMA_VERSION:
            errors.append(
                {
                    "code": "PLAN_SCHEMA_VERSION",
                    "message": (
                        f"plan_schema_version must be {CANONICAL_PLAN_SCHEMA_VERSION}."
                    ),
                }
            )
        if version.llm_profile_id is None:
            errors.append(
                {
                    "code": "LLM_PROFILE_REQUIRED",
                    "message": "llm_profile_id is required.",
                }
            )

        try:
            SelectionSettings.model_validate(version.selection_settings or {})
        except Exception as exc:  # noqa: BLE001 — collect as structured validation error
            errors.append(
                {
                    "code": "SELECTION_SETTINGS_INVALID",
                    "message": str(exc),
                }
            )

        grants = await self._grants.list_for_version(version.id)
        seen_tools: set[uuid.UUID] = set()
        for grant in grants:
            if grant.mcp_tool_id in seen_tools:
                errors.append(
                    {
                        "code": "DUPLICATE_TOOL_GRANT",
                        "message": f"Duplicate mcp_tool_id {grant.mcp_tool_id}.",
                    }
                )
            seen_tools.add(grant.mcp_tool_id)
            try:
                AgentToolGrantEffect(grant.effect)
            except ValueError:
                errors.append(
                    {
                        "code": "GRANT_EFFECT_INVALID",
                        "message": f"Invalid grant effect: {grant.effect}.",
                    }
                )
            if grant.parameter_constraints is not None and not isinstance(
                grant.parameter_constraints, dict
            ):
                errors.append(
                    {
                        "code": "PARAMETER_CONSTRAINTS_INVALID",
                        "message": "parameter_constraints must be object or null.",
                    }
                )
            tool = await self._tools.get(grant.mcp_tool_id)
            if tool is None:
                errors.append(
                    {
                        "code": "TOOL_NOT_FOUND",
                        "message": f"mcp_tool_id {grant.mcp_tool_id} does not exist.",
                    }
                )

        valid = len(errors) == 0
        report: dict[str, Any] = {
            "schema_version": "1.0",
            "valid": valid,
            "errors": errors,
            "dependency_checks": {
                "llm_profile": "DEFERRED",
                "provider_reference_validation": "deferred",
            },
        }
        status_value = (
            AgentVersionValidationStatus.VALID
            if valid
            else AgentVersionValidationStatus.INVALID
        )
        updated = await self._versions.set_validation(
            version,
            validation_status=str(status_value),
            validation_report=report,
        )
        await self._session.commit()
        await self._session.refresh(updated)
        return updated

    async def publish(
        self, agent_id: uuid.UUID, version_id: uuid.UUID
    ) -> AgentVersion:
        agent = await self._agents.lock_for_update(agent_id)
        if agent is None:
            raise AppError(
                code="NOT_FOUND",
                message="Agent not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        version = await self._versions.lock_for_update(agent_id, version_id)
        if version is None:
            raise AppError(
                code="NOT_FOUND",
                message="Agent version not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        if version.status != AgentVersionStatus.DRAFT:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Only DRAFT AgentVersion can be published.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if version.validation_status != AgentVersionValidationStatus.VALID:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="AgentVersion must be VALID before publish.",
                status_code=status.HTTP_409_CONFLICT,
            )

        now = datetime.now(UTC)
        previous_id = agent.current_version_id
        if previous_id is not None and previous_id != version.id:
            previous = await self._versions.lock_for_update(agent_id, previous_id)
            if previous is not None and previous.status == AgentVersionStatus.PUBLISHED:
                await self._versions.mark_deprecated(previous, deprecated_at=now)

        published = await self._versions.mark_published(version, published_at=now)
        updated_agent = await self._agents.set_current_version(
            agent_id,
            current_version_id=published.id,
            expected_lock_version=int(agent.lock_version),
        )
        if updated_agent is None:
            await self._session.rollback()
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Concurrent Agent publish conflict; retry.",
                status_code=status.HTTP_409_CONFLICT,
            )

        await self._session.commit()
        await self._session.refresh(published)
        return published

    async def deprecate(
        self, agent_id: uuid.UUID, version_id: uuid.UUID
    ) -> AgentVersion:
        agent = await self._agents.lock_for_update(agent_id)
        if agent is None:
            raise AppError(
                code="NOT_FOUND",
                message="Agent not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        version = await self._versions.lock_for_update(agent_id, version_id)
        if version is None:
            raise AppError(
                code="NOT_FOUND",
                message="Agent version not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        if version.status == AgentVersionStatus.DEPRECATED:
            return version

        if version.status == AgentVersionStatus.DRAFT:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="DRAFT AgentVersion cannot be deprecated.",
                status_code=status.HTTP_409_CONFLICT,
            )

        if agent.current_version_id == version.id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Cannot directly deprecate the Agent current_version; "
                    "publish a newer version instead."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )

        if version.status != AgentVersionStatus.PUBLISHED:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Only PUBLISHED AgentVersion can be deprecated.",
                status_code=status.HTTP_409_CONFLICT,
            )

        updated = await self._versions.mark_deprecated(
            version, deprecated_at=datetime.now(UTC)
        )
        await self._session.commit()
        await self._session.refresh(updated)
        return updated

    async def list_grants(
        self, agent_id: uuid.UUID, version_id: uuid.UUID
    ) -> list[AgentToolGrant]:
        await self._require_agent(agent_id)
        await self._require_version(agent_id, version_id)
        return await self._grants.list_for_version(version_id)

    async def replace_grants(
        self,
        agent_id: uuid.UUID,
        version_id: uuid.UUID,
        data: AgentToolGrantPut,
    ) -> list[AgentToolGrant]:
        await self._require_agent(agent_id)
        version = await self._versions.lock_for_update(agent_id, version_id)
        if version is None:
            raise AppError(
                code="NOT_FOUND",
                message="Agent version not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if version.status != AgentVersionStatus.DRAFT:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Tool Grants can only be replaced on DRAFT AgentVersion.",
                status_code=status.HTTP_409_CONFLICT,
            )

        seen: set[uuid.UUID] = set()
        payload: list[dict[str, Any]] = []
        for item in data.items:
            if item.mcp_tool_id in seen:
                raise AppError(
                    code="VALIDATION_ERROR",
                    message=f"Duplicate mcp_tool_id in request: {item.mcp_tool_id}.",
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            seen.add(item.mcp_tool_id)
            tool = await self._tools.get(item.mcp_tool_id)
            if tool is None:
                raise AppError(
                    code="VALIDATION_ERROR",
                    message=f"mcp_tool_id does not exist: {item.mcp_tool_id}.",
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            payload.append(
                {
                    "mcp_tool_id": item.mcp_tool_id,
                    "effect": str(item.effect),
                    "parameter_constraints": item.parameter_constraints,
                    "requires_confirmation": item.requires_confirmation,
                }
            )

        try:
            grants = await self._grants.replace_all(version.id, payload)
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Tool Grant replace failed due to a database constraint.",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc

        return grants
