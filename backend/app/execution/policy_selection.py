"""Source-aware pinned ToolPolicy snapshot selection.

AGENT_REQUEST keeps the existing single-TOOL ``Execution.policy_snapshot``.
WORKFLOW_VERSION uses ``workflow_execution_policy.v1`` keyed by Plan TOOL
template id (including LOOP body templates via ``lineage.plan_step.id``).
"""

from __future__ import annotations

import uuid
from typing import Any

from app.core.errors import AppError
from app.domain.enums import ExecutionSourceType
from app.models.execution import Execution

_WORKFLOW_POLICY_SCHEMA = "workflow_execution_policy.v1"


def get_expected_tool_policy_snapshot(
    execution: Execution,
    *,
    plan_step_id: str,
    tool_version_id: uuid.UUID,
) -> dict[str, Any]:
    """Return the pinned per-TOOL policy object for Attempt/B2/retry.

    For WORKFLOW_VERSION, ``plan_step_id`` must be the immutable Plan TOOL
    template id (``lineage.plan_step.id``), never a synthetic runtime step_key.
    """
    snapshot = execution.policy_snapshot
    if not isinstance(snapshot, dict):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Execution.policy_snapshot is missing or malformed.",
            status_code=409,
        )

    if execution.source_type == ExecutionSourceType.AGENT_REQUEST.value:
        return dict(snapshot)

    if execution.source_type == ExecutionSourceType.WORKFLOW_VERSION.value:
        return _workflow_step_policy(
            execution,
            snapshot=snapshot,
            plan_step_id=plan_step_id,
            tool_version_id=tool_version_id,
        )

    if execution.source_type == ExecutionSourceType.MANUAL_TOOL_TEST.value:
        # Manual tool tests historically pin Agent-shaped single-TOOL snapshots.
        if snapshot.get("schema_version") == _WORKFLOW_POLICY_SCHEMA:
            return _workflow_step_policy(
                execution,
                snapshot=snapshot,
                plan_step_id=plan_step_id,
                tool_version_id=tool_version_id,
            )
        return dict(snapshot)

    raise AppError(
        code="RESOURCE_CONFLICT",
        message=(
            f"Unsupported Execution.source_type for policy selection: "
            f"{execution.source_type!r}."
        ),
        status_code=409,
    )


def _workflow_step_policy(
    execution: Execution,
    *,
    snapshot: dict[str, Any],
    plan_step_id: str,
    tool_version_id: uuid.UUID,
) -> dict[str, Any]:
    if snapshot.get("schema_version") != _WORKFLOW_POLICY_SCHEMA:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Workflow Execution.policy_snapshot schema_version mismatch.",
            status_code=409,
        )
    if execution.workflow_version_id is None:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="WORKFLOW_VERSION Execution missing workflow_version_id.",
            status_code=409,
        )
    snap_version = snapshot.get("workflow_version_id")
    if str(snap_version) != str(execution.workflow_version_id):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Workflow policy_snapshot.workflow_version_id mismatch.",
            status_code=409,
        )
    tool_steps = snapshot.get("tool_steps")
    if not isinstance(tool_steps, dict):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Workflow policy_snapshot.tool_steps is malformed.",
            status_code=409,
        )
    entry = tool_steps.get(plan_step_id)
    if not isinstance(entry, dict):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=f"Workflow policy entry missing for plan_step_id={plan_step_id!r}.",
            status_code=409,
        )
    entry_tool = entry.get("tool_version_id")
    if str(entry_tool) != str(tool_version_id):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"Workflow policy entry tool_version_id mismatch for "
                f"plan_step_id={plan_step_id!r}."
            ),
            status_code=409,
        )
    policy = entry.get("policy")
    if not isinstance(policy, dict):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"Workflow policy entry.policy missing for "
                f"plan_step_id={plan_step_id!r}."
            ),
            status_code=409,
        )
    return dict(policy)


def build_workflow_execution_policy_snapshot(
    *,
    workflow_id: uuid.UUID,
    workflow_version_id: uuid.UUID,
    tool_steps: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Build canonical ``workflow_execution_policy.v1`` object.

    ``tool_steps`` keys are Plan TOOL template ids in deterministic Plan order
    when constructed by the caller; JSON object key order is not semantic.
    """
    return {
        "schema_version": _WORKFLOW_POLICY_SCHEMA,
        "workflow_id": str(workflow_id),
        "workflow_version_id": str(workflow_version_id),
        "tool_steps": tool_steps,
    }
