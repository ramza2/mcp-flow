"""ApprovalPolicy admin service (docs/06 §13).

Authz: ACTIVE user + ``approval.policy.manage``.
Never gates on ``approval.decide`` (decide is for ApprovalRequest inbox only).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.approval.decision import validate_approver_scope
from app.core.errors import AppError
from app.domain.enums import (
    ApprovalDecisionMode,
    ApprovalPolicyStatus,
    UserStatus,
)
from app.models.approval import ApprovalPolicy
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.role import RoleRepository
from app.repositories.user import UserRepository
from app.schemas.approval_policy import ApprovalPolicyCreate, ApprovalPolicyUpdate
from app.services.authorization import AuthorizationResolver

_APPROVAL_POLICY_MANAGE = "approval.policy.manage"


class ApprovalPolicyService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._policies = ApprovalPolicyRepository(session)
        self._users = UserRepository(session)
        self._roles = RoleRepository(session)
        self._authz = AuthorizationResolver(session)

    async def _assert_manage(self, actor_user_id: uuid.UUID) -> None:
        user = await self._users.get(actor_user_id)
        if user is None or user.status != UserStatus.ACTIVE.value:
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Actor is not an ACTIVE user.",
                status_code=status.HTTP_403_FORBIDDEN,
            )
        if not await self._authz.has_permission(actor_user_id, _APPROVAL_POLICY_MANAGE):
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Missing approval.policy.manage permission.",
                status_code=status.HTTP_403_FORBIDDEN,
            )

    async def _require(self, policy_id: uuid.UUID) -> ApprovalPolicy:
        row = await self._policies.get(policy_id)
        if row is None:
            raise AppError(
                code="NOT_FOUND",
                message="ApprovalPolicy not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return row

    def _raise_version_conflict(self) -> None:
        raise AppError(
            code="RESOURCE_VERSION_CONFLICT",
            message="ApprovalPolicy lock_version does not match.",
            status_code=status.HTTP_409_CONFLICT,
        )

    def _normalize_approver_scope(
        self, scope: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Validate shape; return normalized scope (trimmed role_codes) or None/{}."""
        try:
            role_codes = validate_approver_scope(scope)
        except AppError as exc:
            raise AppError(
                code="VALIDATION_ERROR",
                message=str(exc.message).replace(
                    "ApprovalRequest approval_scope", "approver_scope"
                ),
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            ) from exc
        if role_codes is None:
            return scope if scope == {} else None
        return {"role_codes": role_codes}

    async def _assert_role_codes_exist(self, role_codes: list[str]) -> None:
        missing: list[str] = []
        for code in role_codes:
            role = await self._roles.get_by_code(code)
            if role is None:
                missing.append(code)
        if missing:
            raise AppError(
                code="VALIDATION_ERROR",
                message=(
                    "approver_scope.role_codes references unknown role code(s): "
                    + ", ".join(missing)
                ),
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

    async def _validate_scope_refs(
        self, scope: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        normalized = self._normalize_approver_scope(scope)
        if normalized is not None and "role_codes" in normalized:
            await self._assert_role_codes_exist(list(normalized["role_codes"]))
        return normalized

    @staticmethod
    def _validate_decision_pairing(
        *,
        decision_mode: str,
        required_approvals: int,
    ) -> None:
        if decision_mode not in {
            ApprovalDecisionMode.ANY.value,
            ApprovalDecisionMode.ALL.value,
            ApprovalDecisionMode.QUORUM.value,
        }:
            raise AppError(
                code="VALIDATION_ERROR",
                message=f"Unsupported decision_mode {decision_mode!r}.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        if required_approvals < 1:
            raise AppError(
                code="VALIDATION_ERROR",
                message="required_approvals must be >= 1.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        if (
            decision_mode == ApprovalDecisionMode.ANY.value
            and required_approvals != 1
        ):
            raise AppError(
                code="VALIDATION_ERROR",
                message="decision_mode ANY requires required_approvals=1.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

    async def list(
        self,
        *,
        actor_user_id: uuid.UUID,
        page: int = 1,
        page_size: int = 20,
        status_filter: str | None = None,
        q: str | None = None,
        sort: str = "-updated_at",
    ) -> tuple[list[ApprovalPolicy], int]:
        await self._assert_manage(actor_user_id)
        if status_filter:
            parts = [p.strip() for p in status_filter.split(",") if p.strip()]
            allowed = {s.value for s in ApprovalPolicyStatus}
            bad = [p for p in parts if p not in allowed]
            if bad:
                raise AppError(
                    code="VALIDATION_ERROR",
                    message=f"Unsupported status filter value(s): {', '.join(bad)}.",
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
        return await self._policies.list(
            page=page,
            page_size=page_size,
            status_filter=status_filter,
            q=q,
            sort=sort,
        )

    async def get(
        self, policy_id: uuid.UUID, *, actor_user_id: uuid.UUID
    ) -> ApprovalPolicy:
        await self._assert_manage(actor_user_id)
        return await self._require(policy_id)

    async def create(
        self, data: ApprovalPolicyCreate, *, actor_user_id: uuid.UUID
    ) -> ApprovalPolicy:
        await self._assert_manage(actor_user_id)
        mode = str(data.decision_mode)
        self._validate_decision_pairing(
            decision_mode=mode,
            required_approvals=data.required_approvals,
        )
        scope = await self._validate_scope_refs(data.approver_scope)
        if await self._policies.get_by_code(data.code) is not None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="ApprovalPolicy code is already in use.",
                status_code=status.HTTP_409_CONFLICT,
            )
        try:
            row = await self._policies.create(
                code=data.code,
                name=data.name,
                description=data.description,
                status=ApprovalPolicyStatus.ACTIVE.value,
                decision_mode=mode,
                required_approvals=data.required_approvals,
                approver_scope=scope,
                default_expiry_seconds=data.default_expiry_seconds,
                allow_self_approval=data.allow_self_approval,
                reject_comment_required=data.reject_comment_required,
            )
            await self._session.commit()
            await self._session.refresh(row)
            return row
        except IntegrityError as exc:
            await self._session.rollback()
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="ApprovalPolicy code is already in use.",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc

    async def update(
        self,
        policy_id: uuid.UUID,
        data: ApprovalPolicyUpdate,
        *,
        actor_user_id: uuid.UUID,
        expected_lock_version: int,
    ) -> ApprovalPolicy:
        await self._assert_manage(actor_user_id)
        current = await self._require(policy_id)

        fields: dict[str, Any] = {}
        if "name" in data.model_fields_set:
            fields["name"] = data.name
        if "description" in data.model_fields_set:
            fields["description"] = data.description
        if "decision_mode" in data.model_fields_set:
            fields["decision_mode"] = str(data.decision_mode)
        if "required_approvals" in data.model_fields_set:
            fields["required_approvals"] = data.required_approvals
        if "approver_scope" in data.model_fields_set:
            fields["approver_scope"] = await self._validate_scope_refs(
                data.approver_scope
            )
        if "default_expiry_seconds" in data.model_fields_set:
            fields["default_expiry_seconds"] = data.default_expiry_seconds
        if "allow_self_approval" in data.model_fields_set:
            fields["allow_self_approval"] = data.allow_self_approval
        if "reject_comment_required" in data.model_fields_set:
            fields["reject_comment_required"] = data.reject_comment_required

        mode = fields.get("decision_mode", current.decision_mode)
        required = fields.get("required_approvals", current.required_approvals)
        self._validate_decision_pairing(
            decision_mode=str(mode),
            required_approvals=int(required),
        )

        if not fields:
            if current.lock_version != expected_lock_version:
                self._raise_version_conflict()
            return current

        updated = await self._policies.update_atomic(
            policy_id,
            expected_lock_version=expected_lock_version,
            **fields,
        )
        if updated is None:
            # Distinguish missing vs stale lock.
            again = await self._policies.get(policy_id)
            if again is None:
                raise AppError(
                    code="NOT_FOUND",
                    message="ApprovalPolicy not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            self._raise_version_conflict()
        await self._session.commit()
        await self._session.refresh(updated)
        return updated

    async def activate(
        self, policy_id: uuid.UUID, *, actor_user_id: uuid.UUID
    ) -> ApprovalPolicy:
        await self._assert_manage(actor_user_id)
        current = await self._require(policy_id)
        if current.status == ApprovalPolicyStatus.ACTIVE.value:
            return current
        if current.status != ApprovalPolicyStatus.INACTIVE.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"Cannot activate ApprovalPolicy in status {current.status}.",
                status_code=status.HTTP_409_CONFLICT,
            )
        # Re-validate scope role refs before returning to ACTIVE.
        await self._validate_scope_refs(current.approver_scope)
        updated = await self._policies.set_status_atomic(
            policy_id,
            expected_lock_version=current.lock_version,
            status=ApprovalPolicyStatus.ACTIVE.value,
        )
        if updated is None:
            self._raise_version_conflict()
        await self._session.commit()
        await self._session.refresh(updated)
        return updated

    async def deactivate(
        self, policy_id: uuid.UUID, *, actor_user_id: uuid.UUID
    ) -> ApprovalPolicy:
        await self._assert_manage(actor_user_id)
        current = await self._require(policy_id)
        if current.status == ApprovalPolicyStatus.INACTIVE.value:
            return current
        if current.status != ApprovalPolicyStatus.ACTIVE.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"Cannot deactivate ApprovalPolicy in status {current.status}."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )

        tool_refs = await self._policies.count_tool_policy_references(policy_id)
        if tool_refs > 0:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Cannot deactivate ApprovalPolicy while ToolPolicy rows "
                    "require_approval with this policy."
                ),
                status_code=status.HTTP_409_CONFLICT,
                details=[{"tool_policy_references": tool_refs}],
            )
        pending_refs = await self._policies.count_pending_request_references(policy_id)
        if pending_refs > 0:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Cannot deactivate ApprovalPolicy while PENDING ApprovalRequest "
                    "rows reference it."
                ),
                status_code=status.HTTP_409_CONFLICT,
                details=[{"pending_approval_request_references": pending_refs}],
            )
        workflow_refs = await self._policies.count_live_workflow_approval_references(
            policy_id
        )
        if workflow_refs > 0:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Cannot deactivate ApprovalPolicy while DRAFT/PUBLISHED "
                    "WorkflowVersion APPROVAL steps reference it."
                ),
                status_code=status.HTTP_409_CONFLICT,
                details=[{"live_workflow_approval_references": workflow_refs}],
            )

        updated = await self._policies.set_status_atomic(
            policy_id,
            expected_lock_version=current.lock_version,
            status=ApprovalPolicyStatus.INACTIVE.value,
        )
        if updated is None:
            self._raise_version_conflict()
        await self._session.commit()
        await self._session.refresh(updated)
        return updated
