"""Deterministic runtime Plan Binding resolution (docs/04 §8.4).

Side-effect free: no MCP / LLM / SecretResolver / DB writes.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.core.errors import AppError
from app.domain.enums import BindingKind, StepStatus
from app.execution.json_pointer import MISSING, resolve_json_pointer
from app.models.execution import Execution, ExecutionStep
from app.schemas.execution_plan import ExecutionPlanStep, ExecutionPlanV1
from app.schemas.plan_binding import (
    PlanBindingValue,
    PlanExecutionContextBinding,
    PlanInputBinding,
    PlanLiteralBinding,
    PlanLoopContextBinding,
    PlanSecretRefBinding,
    PlanStepOutputBinding,
)

_DYNAMIC_KINDS = frozenset(
    {
        BindingKind.PLAN_INPUT,
        BindingKind.STEP_OUTPUT,
        BindingKind.EXECUTION_CONTEXT,
        BindingKind.LOOP_CONTEXT,
    }
)


@dataclass(frozen=True, slots=True)
class ToolStepLineage:
    """Source-aware TOOL Step lineage used by Attempt start / replay."""

    tool_version_id: uuid.UUID
    bindings: dict[str, PlanBindingValue]
    plan: ExecutionPlanV1
    plan_step: ExecutionPlanStep
    agent_request_static_only: bool


def build_execution_context_projection(execution: Execution) -> dict[str, Any]:
    """Allowlisted EXECUTION_CONTEXT root (docs/04 §8.4)."""
    return {
        "execution_id": str(execution.id),
        "source_type": execution.source_type,
        "trigger_type": execution.trigger_type,
        "trace_id": execution.trace_id,
    }


def binding_kinds_include_dynamic(bindings: Mapping[str, PlanBindingValue]) -> bool:
    return any(binding.kind in _DYNAMIC_KINDS for binding in bindings.values())


def serialize_plan_bindings(
    bindings: Mapping[str, PlanBindingValue],
) -> dict[str, Any]:
    return {key: binding.model_dump(mode="json") for key, binding in bindings.items()}


def secret_ref_resolved(secret_id: uuid.UUID) -> dict[str, Any]:
    return {
        "kind": BindingKind.SECRET_REF.value,
        "secret_id": str(secret_id),
    }


def is_secret_ref_value(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("kind") == BindingKind.SECRET_REF.value
        and "secret_id" in value
        and len(value) == 2
    )


class RuntimeBindingResolver:
    """Resolve Plan bindings into a secret-safe ``resolved_input`` map."""

    def resolve(
        self,
        *,
        execution: Execution,
        step: ExecutionStep,
        steps: Sequence[ExecutionStep],
        bindings: Mapping[str, PlanBindingValue],
        plan: ExecutionPlanV1,
    ) -> dict[str, Any]:
        if step.execution_id != execution.id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Step does not belong to Execution during Binding resolve.",
                status_code=409,
            )
        by_key = {s.step_key: s for s in steps}
        if step.step_key not in by_key or by_key[step.step_key].id != step.id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Owning Step missing from Execution Step set.",
                status_code=409,
            )
        ancestors = _transitive_ancestors(plan)

        resolved: dict[str, Any] = {}
        for key, binding in bindings.items():
            resolved[key] = self._resolve_one(
                binding=binding,
                execution=execution,
                owning_step=step,
                by_key=by_key,
                plan=plan,
                ancestors=ancestors,
            )
        return resolved

    def _resolve_one(
        self,
        *,
        binding: PlanBindingValue,
        execution: Execution,
        owning_step: ExecutionStep,
        by_key: dict[str, ExecutionStep],
        plan: ExecutionPlanV1,
        ancestors: dict[str, set[str]],
    ) -> Any:
        if isinstance(binding, PlanLiteralBinding):
            return binding.value
        if isinstance(binding, PlanSecretRefBinding):
            return secret_ref_resolved(binding.secret_id)
        if isinstance(binding, PlanInputBinding):
            return self._resolve_plan_input(execution, plan, binding.path)
        if isinstance(binding, PlanStepOutputBinding):
            return self._resolve_step_output(
                binding=binding,
                owning_step=owning_step,
                by_key=by_key,
                ancestors=ancestors,
            )
        if isinstance(binding, PlanExecutionContextBinding):
            root = build_execution_context_projection(execution)
            value = resolve_json_pointer(root, binding.path)
            if value is MISSING:
                raise AppError(
                    code="EXECUTION_PRECONDITION_FAILED",
                    message=(
                        f"EXECUTION_CONTEXT path {binding.path!r} is MISSING "
                        "(not JSON null)."
                    ),
                    status_code=409,
                )
            return value
        if isinstance(binding, PlanLoopContextBinding):
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="LOOP_CONTEXT is unsupported until LOOP runtime exists.",
                status_code=409,
            )
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message=f"Unsupported BindingKind {getattr(binding, 'kind', None)!r}.",
            status_code=409,
        )

    def _resolve_plan_input(
        self, execution: Execution, plan: ExecutionPlanV1, path: str
    ) -> Any:
        root = execution.input_snapshot
        if root is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="Execution.input_snapshot is missing for PLAN_INPUT.",
                status_code=409,
            )
        if not isinstance(root, dict):
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="Execution.input_snapshot must be a JSON object.",
                status_code=409,
            )
        value = resolve_json_pointer(root, path)
        if value is MISSING:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=f"PLAN_INPUT path {path!r} is MISSING (not JSON null).",
                status_code=409,
            )
        self._assert_plan_input_secret_safe(plan, path, value)
        if is_secret_ref_value(value):
            # Preserve canonical reference; never call SecretResolver here.
            return {
                "kind": BindingKind.SECRET_REF.value,
                "secret_id": str(value["secret_id"]),
            }
        return value

    def _assert_plan_input_secret_safe(
        self, plan: ExecutionPlanV1, path: str, value: Any
    ) -> None:
        """Fail closed when a Plan input marked secret is plaintext."""
        if path == "/":
            return
        # First non-empty token after '/' is the top-level input key.
        first = path.split("/", 2)[1].replace("~1", "/").replace("~0", "~")
        decl = plan.inputs.get(first) if plan.inputs else None
        if decl is None or not getattr(decl, "secret", False):
            return
        if is_secret_ref_value(value):
            return
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message=(
                f"PLAN_INPUT {path!r} is declared secret but is not a "
                "canonical SECRET_REF reference."
            ),
            status_code=409,
        )

    def _resolve_step_output(
        self,
        *,
        binding: PlanStepOutputBinding,
        owning_step: ExecutionStep,
        by_key: dict[str, ExecutionStep],
        ancestors: dict[str, set[str]],
    ) -> Any:
        source = by_key.get(binding.step_id)
        if source is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=f"STEP_OUTPUT source step {binding.step_id!r} is missing.",
                status_code=409,
            )
        if source.execution_id != owning_step.execution_id:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="STEP_OUTPUT source belongs to another Execution.",
                status_code=409,
            )
        if binding.step_id not in ancestors.get(owning_step.step_key, set()):
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    f"STEP_OUTPUT source {binding.step_id!r} is not a transitive "
                    f"dependency ancestor of {owning_step.step_key!r}."
                ),
                status_code=409,
            )
        if source.status != StepStatus.SUCCEEDED.value:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    f"STEP_OUTPUT source {binding.step_id!r} must be SUCCEEDED "
                    f"(got {source.status!r})."
                ),
                status_code=409,
            )
        if source.result_inline is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    f"STEP_OUTPUT source {binding.step_id!r} has no result_inline."
                ),
                status_code=409,
            )
        value = resolve_json_pointer(source.result_inline, binding.path)
        if value is MISSING:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    f"STEP_OUTPUT path {binding.path!r} on {binding.step_id!r} "
                    "is MISSING (not JSON null)."
                ),
                status_code=409,
            )
        return value


def _transitive_ancestors(plan: ExecutionPlanV1) -> dict[str, set[str]]:
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

    for sid in ids:
        walk(sid)
    return cache

