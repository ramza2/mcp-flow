"""ApprovalPolicy repository — admin API + runtime lookups."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    ApprovalStatus,
    AuthorableStepType,
    WorkflowVersionStatus,
)
from app.models.approval import ApprovalPolicy, ApprovalRequest
from app.models.mcp import MCPToolPolicy
from app.models.workflow import WorkflowVersion
from app.schemas.common_page import parse_sort

ALLOWED_APPROVAL_POLICY_SORT = frozenset(
    {"name", "updated_at", "created_at", "status", "code"}
)
_LIVE_WORKFLOW_VERSION_STATUSES = frozenset(
    {
        WorkflowVersionStatus.DRAFT.value,
        WorkflowVersionStatus.PUBLISHED.value,
    }
)


class ApprovalPolicyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _base(self) -> Select[tuple[ApprovalPolicy]]:
        return select(ApprovalPolicy)

    async def get(self, policy_id: uuid.UUID) -> ApprovalPolicy | None:
        stmt = self._base().where(ApprovalPolicy.id == policy_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_code(self, code: str) -> ApprovalPolicy | None:
        stmt = self._base().where(ApprovalPolicy.code == code)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status_filter: str | None = None,
        q: str | None = None,
        sort: str = "-updated_at",
    ) -> tuple[list[ApprovalPolicy], int]:
        field, direction = parse_sort(sort, allowed=set(ALLOWED_APPROVAL_POLICY_SORT))
        stmt = self._base()

        if status_filter:
            statuses = [part.strip() for part in status_filter.split(",") if part.strip()]
            if len(statuses) == 1:
                stmt = stmt.where(ApprovalPolicy.status == statuses[0])
            elif statuses:
                stmt = stmt.where(ApprovalPolicy.status.in_(statuses))

        if q:
            pattern = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(
                    ApprovalPolicy.name.ilike(pattern),
                    ApprovalPolicy.code.ilike(pattern),
                    ApprovalPolicy.description.ilike(pattern),
                )
            )

        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())

        order_col = getattr(ApprovalPolicy, field)
        order_expr = order_col.desc() if direction == "desc" else order_col.asc()
        stmt = (
            stmt.order_by(order_expr, ApprovalPolicy.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        return rows, total

    async def create(
        self,
        *,
        code: str,
        name: str,
        description: str | None = None,
        status: str = "ACTIVE",
        decision_mode: str = "ANY",
        required_approvals: int = 1,
        approver_scope: dict[str, Any] | None = None,
        default_expiry_seconds: int = 3600,
        allow_self_approval: bool = False,
        reject_comment_required: bool = False,
    ) -> ApprovalPolicy:
        row = ApprovalPolicy(
            code=code,
            name=name,
            description=description,
            status=status,
            decision_mode=decision_mode,
            required_approvals=required_approvals,
            approver_scope=approver_scope,
            default_expiry_seconds=default_expiry_seconds,
            allow_self_approval=allow_self_approval,
            reject_comment_required=reject_comment_required,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def update_atomic(
        self,
        policy_id: uuid.UUID,
        *,
        expected_lock_version: int,
        **fields: Any,
    ) -> ApprovalPolicy | None:
        values = {
            key: value
            for key, value in fields.items()
            if hasattr(ApprovalPolicy, key)
            and key
            not in {
                "id",
                "code",
                "status",
                "lock_version",
                "created_at",
            }
        }
        values["lock_version"] = ApprovalPolicy.lock_version + 1
        values["updated_at"] = func.now()
        stmt = (
            update(ApprovalPolicy)
            .where(
                ApprovalPolicy.id == policy_id,
                ApprovalPolicy.lock_version == expected_lock_version,
            )
            .values(**values)
            .returning(ApprovalPolicy)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def set_status_atomic(
        self,
        policy_id: uuid.UUID,
        *,
        expected_lock_version: int,
        status: str,
    ) -> ApprovalPolicy | None:
        stmt = (
            update(ApprovalPolicy)
            .where(
                ApprovalPolicy.id == policy_id,
                ApprovalPolicy.lock_version == expected_lock_version,
            )
            .values(
                status=status,
                lock_version=ApprovalPolicy.lock_version + 1,
                updated_at=func.now(),
            )
            .returning(ApprovalPolicy)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def count_tool_policy_references(self, policy_id: uuid.UUID) -> int:
        stmt = select(func.count()).select_from(MCPToolPolicy).where(
            MCPToolPolicy.approval_policy_id == policy_id,
            MCPToolPolicy.requires_approval.is_(True),
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def count_pending_request_references(self, policy_id: uuid.UUID) -> int:
        stmt = select(func.count()).select_from(ApprovalRequest).where(
            ApprovalRequest.approval_policy_id == policy_id,
            ApprovalRequest.status == ApprovalStatus.PENDING.value,
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def count_live_workflow_approval_references(
        self, policy_id: uuid.UUID
    ) -> int:
        """Count DRAFT/PUBLISHED WorkflowVersions with APPROVAL Step refs.

        DEPRECATED versions are historical and intentionally ignored so past
        history does not block deactivate.
        """
        stmt = select(
            WorkflowVersion.id,
            WorkflowVersion.plan_definition,
        ).where(WorkflowVersion.status.in_(sorted(_LIVE_WORKFLOW_VERSION_STATUSES)))
        rows = list((await self._session.execute(stmt)).all())
        target = str(policy_id)
        matched = 0
        for _version_id, plan in rows:
            if self._plan_references_approval_policy(plan, target):
                matched += 1
        return matched

    @staticmethod
    def _plan_references_approval_policy(plan: Any, policy_id: str) -> bool:
        if not isinstance(plan, dict):
            return False
        steps = plan.get("steps")
        if not isinstance(steps, list):
            return False
        for step in steps:
            if not isinstance(step, dict):
                continue
            if step.get("type") != AuthorableStepType.APPROVAL.value:
                continue
            config = step.get("config")
            if not isinstance(config, dict):
                continue
            raw = config.get("approval_policy_id")
            if raw is None:
                continue
            if str(raw) == policy_id:
                return True
        return False
