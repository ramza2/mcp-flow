"""Authorable APPROVAL Step checkpoint context (approval_step_context.v1).

Distinct from ToolPolicy ``approval_context.v1``. Plan checkpoint only —
does not authorize downstream TOOL invocation.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from app.core.canonical_hash import compute_canonical_json_hash
from app.core.errors import AppError
from app.domain.enums import (
    ApprovalDecisionMode,
    ApprovalPolicyStatus,
    AuthorableStepType,
    StepStatus,
)
from app.models.approval import ApprovalPolicy
from app.models.execution import Execution, ExecutionStep
from app.schemas.execution_plan import (
    ApprovalStepConfigV1,
    ExecutionPlanStep,
    ExecutionPlanV1,
)

APPROVAL_STEP_CONTEXT_SCHEMA_VERSION = "approval_step_context.v1"
APPROVAL_KIND_AUTHORABLE_STEP = "AUTHORABLE_STEP"

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_UPSTREAM_EVIDENCE_FIELDS = frozenset(
    {
        "step_key",
        "step_type",
        "status",
        "condition_result",
        "error_code",
        "result_inline_hash",
    }
)
_CANONICAL_STEP_TYPES = frozenset(t.value for t in AuthorableStepType)
_CANONICAL_STEP_STATUSES = frozenset(s.value for s in StepStatus)
_POLICY_SNAPSHOT_FIELDS = (
    "id",
    "status",
    "decision_mode",
    "required_approvals",
    "approver_scope",
    "default_expiry_seconds",
    "allow_self_approval",
    "reject_comment_required",
)


def _parse_context_uuid(value: Any, *, field: str) -> uuid.UUID:
    """Fail closed for non-null malformed UUID-bearing context fields."""
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=f"ApprovalRequest context_snapshot {field} is corrupted.",
            status_code=409,
        ) from exc


def _parse_optional_context_uuid(value: Any, *, field: str) -> uuid.UUID | None:
    if value is None:
        return None
    return _parse_context_uuid(value, field=field)


def _require_positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=f"ApprovalRequest context_snapshot {field} is corrupted.",
            status_code=409,
        )
    return value


def _require_bool(value: Any, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=f"ApprovalRequest context_snapshot {field} is corrupted.",
            status_code=409,
        )
    return value


def _validate_approval_policy_snapshot(approval_policy: Any) -> dict[str, Any]:
    if not isinstance(approval_policy, dict):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ApprovalRequest approval_policy snapshot is corrupted.",
            status_code=409,
        )
    if set(approval_policy.keys()) != set(_POLICY_SNAPSHOT_FIELDS):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ApprovalRequest approval_policy snapshot fields are corrupted.",
            status_code=409,
        )
    _parse_context_uuid(approval_policy["id"], field="approval_policy.id")
    status = approval_policy["status"]
    if status not in {
        ApprovalPolicyStatus.ACTIVE.value,
        ApprovalPolicyStatus.INACTIVE.value,
    }:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ApprovalRequest approval_policy.status is corrupted.",
            status_code=409,
        )
    mode = approval_policy["decision_mode"]
    if mode not in {
        ApprovalDecisionMode.ANY.value,
        ApprovalDecisionMode.ALL.value,
        ApprovalDecisionMode.QUORUM.value,
    }:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ApprovalRequest approval_policy.decision_mode is corrupted.",
            status_code=409,
        )
    _require_positive_int(
        approval_policy["required_approvals"],
        field="approval_policy.required_approvals",
    )
    expiry = approval_policy["default_expiry_seconds"]
    if isinstance(expiry, bool) or not isinstance(expiry, int) or expiry <= 0:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                "ApprovalRequest approval_policy.default_expiry_seconds is corrupted."
            ),
            status_code=409,
        )
    _require_bool(
        approval_policy["allow_self_approval"],
        field="approval_policy.allow_self_approval",
    )
    _require_bool(
        approval_policy["reject_comment_required"],
        field="approval_policy.reject_comment_required",
    )
    # Supported shapes only; unknown keys / malformed types fail closed.
    # Lazy import avoids approval.decision ↔ evidence ↔ step_context cycle.
    from app.approval.decision import validate_approver_scope

    validate_approver_scope(approval_policy["approver_scope"])
    return approval_policy


def _validate_upstream_evidence_item(item: Any, *, index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=f"ApprovalRequest upstream_evidence[{index}] is corrupted.",
            status_code=409,
        )
    if set(item.keys()) != _UPSTREAM_EVIDENCE_FIELDS:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"ApprovalRequest upstream_evidence[{index}] fields are corrupted."
            ),
            status_code=409,
        )
    step_key = item["step_key"]
    if not isinstance(step_key, str) or not step_key:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"ApprovalRequest upstream_evidence[{index}].step_key is corrupted."
            ),
            status_code=409,
        )
    step_type = item["step_type"]
    if step_type not in _CANONICAL_STEP_TYPES:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"ApprovalRequest upstream_evidence[{index}].step_type is corrupted."
            ),
            status_code=409,
        )
    status = item["status"]
    if status not in _CANONICAL_STEP_STATUSES:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"ApprovalRequest upstream_evidence[{index}].status is corrupted."
            ),
            status_code=409,
        )
    condition_result = item["condition_result"]
    if condition_result is not None and not isinstance(condition_result, bool):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"ApprovalRequest upstream_evidence[{index}].condition_result "
                "is corrupted."
            ),
            status_code=409,
        )
    error_code = item["error_code"]
    if error_code is not None and not isinstance(error_code, str):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"ApprovalRequest upstream_evidence[{index}].error_code is corrupted."
            ),
            status_code=409,
        )
    result_hash = item["result_inline_hash"]
    if result_hash is not None and (
        not isinstance(result_hash, str) or _SHA256_HEX_RE.fullmatch(result_hash) is None
    ):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"ApprovalRequest upstream_evidence[{index}].result_inline_hash "
                "is corrupted."
            ),
            status_code=409,
        )
    return item


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
    _parse_context_uuid(snapshot["execution_id"], field="execution_id")
    _parse_context_uuid(snapshot["step_execution_id"], field="step_execution_id")
    _parse_context_uuid(snapshot["requester_id"], field="requester_id")
    _parse_optional_context_uuid(
        snapshot["agent_version_id"], field="agent_version_id"
    )
    _parse_optional_context_uuid(
        snapshot["workflow_version_id"], field="workflow_version_id"
    )
    if not isinstance(snapshot["step_key"], str) or not snapshot["step_key"]:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ApprovalRequest context_snapshot step_key is corrupted.",
            status_code=409,
        )
    if not isinstance(snapshot["plan_hash"], str) or not snapshot["plan_hash"]:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ApprovalRequest context_snapshot plan_hash is corrupted.",
            status_code=409,
        )
    _validate_approval_policy_snapshot(snapshot["approval_policy"])
    upstream = snapshot["upstream_evidence"]
    if not isinstance(upstream, list):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ApprovalRequest upstream_evidence must be a list.",
            status_code=409,
        )
    for index, item in enumerate(upstream):
        _validate_upstream_evidence_item(item, index=index)
    return snapshot


def assert_approval_step_lineage_matches(
    *,
    execution: Execution,
    step: ExecutionStep,
    snapshot: dict[str, Any],
) -> None:
    snap_exec = _parse_context_uuid(snapshot["execution_id"], field="execution_id")
    snap_step = _parse_context_uuid(
        snapshot["step_execution_id"], field="step_execution_id"
    )
    snap_requester = _parse_context_uuid(
        snapshot["requester_id"], field="requester_id"
    )
    snap_agent = _parse_optional_context_uuid(
        snapshot.get("agent_version_id"), field="agent_version_id"
    )
    snap_workflow = _parse_optional_context_uuid(
        snapshot.get("workflow_version_id"), field="workflow_version_id"
    )
    policy = snapshot.get("approval_policy")
    if isinstance(policy, dict) and "id" in policy:
        _parse_context_uuid(policy["id"], field="approval_policy.id")
    agent_ok = snap_agent == execution.agent_version_id
    workflow_ok = snap_workflow == execution.workflow_version_id
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
