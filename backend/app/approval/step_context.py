"""Authorable APPROVAL Step checkpoint context (approval_step_context.v1).

Distinct from ToolPolicy ``approval_context.v1``. Plan checkpoint only —
does not authorize downstream TOOL invocation.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from app.core.canonical_hash import compute_canonical_json_hash
from app.core.errors import AppError
from app.domain.enums import ApprovalPolicyStatus, AuthorableStepType
from app.models.approval import ApprovalPolicy
from app.models.execution import Execution, ExecutionStep
from app.schemas.execution_plan import (
    ApprovalStepConfigV1,
    ExecutionPlanStep,
    ExecutionPlanV1,
)

APPROVAL_STEP_CONTEXT_SCHEMA_VERSION = "approval_step_context.v1"
APPROVAL_KIND_AUTHORABLE_STEP = "AUTHORABLE_STEP"


def hash_result_inline(result_inline: Any) -> str | None:
    """Canonical hash of durable result_inline; null when absent."""
    if result_inline is None:
        return None
    return compute_canonical_json_hash(result_inline)


def _approval_policy_snapshot(policy: ApprovalPolicy) -> dict[str, Any]:
    return {
        "id": str(policy.id),
        "status": policy.status,
        "decision_mode": policy.decision_mode,
        "required_approvals": policy.required_approvals,
        "approver_scope": (
            dict(policy.approver_scope) if policy.approver_scope is not None else None
        ),
        "default_expiry_seconds": policy.default_expiry_seconds,
        "allow_self_approval": policy.allow_self_approval,
        "reject_comment_required": policy.reject_comment_required,
    }


def _transitive_ancestor_keys(
    plan: ExecutionPlanV1, step_key: str
) -> list[str]:
    """Transitive dependency ancestors in immutable Plan order."""
    ids = {s.id for s in plan.steps}
    deps = {s.id: [d for d in s.depends_on if d in ids] for s in plan.steps}
    cache: dict[str, set[str]] = {}

    def walk(sid: str) -> set[str]:
        if sid in cache:
            return cache[sid]
        out: set[str] = set()
        for dep in deps.get(sid, []):
            out.add(dep)
            out |= walk(dep)
        cache[sid] = out
        return out

    ancestors = walk(step_key)
    return [ps.id for ps in plan.steps if ps.id in ancestors]


def build_upstream_evidence(
    *,
    plan: ExecutionPlanV1,
    owning_step_key: str,
    by_key: Mapping[str, ExecutionStep],
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for key in _transitive_ancestor_keys(plan, owning_step_key):
        step = by_key[key]
        evidence.append(
            {
                "step_key": step.step_key,
                "step_type": step.step_type,
                "status": step.status,
                "condition_result": step.condition_result,
                "error_code": step.error_code,
                "result_inline_hash": hash_result_inline(step.result_inline),
            }
        )
    return evidence


def build_approval_step_context_snapshot(
    *,
    execution: Execution,
    step: ExecutionStep,
    plan: ExecutionPlanV1,
    approval_policy: ApprovalPolicy,
    steps: Sequence[ExecutionStep],
) -> dict[str, Any]:
    """Build deterministic authorable APPROVAL checkpoint context."""
    if step.step_type != AuthorableStepType.APPROVAL.value:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="Authorable approval context requires APPROVAL Step.",
            status_code=409,
        )
    if step.mcp_tool_version_id is not None:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="APPROVAL Step must have null mcp_tool_version_id.",
            status_code=409,
        )
    if approval_policy.status != ApprovalPolicyStatus.ACTIVE.value:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="ApprovalPolicy must be ACTIVE for authorable APPROVAL.",
            status_code=409,
        )
    if approval_policy.default_expiry_seconds <= 0:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="ApprovalPolicy.default_expiry_seconds must be > 0.",
            status_code=409,
        )

    try:
        plan_step = ExecutionPlanStep.model_validate(step.step_snapshot)
        cfg = ApprovalStepConfigV1.model_validate(plan_step.config)
    except Exception as exc:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="APPROVAL Step snapshot/config is invalid.",
            status_code=409,
        ) from exc
    if plan_step.model_dump(mode="json") != step.step_snapshot:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="APPROVAL step_snapshot drift vs Plan projection.",
            status_code=409,
        )
    expected = next((p for p in plan.steps if p.id == step.step_key), None)
    if expected is None or expected.model_dump(mode="json") != step.step_snapshot:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="APPROVAL Step is not an exact immutable Plan projection.",
            status_code=409,
        )
    if cfg.approval_policy_id != approval_policy.id:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="ApprovalPolicy id does not match APPROVAL Step config.",
            status_code=409,
        )

    by_key = {s.step_key: s for s in steps}
    return {
        "schema_version": APPROVAL_STEP_CONTEXT_SCHEMA_VERSION,
        "approval_kind": APPROVAL_KIND_AUTHORABLE_STEP,
        "execution_id": str(execution.id),
        "step_execution_id": str(step.id),
        "step_key": step.step_key,
        "requester_id": str(execution.requester_id),
        "source_type": execution.source_type,
        "trigger_type": execution.trigger_type,
        "agent_version_id": (
            str(execution.agent_version_id)
            if execution.agent_version_id is not None
            else None
        ),
        "workflow_version_id": (
            str(execution.workflow_version_id)
            if execution.workflow_version_id is not None
            else None
        ),
        "plan_hash": execution.plan_hash,
        "approval_policy": _approval_policy_snapshot(approval_policy),
        "upstream_evidence": build_upstream_evidence(
            plan=plan, owning_step_key=step.step_key, by_key=by_key
        ),
    }


def compute_approval_step_context_hash(context_snapshot: dict[str, Any]) -> str:
    return compute_canonical_json_hash(context_snapshot)


def validate_stored_approval_step_context(snapshot: Any) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ApprovalRequest context_snapshot is corrupted.",
            status_code=409,
        )
    if snapshot.get("schema_version") != APPROVAL_STEP_CONTEXT_SCHEMA_VERSION:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ApprovalRequest context_snapshot schema is invalid.",
            status_code=409,
        )
    if snapshot.get("approval_kind") != APPROVAL_KIND_AUTHORABLE_STEP:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Authorable ApprovalRequest approval_kind is invalid.",
            status_code=409,
        )
    required = (
        "execution_id",
        "step_execution_id",
        "step_key",
        "requester_id",
        "source_type",
        "trigger_type",
        "agent_version_id",
        "workflow_version_id",
        "plan_hash",
        "approval_policy",
        "upstream_evidence",
    )
    for key in required:
        if key not in snapshot:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"ApprovalRequest context_snapshot missing {key!r}.",
                status_code=409,
            )
    approval_policy = snapshot["approval_policy"]
    if not isinstance(approval_policy, dict):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ApprovalRequest approval_policy snapshot is corrupted.",
            status_code=409,
        )
    for key in (
        "id",
        "status",
        "decision_mode",
        "required_approvals",
        "approver_scope",
        "default_expiry_seconds",
        "allow_self_approval",
        "reject_comment_required",
    ):
        if key not in approval_policy:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"ApprovalRequest context_snapshot missing approval_policy.{key}.",
                status_code=409,
            )
    if not isinstance(snapshot["upstream_evidence"], list):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ApprovalRequest upstream_evidence must be a list.",
            status_code=409,
        )
    return snapshot


def assert_approval_step_lineage_matches(
    *,
    execution: Execution,
    step: ExecutionStep,
    snapshot: dict[str, Any],
) -> None:
    try:
        snap_exec = uuid.UUID(str(snapshot["execution_id"]))
        snap_step = uuid.UUID(str(snapshot["step_execution_id"]))
        snap_requester = uuid.UUID(str(snapshot["requester_id"]))
    except (TypeError, ValueError) as exc:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ApprovalRequest context_snapshot lineage ids are corrupted.",
            status_code=409,
        ) from exc
    snap_agent = snapshot.get("agent_version_id")
    snap_workflow = snapshot.get("workflow_version_id")
    agent_ok = (
        (snap_agent is None and execution.agent_version_id is None)
        or (
            snap_agent is not None
            and execution.agent_version_id is not None
            and uuid.UUID(str(snap_agent)) == execution.agent_version_id
        )
    )
    workflow_ok = (
        (snap_workflow is None and execution.workflow_version_id is None)
        or (
            snap_workflow is not None
            and execution.workflow_version_id is not None
            and uuid.UUID(str(snap_workflow)) == execution.workflow_version_id
        )
    )
    if (
        snap_exec != execution.id
        or snap_step != step.id
        or snap_requester != execution.requester_id
        or not agent_ok
        or not workflow_ok
        or snapshot.get("step_key") != step.step_key
        or snapshot.get("plan_hash") != execution.plan_hash
        or snapshot.get("source_type") != execution.source_type
        or snapshot.get("trigger_type") != execution.trigger_type
        or step.step_type != AuthorableStepType.APPROVAL.value
    ):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                "Authorable ApprovalRequest context_snapshot lineage does not "
                "match Execution/Step."
            ),
            status_code=409,
        )


def project_safe_approval_step_context(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Safe historical projection for approval query APIs."""
    upstream = snapshot.get("upstream_evidence") or []
    safe_upstream: list[dict[str, Any]] = []
    if isinstance(upstream, list):
        for item in upstream:
            if not isinstance(item, dict):
                continue
            safe_upstream.append(
                {
                    "step_key": item.get("step_key"),
                    "status": item.get("status"),
                }
            )
    policy = snapshot.get("approval_policy") or {}
    policy_id = policy.get("id") if isinstance(policy, dict) else None
    return {
        "approval_kind": APPROVAL_KIND_AUTHORABLE_STEP,
        "step_key": snapshot.get("step_key"),
        "approval_policy_id": policy_id,
        "upstream_steps": safe_upstream,
    }
