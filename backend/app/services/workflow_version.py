"""WorkflowVersion lifecycle, Plan save, validation, publish (docs/05–06)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.complex_plan_validator import StaticComplexPlanValidator
from app.core.errors import AppError
from app.domain.enums import (
    ApprovalPolicyStatus,
    AuthorableStepType,
    ToolVersionValidationStatus,
    WorkflowVersionStatus,
    WorkflowVersionValidationStatus,
)
from app.models.workflow import Workflow, WorkflowVersion
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.workflow import WorkflowRepository
from app.repositories.workflow_version import WorkflowVersionRepository
from app.repositories.workflow_version_tool_ref import WorkflowVersionToolRefRepository
from app.schemas.execution_plan import (
    ExecutionPlanV1,
    parse_approval_step_config,
    parse_complex_tool_step_config,
)
from app.schemas.workflow import (
    CANONICAL_PLAN_SCHEMA_VERSION,
    WorkflowPlanPut,
    WorkflowVersionCreate,
)
from app.services.workflow_content import workflow_version_content_hash


def _error(
    *,
    code: str,
    message: str,
    step_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {"code": code, "message": message}
    if step_id is not None:
        row["step_id"] = step_id
    if details:
        row["details"] = details
    return row


def _sort_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        errors,
        key=lambda e: (
            str(e.get("code", "")),
            str(e.get("step_id", "")),
            str(e.get("message", "")),
        ),
    )


class WorkflowVersionService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._workflows = WorkflowRepository(session)
        self._versions = WorkflowVersionRepository(session)
        self._tool_refs = WorkflowVersionToolRefRepository(session)
        self._tools = MCPToolRepository(session)
        self._policies = ApprovalPolicyRepository(session)
        self._static_validator = StaticComplexPlanValidator()

    async def _require_workflow(self, workflow_id: uuid.UUID) -> Workflow:
        workflow = await self._workflows.get(workflow_id)
        if workflow is None:
            raise AppError(
                code="NOT_FOUND",
                message="Workflow not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return workflow

    async def _require_version(
        self, workflow_id: uuid.UUID, version_id: uuid.UUID
    ) -> WorkflowVersion:
        version = await self._versions.get_for_workflow(workflow_id, version_id)
        if version is None:
            raise AppError(
                code="NOT_FOUND",
                message="Workflow version not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return version

    async def list_versions(
        self,
        workflow_id: uuid.UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        sort: str = "-version_no",
    ) -> tuple[list[WorkflowVersion], int]:
        await self._require_workflow(workflow_id)
        return await self._versions.list_for_workflow(
            workflow_id, page=page, page_size=page_size, sort=sort
        )

    async def get_version(
        self, workflow_id: uuid.UUID, version_id: uuid.UUID
    ) -> WorkflowVersion:
        await self._require_workflow(workflow_id)
        return await self._require_version(workflow_id, version_id)

    async def create_version(
        self, workflow_id: uuid.UUID, data: WorkflowVersionCreate
    ) -> WorkflowVersion:
        workflow = await self._workflows.lock_for_update(workflow_id)
        if workflow is None:
            raise AppError(
                code="NOT_FOUND",
                message="Workflow not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        if data.source_version_id is not None:
            source = await self._versions.lock_for_update(
                workflow_id, data.source_version_id
            )
            if source is None:
                raise AppError(
                    code="NOT_FOUND",
                    message=(
                        "source_version_id must reference a version of this Workflow."
                    ),
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            plan_definition = (
                dict(data.plan_definition)
                if data.plan_definition is not None
                else dict(source.plan_definition or {})
            )
            plan_schema_version = (
                data.plan_schema_version or source.plan_schema_version
            )
            input_schema = (
                dict(data.input_schema)
                if data.input_schema is not None
                else dict(source.input_schema or {})
            )
            output_schema = (
                dict(data.output_schema)
                if data.output_schema is not None
                else dict(source.output_schema or {})
            )
            policy_defaults = (
                dict(data.policy_defaults)
                if data.policy_defaults is not None
                else dict(source.policy_defaults or {})
            )
            change_summary = data.change_summary
        else:
            assert data.plan_definition is not None
            plan_definition = dict(data.plan_definition)
            plan_schema_version = (
                data.plan_schema_version or CANONICAL_PLAN_SCHEMA_VERSION
            )
            input_schema = dict(data.input_schema or {})
            output_schema = dict(data.output_schema or {})
            policy_defaults = dict(data.policy_defaults or {})
            change_summary = data.change_summary

        version_no = await self._versions.next_version_no(workflow_id)
        content_hash = workflow_version_content_hash(
            plan_schema_version=plan_schema_version,
            plan_definition=plan_definition,
            input_schema=input_schema,
            output_schema=output_schema,
            policy_defaults=policy_defaults,
        )

        try:
            version = await self._versions.create(
                workflow_id=workflow_id,
                version_no=version_no,
                plan_schema_version=plan_schema_version,
                plan_definition=plan_definition,
                input_schema=input_schema,
                output_schema=output_schema,
                policy_defaults=policy_defaults,
                content_hash=content_hash,
                change_summary=change_summary,
            )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Concurrent WorkflowVersion create conflict; retry.",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc

        await self._session.refresh(version)
        return version

    async def put_plan(
        self,
        workflow_id: uuid.UUID,
        version_id: uuid.UUID,
        data: WorkflowPlanPut,
    ) -> WorkflowVersion:
        await self._require_workflow(workflow_id)
        version = await self._versions.lock_for_update(workflow_id, version_id)
        if version is None:
            raise AppError(
                code="NOT_FOUND",
                message="Workflow version not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if version.status != WorkflowVersionStatus.DRAFT:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Only DRAFT WorkflowVersion plan can be updated.",
                status_code=status.HTTP_409_CONFLICT,
            )

        plan_definition = dict(data.plan_definition)
        content_hash = workflow_version_content_hash(
            plan_schema_version=version.plan_schema_version,
            plan_definition=plan_definition,
            input_schema=version.input_schema,
            output_schema=version.output_schema,
            policy_defaults=version.policy_defaults,
        )
        updated = await self._versions.update_plan(
            version,
            plan_definition=plan_definition,
            content_hash=content_hash,
            change_summary=data.change_summary,
        )
        await self._tool_refs.clear(updated.id)
        await self._session.commit()
        await self._session.refresh(updated)
        return updated

    async def _collect_dependency_errors(
        self, plan: ExecutionPlanV1
    ) -> tuple[list[dict[str, Any]], list[tuple[str, uuid.UUID]], str, str]:
        """Return (errors, tool_ref_items, tool_check, approval_check)."""

        errors: list[dict[str, Any]] = []
        tool_refs: list[tuple[str, uuid.UUID]] = []
        tool_check = "OK"
        approval_check = "OK"

        for step in plan.steps:
            if step.type != AuthorableStepType.TOOL:
                continue
            try:
                cfg = parse_complex_tool_step_config(step.config)
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    _error(
                        code="WORKFLOW_TOOL_CONFIG_INVALID",
                        message=str(exc),
                        step_id=step.id,
                    )
                )
                tool_check = "ERROR"
                continue
            tv = await self._tools.get_version(cfg.tool_version_id)
            if tv is None:
                errors.append(
                    _error(
                        code="WORKFLOW_TOOL_VERSION_NOT_FOUND",
                        message=(
                            f"tool_version_id {cfg.tool_version_id} does not exist."
                        ),
                        step_id=step.id,
                        details={
                            "step_id": step.id,
                            "tool_version_id": str(cfg.tool_version_id),
                        },
                    )
                )
                tool_check = "NOT_FOUND"
                continue
            if tv.validation_status != ToolVersionValidationStatus.VALID:
                errors.append(
                    _error(
                        code="WORKFLOW_TOOL_VERSION_INVALID",
                        message=(
                            f"tool_version_id {cfg.tool_version_id} "
                            "validation_status is not VALID."
                        ),
                        step_id=step.id,
                        details={
                            "step_id": step.id,
                            "tool_version_id": str(cfg.tool_version_id),
                            "validation_status": tv.validation_status,
                        },
                    )
                )
                tool_check = "INVALID"
                continue
            tool_refs.append((step.id, cfg.tool_version_id))

        for step in plan.steps:
            if step.type != AuthorableStepType.APPROVAL:
                continue
            try:
                cfg = parse_approval_step_config(step.config)
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    _error(
                        code="WORKFLOW_APPROVAL_CONFIG_INVALID",
                        message=str(exc),
                        step_id=step.id,
                    )
                )
                approval_check = "ERROR"
                continue
            policy = await self._policies.get(cfg.approval_policy_id)
            if policy is None:
                errors.append(
                    _error(
                        code="WORKFLOW_APPROVAL_POLICY_NOT_FOUND",
                        message=(
                            f"approval_policy_id {cfg.approval_policy_id} "
                            "does not exist."
                        ),
                        step_id=step.id,
                        details={
                            "step_id": step.id,
                            "approval_policy_id": str(cfg.approval_policy_id),
                        },
                    )
                )
                approval_check = "NOT_FOUND"
                continue
            if policy.status != ApprovalPolicyStatus.ACTIVE:
                errors.append(
                    _error(
                        code="WORKFLOW_APPROVAL_POLICY_INACTIVE",
                        message=(
                            f"approval_policy_id {cfg.approval_policy_id} "
                            "is not ACTIVE."
                        ),
                        step_id=step.id,
                        details={
                            "step_id": step.id,
                            "approval_policy_id": str(cfg.approval_policy_id),
                            "status": policy.status,
                        },
                    )
                )
                approval_check = "INACTIVE"

        return errors, tool_refs, tool_check, approval_check

    async def _build_validation(
        self, version: WorkflowVersion
    ) -> tuple[bool, dict[str, Any], list[tuple[str, uuid.UUID]]]:
        errors: list[dict[str, Any]] = []
        checks: dict[str, str] = {
            "plan_schema": "OK",
            "static_plan": "OK",
            "tool_versions": "OK",
            "approval_policies": "OK",
        }
        tool_refs: list[tuple[str, uuid.UUID]] = []

        if not isinstance(version.input_schema, dict):
            errors.append(
                _error(
                    code="WORKFLOW_INPUT_SCHEMA_INVALID",
                    message="input_schema must be a JSON object.",
                )
            )
        if not isinstance(version.output_schema, dict):
            errors.append(
                _error(
                    code="WORKFLOW_OUTPUT_SCHEMA_INVALID",
                    message="output_schema must be a JSON object.",
                )
            )
        if not isinstance(version.policy_defaults, dict):
            errors.append(
                _error(
                    code="WORKFLOW_POLICY_DEFAULTS_INVALID",
                    message="policy_defaults must be a JSON object.",
                )
            )

        if version.plan_schema_version != CANONICAL_PLAN_SCHEMA_VERSION:
            errors.append(
                _error(
                    code="PLAN_SCHEMA_VERSION",
                    message=(
                        f"plan_schema_version must be {CANONICAL_PLAN_SCHEMA_VERSION}."
                    ),
                )
            )
            checks["plan_schema"] = "ERROR"
            plan: ExecutionPlanV1 | None = None
        elif not isinstance(version.plan_definition, dict):
            errors.append(
                _error(
                    code="PLAN_DEFINITION_INVALID",
                    message="plan_definition must be a JSON object.",
                )
            )
            checks["plan_schema"] = "ERROR"
            plan = None
        else:
            try:
                plan = ExecutionPlanV1.model_validate(version.plan_definition)
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    _error(
                        code="PLAN_DEFINITION_INVALID",
                        message=str(exc),
                    )
                )
                checks["plan_schema"] = "ERROR"
                plan = None

        if plan is not None:
            if plan.source.type != "WORKFLOW":
                errors.append(
                    _error(
                        code="WORKFLOW_PLAN_SOURCE_INVALID",
                        message=(
                            "Workflow Plan source.type must be WORKFLOW "
                            f"(got {plan.source.type!r})."
                        ),
                    )
                )
                checks["plan_schema"] = "ERROR"
            elif plan.source.workflow_id != version.workflow_id:
                errors.append(
                    _error(
                        code="WORKFLOW_PLAN_SOURCE_MISMATCH",
                        message=(
                            "Workflow Plan source.workflow_id must equal the "
                            "owning Workflow id."
                        ),
                        details={
                            "expected_workflow_id": str(version.workflow_id),
                            "plan_workflow_id": str(plan.source.workflow_id),
                        },
                    )
                )
                checks["plan_schema"] = "ERROR"

            static_result = self._static_validator.validate(plan)
            if not static_result.ok:
                checks["static_plan"] = "ERROR"
                for issue in static_result.errors:
                    errors.append(
                        _error(
                            code=issue.code,
                            message=issue.message,
                            step_id=issue.step_id,
                            details=issue.details or None,
                        )
                    )
            dep_errors, tool_refs, tool_check, approval_check = (
                await self._collect_dependency_errors(plan)
            )
            errors.extend(dep_errors)
            checks["tool_versions"] = tool_check
            checks["approval_policies"] = approval_check
        else:
            checks["static_plan"] = "SKIPPED"
            checks["tool_versions"] = "SKIPPED"
            checks["approval_policies"] = "SKIPPED"

        errors = _sort_errors(errors)
        valid = len(errors) == 0
        report: dict[str, Any] = {
            "schema_version": "1.0",
            "valid": valid,
            "content_hash": version.content_hash,
            "errors": errors,
            "checks": checks,
        }
        return valid, report, tool_refs if valid else []

    async def _invalidate_draft_for_publish(
        self,
        version: WorkflowVersion,
        *,
        report: dict[str, Any] | None,
        message: str,
    ) -> None:
        """Persist INVALID + clear tool refs, then raise RESOURCE_CONFLICT."""

        await self._versions.set_validation(
            version,
            validation_status=str(WorkflowVersionValidationStatus.INVALID),
            validation_report=report,
        )
        await self._tool_refs.clear(version.id)
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=message,
            status_code=status.HTTP_409_CONFLICT,
        )

    async def validate(
        self, workflow_id: uuid.UUID, version_id: uuid.UUID
    ) -> WorkflowVersion:
        await self._require_workflow(workflow_id)
        version = await self._versions.lock_for_update(workflow_id, version_id)
        if version is None:
            raise AppError(
                code="NOT_FOUND",
                message="Workflow version not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if version.status != WorkflowVersionStatus.DRAFT:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Only DRAFT WorkflowVersion can be validated.",
                status_code=status.HTTP_409_CONFLICT,
            )

        # Hash exact persisted JSON — never coerce non-objects to {}.
        try:
            recomputed = workflow_version_content_hash(
                plan_schema_version=version.plan_schema_version,
                plan_definition=version.plan_definition,
                input_schema=version.input_schema,
                output_schema=version.output_schema,
                policy_defaults=version.policy_defaults,
            )
        except (TypeError, ValueError):
            report = {
                "schema_version": "1.0",
                "valid": False,
                "content_hash": version.content_hash,
                "errors": [
                    _error(
                        code="PLAN_DEFINITION_INVALID",
                        message=(
                            "WorkflowVersion executable content is not "
                            "JSON-hashable."
                        ),
                    )
                ],
                "checks": {
                    "plan_schema": "ERROR",
                    "static_plan": "SKIPPED",
                    "tool_versions": "SKIPPED",
                    "approval_policies": "SKIPPED",
                },
            }
            updated = await self._versions.set_validation(
                version,
                validation_status=str(WorkflowVersionValidationStatus.INVALID),
                validation_report=report,
            )
            await self._tool_refs.clear(updated.id)
            await self._session.commit()
            await self._session.refresh(updated)
            return updated

        if recomputed != version.content_hash:
            version.content_hash = recomputed

        valid, report, tool_refs = await self._build_validation(version)
        status_value = (
            WorkflowVersionValidationStatus.VALID
            if valid
            else WorkflowVersionValidationStatus.INVALID
        )
        updated = await self._versions.set_validation(
            version,
            validation_status=str(status_value),
            validation_report=report,
        )
        if valid:
            await self._tool_refs.replace_all(updated.id, tool_refs)
        else:
            await self._tool_refs.clear(updated.id)
        await self._session.commit()
        await self._session.refresh(updated)
        return updated

    async def _assert_publish_ready(
        self, version: WorkflowVersion
    ) -> list[tuple[str, uuid.UUID]]:
        """Stale-dependency recheck before publish. Returns expected tool refs.

        Any stale/malformed evidence invalidates the DRAFT before raising.
        """

        try:
            recomputed = workflow_version_content_hash(
                plan_schema_version=version.plan_schema_version,
                plan_definition=version.plan_definition,
                input_schema=version.input_schema,
                output_schema=version.output_schema,
                policy_defaults=version.policy_defaults,
            )
        except (TypeError, ValueError):
            await self._invalidate_draft_for_publish(
                version,
                report=None,
                message=(
                    "WorkflowVersion publish blocked by malformed executable "
                    "content."
                ),
            )

        if recomputed != version.content_hash:
            version.content_hash = recomputed
            _valid, report, _refs = await self._build_validation(version)
            await self._invalidate_draft_for_publish(
                version,
                report=report,
                message=(
                    "WorkflowVersion content_hash drifted from persisted fields."
                ),
            )

        report = version.validation_report or {}
        if (
            version.validation_status != WorkflowVersionValidationStatus.VALID
            or report.get("valid") is not True
            or report.get("content_hash") != version.content_hash
        ):
            _valid, fresh_report, _refs = await self._build_validation(version)
            await self._invalidate_draft_for_publish(
                version,
                report=fresh_report,
                message=(
                    "WorkflowVersion must be VALID with matching "
                    "validation_report.content_hash before publish."
                ),
            )

        valid, fresh_report, tool_refs = await self._build_validation(version)
        if not valid:
            await self._invalidate_draft_for_publish(
                version,
                report=fresh_report,
                message=(
                    "WorkflowVersion publish blocked by stale or invalid "
                    "dependencies."
                ),
            )

        existing = await self._tool_refs.list_for_version(version.id)
        existing_map = {r.step_key: r.mcp_tool_version_id for r in existing}
        expected_map = {k: v for k, v in tool_refs}
        if existing_map != expected_map:
            mismatch_report = {
                "schema_version": "1.0",
                "valid": False,
                "content_hash": version.content_hash,
                "errors": [
                    _error(
                        code="WORKFLOW_TOOL_REFS_MISMATCH",
                        message=(
                            "workflow_version_tool_refs do not match current "
                            "Plan TOOL projections."
                        ),
                    )
                ],
                "checks": dict(fresh_report.get("checks") or {}),
            }
            await self._invalidate_draft_for_publish(
                version,
                report=mismatch_report,
                message=(
                    "workflow_version_tool_refs do not match current Plan TOOL "
                    "projections."
                ),
            )
        return tool_refs


    async def publish(
        self, workflow_id: uuid.UUID, version_id: uuid.UUID
    ) -> WorkflowVersion:
        workflow = await self._workflows.lock_for_update(workflow_id)
        if workflow is None:
            raise AppError(
                code="NOT_FOUND",
                message="Workflow not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        version = await self._versions.lock_for_update(workflow_id, version_id)
        if version is None:
            raise AppError(
                code="NOT_FOUND",
                message="Workflow version not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        if version.status != WorkflowVersionStatus.DRAFT:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Only DRAFT WorkflowVersion can be published.",
                status_code=status.HTTP_409_CONFLICT,
            )

        try:
            await self._assert_publish_ready(version)
        except AppError:
            await self._session.commit()
            raise

        now = datetime.now(UTC)
        previous_id = workflow.current_version_id
        if previous_id is not None and previous_id != version.id:
            previous = await self._versions.lock_for_update(workflow_id, previous_id)
            if (
                previous is not None
                and previous.status == WorkflowVersionStatus.PUBLISHED
            ):
                await self._versions.mark_deprecated(previous, deprecated_at=now)

        published = await self._versions.mark_published(version, published_at=now)
        updated_workflow = await self._workflows.set_current_version(
            workflow_id,
            current_version_id=published.id,
            expected_lock_version=int(workflow.lock_version),
        )
        if updated_workflow is None:
            await self._session.rollback()
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Concurrent Workflow publish conflict; retry.",
                status_code=status.HTTP_409_CONFLICT,
            )

        await self._session.commit()
        await self._session.refresh(published)
        return published

    async def deprecate(
        self, workflow_id: uuid.UUID, version_id: uuid.UUID
    ) -> WorkflowVersion:
        workflow = await self._workflows.lock_for_update(workflow_id)
        if workflow is None:
            raise AppError(
                code="NOT_FOUND",
                message="Workflow not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        version = await self._versions.lock_for_update(workflow_id, version_id)
        if version is None:
            raise AppError(
                code="NOT_FOUND",
                message="Workflow version not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        if version.status == WorkflowVersionStatus.DEPRECATED:
            return version

        if version.status == WorkflowVersionStatus.DRAFT:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="DRAFT WorkflowVersion cannot be deprecated.",
                status_code=status.HTTP_409_CONFLICT,
            )

        if workflow.current_version_id == version.id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Cannot directly deprecate the Workflow current_version; "
                    "publish a newer version instead."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )

        if version.status != WorkflowVersionStatus.PUBLISHED:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Only PUBLISHED WorkflowVersion can be deprecated.",
                status_code=status.HTTP_409_CONFLICT,
            )

        updated = await self._versions.mark_deprecated(
            version, deprecated_at=datetime.now(UTC)
        )
        await self._session.commit()
        await self._session.refresh(updated)
        return updated
