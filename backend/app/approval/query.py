"""Approval query / pending inbox (GET list + detail) — no mutations."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.approval.decision import validate_approver_scope
from app.approval.evidence import (
    assert_request_context_hash_intact,
    snapshotted_allow_self_approval,
)
from app.core.errors import AppError
from app.domain.enums import (
    ApprovalDecisionValue,
    ApprovalStatus,
    UserStatus,
)
from app.models.approval import ApprovalRequest
from app.repositories.approval_decision import ApprovalDecisionRepository
from app.repositories.approval_request import ApprovalRequestRepository
from app.repositories.role import UserRoleRepository
from app.repositories.user import UserRepository
from app.services.authorization import AuthorizationResolver

ApprovalStatusLiteral = Literal[
    "PENDING", "APPROVED", "REJECTED", "EXPIRED", "CANCELLED"
]

ALLOWED_APPROVAL_SORT_FIELDS = frozenset({"requested_at", "expires_at"})
SortDirection = Literal["asc", "desc"]


@dataclass(frozen=True, slots=True)
class ApprovalListItem:
    id: uuid.UUID
    status: str
    execution_id: uuid.UUID
    step_execution_id: uuid.UUID
    requested_by: uuid.UUID
    decision_mode: str
    required_approvals: int
    requested_at: datetime
    expires_at: datetime
    resolved_at: datetime | None
    approve_count: int
    reject_count: int
    can_decide: bool


@dataclass(frozen=True, slots=True)
class ApprovalDecisionItem:
    decision_id: uuid.UUID
    decided_by: uuid.UUID
    decision: str
    comment: str | None
    decided_at: datetime


@dataclass(frozen=True, slots=True)
class ApprovalDetail:
    item: ApprovalListItem
    safe_context: dict[str, Any]
    decisions: list[ApprovalDecisionItem]


@dataclass(frozen=True, slots=True)
class ApprovalListResult:
    items: list[ApprovalListItem]
    page: int
    page_size: int
    total: int
    has_next: bool


def parse_approval_sort(sort: str) -> tuple[str, SortDirection]:
    """Strict sort parser — invalid values raise 422 (no silent fallback)."""
    raw = (sort or "expires_at").strip()
    if not raw:
        return "expires_at", "asc"
    direction: SortDirection = "asc"
    field = raw
    if raw.startswith("-"):
        direction = "desc"
        field = raw[1:]
    elif raw.startswith("+"):
        direction = "asc"
        field = raw[1:]
    if field not in ALLOWED_APPROVAL_SORT_FIELDS:
        raise AppError(
            code="VALIDATION_ERROR",
            message=(
                "Invalid sort. Allowed: requested_at, -requested_at, "
                "expires_at, -expires_at."
            ),
            status_code=422,
        )
    return field, direction


def parse_approval_status(status: str | None) -> str:
    """Default PENDING; invalid canonical status → 422."""
    value = (status or ApprovalStatus.PENDING.value).strip()
    allowed = {s.value for s in ApprovalStatus}
    if value not in allowed:
        raise AppError(
            code="VALIDATION_ERROR",
            message=(
                "Invalid status. Allowed: PENDING, APPROVED, REJECTED, "
                "EXPIRED, CANCELLED."
            ),
            status_code=422,
        )
    return value


def mask_resolved_input(resolved_input: Any) -> dict[str, Any]:
    """Project resolved_input with SECRET_REF → {kind, masked:true} only."""
    if not isinstance(resolved_input, dict):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ApprovalRequest resolved_input is corrupted.",
            status_code=409,
        )
    out: dict[str, Any] = {}
    for key, value in resolved_input.items():
        if isinstance(value, dict) and value.get("kind") == "SECRET_REF":
            out[key] = {"kind": "SECRET_REF", "masked": True}
        else:
            out[key] = value
    return out


def project_safe_context(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Explicit historical context projection — never raw snapshot/hash/secrets."""
    try:
        tool_version = snapshot["mcp_tool_version_id"]
        step_key = snapshot["step_key"]
        risk_class = snapshot["risk_class"]
        resolved = snapshot["resolved_input"]
    except KeyError as exc:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ApprovalRequest context_snapshot is missing projection fields.",
            status_code=409,
        ) from exc
    return {
        "step_key": step_key,
        "mcp_tool_version_id": tool_version,
        "risk_class": risk_class,
        "resolved_input": mask_resolved_input(resolved),
    }


def compute_can_decide(
    *,
    request: ApprovalRequest,
    actor_user_id: uuid.UUID,
    actor_has_decision: bool,
    now: datetime,
) -> bool:
    if request.status != ApprovalStatus.PENDING.value:
        return False
    expires_at = request.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= now:
        return False
    if actor_has_decision:
        return False
    return True


class ApprovalQueryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._requests = ApprovalRequestRepository(session)
        self._decisions = ApprovalDecisionRepository(session)
        self._users = UserRepository(session)
        self._user_roles = UserRoleRepository(session)
        self._authz = AuthorizationResolver(session)

    async def list_for_actor(
        self,
        *,
        actor_user_id: uuid.UUID,
        status: str | None = None,
        execution_id: uuid.UUID | None = None,
        requested_by: uuid.UUID | None = None,
        page: int = 1,
        page_size: int = 20,
        sort: str = "expires_at",
        now: datetime | None = None,
    ) -> ApprovalListResult:
        ts = _aware_now(now)
        await self._assert_actor_authorized(actor_user_id)
        status_value = parse_approval_status(status)
        sort_field, sort_dir = parse_approval_sort(sort)
        actor_role_codes = await self._actor_role_codes(actor_user_id)

        rows, total = await self._requests.list_visible_for_actor(
            actor_user_id=actor_user_id,
            actor_role_codes=actor_role_codes,
            status=status_value,
            actionable_pending=(status_value == ApprovalStatus.PENDING.value),
            execution_id=execution_id,
            requested_by=requested_by,
            page=page,
            page_size=page_size,
            sort_field=sort_field,
            sort_dir=sort_dir,
            now=ts,
        )
        items = await self._project_list_items(
            rows, actor_user_id=actor_user_id, now=ts
        )
        return ApprovalListResult(
            items=items,
            page=page,
            page_size=page_size,
            total=total,
            has_next=page * page_size < total,
        )

    async def get_for_actor(
        self,
        *,
        approval_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        now: datetime | None = None,
    ) -> ApprovalDetail:
        ts = _aware_now(now)
        await self._assert_actor_authorized(actor_user_id)
        actor_role_codes = await self._actor_role_codes(actor_user_id)

        request = await self._requests.get_visible_for_actor(
            approval_id=approval_id,
            actor_user_id=actor_user_id,
            actor_role_codes=actor_role_codes,
        )
        if request is None:
            # IDOR / not eligible → 404 (do not reveal existence via 403).
            raise AppError(
                code="NOT_FOUND",
                message="ApprovalRequest not found.",
                status_code=404,
            )

        # Re-validate scope/self with the same Python semantics as decide.
        try:
            required = validate_approver_scope(request.approval_scope)
        except AppError:
            raise AppError(
                code="NOT_FOUND",
                message="ApprovalRequest not found.",
                status_code=404,
            ) from None
        if required is not None and not set(actor_role_codes).intersection(required):
            raise AppError(
                code="NOT_FOUND",
                message="ApprovalRequest not found.",
                status_code=404,
            )

        snapshot = assert_request_context_hash_intact(request)
        allow_self = snapshotted_allow_self_approval(snapshot)
        if not allow_self and actor_user_id == request.requested_by:
            raise AppError(
                code="NOT_FOUND",
                message="ApprovalRequest not found.",
                status_code=404,
            )

        decisions = await self._decisions.list_for_request(request.id)
        actor_has = any(d.decided_by == actor_user_id for d in decisions)
        approve_count = sum(
            1 for d in decisions if d.decision == ApprovalDecisionValue.APPROVE.value
        )
        reject_count = sum(
            1 for d in decisions if d.decision == ApprovalDecisionValue.REJECT.value
        )
        item = ApprovalListItem(
            id=request.id,
            status=request.status,
            execution_id=request.execution_id,
            step_execution_id=request.step_execution_id,
            requested_by=request.requested_by,
            decision_mode=request.decision_mode,
            required_approvals=request.required_approvals,
            requested_at=request.requested_at,
            expires_at=request.expires_at,
            resolved_at=request.resolved_at,
            approve_count=approve_count,
            reject_count=reject_count,
            can_decide=compute_can_decide(
                request=request,
                actor_user_id=actor_user_id,
                actor_has_decision=actor_has,
                now=ts,
            ),
        )
        return ApprovalDetail(
            item=item,
            safe_context=project_safe_context(snapshot),
            decisions=[
                ApprovalDecisionItem(
                    decision_id=d.id,
                    decided_by=d.decided_by,
                    decision=d.decision,
                    comment=d.comment,
                    decided_at=d.decided_at,
                )
                for d in decisions
            ],
        )

    async def _project_list_items(
        self,
        rows: list[ApprovalRequest],
        *,
        actor_user_id: uuid.UUID,
        now: datetime,
    ) -> list[ApprovalListItem]:
        if not rows:
            return []
        stats = await self._decisions.counts_and_actor_flags(
            approval_request_ids=[r.id for r in rows],
            actor_user_id=actor_user_id,
        )
        items: list[ApprovalListItem] = []
        for row in rows:
            approve_count, reject_count, actor_has = stats.get(
                row.id, (0, 0, False)
            )
            items.append(
                ApprovalListItem(
                    id=row.id,
                    status=row.status,
                    execution_id=row.execution_id,
                    step_execution_id=row.step_execution_id,
                    requested_by=row.requested_by,
                    decision_mode=row.decision_mode,
                    required_approvals=row.required_approvals,
                    requested_at=row.requested_at,
                    expires_at=row.expires_at,
                    resolved_at=row.resolved_at,
                    approve_count=approve_count,
                    reject_count=reject_count,
                    can_decide=compute_can_decide(
                        request=row,
                        actor_user_id=actor_user_id,
                        actor_has_decision=actor_has,
                        now=now,
                    ),
                )
            )
        return items

    async def _assert_actor_authorized(self, actor_user_id: uuid.UUID) -> None:
        user = await self._users.get(actor_user_id)
        if user is None or user.status != UserStatus.ACTIVE.value:
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Actor is not an ACTIVE user.",
                status_code=403,
            )
        if not await self._authz.has_permission(actor_user_id, "approval.decide"):
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Missing approval.decide permission.",
                status_code=403,
            )

    async def _actor_role_codes(self, actor_user_id: uuid.UUID) -> list[str]:
        roles = await self._user_roles.list_roles(actor_user_id)
        return [role.code for role in roles]


def _aware_now(now: datetime | None) -> datetime:
    ts = now or datetime.now(UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts
