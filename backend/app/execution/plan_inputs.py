"""Shared ExecutionPlanV1 Plan-input normalization (Workflow execution / Schedule).

Extracted from WorkflowExecutionCreationService for reuse by Schedule registry.
Behavior must remain semantic-compatible with manual Workflow execution.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.agent.plan_validator import _literal_matches_type
from app.core.errors import AppError
from app.domain.enums import BindingKind
from app.schemas.execution_plan import ExecutionPlanV1

_SUPPORTED_INPUT_TYPES = frozenset(
    {"string", "integer", "number", "boolean", "object", "array"}
)


def normalize_secret_ref(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise AppError(
            code="VALIDATION_ERROR",
            message="secret Plan input requires exact SECRET_REF object.",
            status_code=400,
        )
    if set(value.keys()) != {"kind", "secret_id"}:
        raise AppError(
            code="VALIDATION_ERROR",
            message="secret Plan input requires exact keys kind/secret_id.",
            status_code=400,
        )
    if value.get("kind") != BindingKind.SECRET_REF.value:
        raise AppError(
            code="VALIDATION_ERROR",
            message="secret Plan input kind must be SECRET_REF.",
            status_code=400,
        )
    secret_id = value.get("secret_id")
    try:
        normalized = str(uuid.UUID(str(secret_id)))
    except (TypeError, ValueError) as exc:
        raise AppError(
            code="VALIDATION_ERROR",
            message="secret Plan input secret_id must be a canonical UUID.",
            status_code=400,
        ) from exc
    return {"kind": BindingKind.SECRET_REF.value, "secret_id": normalized}


def normalize_plan_inputs(
    plan: ExecutionPlanV1, request_inputs: dict[str, Any]
) -> dict[str, Any]:
    """Validate request inputs against ExecutionPlanV1.inputs; return snapshot."""
    declared = plan.inputs
    for key in request_inputs:
        if key not in declared:
            raise AppError(
                code="VALIDATION_ERROR",
                message=f"unknown Plan input key={key!r}",
                status_code=400,
            )

    snapshot: dict[str, Any] = {}
    for name, definition in declared.items():
        if definition.type not in _SUPPORTED_INPUT_TYPES:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=f"unsupported Plan input type={definition.type!r}",
                status_code=409,
            )
        if name not in request_inputs:
            if definition.required:
                raise AppError(
                    code="VALIDATION_ERROR",
                    message=f"missing required Plan input={name!r}",
                    status_code=400,
                )
            continue
        raw = request_inputs[name]
        if definition.secret:
            snapshot[name] = normalize_secret_ref(raw)
            continue
        if isinstance(raw, dict) and raw.get("kind") == BindingKind.SECRET_REF.value:
            # SECRET_REF is not a type bypass for non-secret inputs.
            if definition.type != "object":
                raise AppError(
                    code="VALIDATION_ERROR",
                    message=(
                        f"SECRET_REF is not accepted for non-secret Plan "
                        f"input={name!r}"
                    ),
                    status_code=400,
                )
        if not _literal_matches_type(raw, definition.type):
            raise AppError(
                code="VALIDATION_ERROR",
                message=f"Plan input {name!r} type mismatch for {definition.type!r}",
                status_code=400,
            )
        snapshot[name] = raw
    return snapshot
