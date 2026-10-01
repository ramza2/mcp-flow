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


def _parse_secret_id(value: Any) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except (TypeError, ValueError):
        return None


def is_secret_ref_value(value: Any) -> bool:
    """True only for exact ``{"kind":"SECRET_REF","secret_id":"<uuid>"}``."""
    if not isinstance(value, dict) or len(value) != 2:
        return False
    if value.get("kind") != BindingKind.SECRET_REF.value:
        return False
    return _parse_secret_id(value.get("secret_id")) is not None


def canonicalize_secret_ref_value(value: Any) -> dict[str, Any]:
    """Normalize a canonical SECRET_REF or fail closed on malformed shape."""
    if not isinstance(value, dict):
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="SECRET_REF value must be an object.",
            status_code=409,
        )
    if value.get("kind") != BindingKind.SECRET_REF.value:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="SECRET_REF value kind must be SECRET_REF.",
            status_code=409,
        )
    if set(value.keys()) != {"kind", "secret_id"}:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="SECRET_REF value must contain exactly kind and secret_id.",
            status_code=409,
        )
    secret_id = _parse_secret_id(value.get("secret_id"))
    if secret_id is None:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="SECRET_REF secret_id must be a valid UUID string.",
            status_code=409,
        )
    return secret_ref_resolved(secret_id)


class RuntimeBindingResolver:
    """Resolve Plan bindings into a secret-safe ``resolved_input`` map."""

    def transitive_ancestors(self, plan: ExecutionPlanV1) -> dict[str, set[str]]:
        return _transitive_ancestors(plan)

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
            resolved[key] = self.resolve_binding(
                binding=binding,
                execution=execution,
                owning_step=step,
                by_key=by_key,
                plan=plan,
                ancestors=ancestors,
                missing_ok=False,
            )
        return resolved

    def resolve_binding(
        self,
        *,
        binding: PlanBindingValue,
        execution: Execution,
        owning_step: ExecutionStep,
        by_key: dict[str, ExecutionStep],
        plan: ExecutionPlanV1,
        ancestors: dict[str, set[str]],
        missing_ok: bool = False,
    ) -> Any:
        """Resolve one Binding.

        When ``missing_ok`` is True (Predicate evaluation), missing JSON Pointer
        paths return the ``MISSING`` sentinel. TOOL Binding resolution keeps
        ``missing_ok=False`` and still fails closed on MISSING.
        """
        if isinstance(binding, PlanLiteralBinding):
            return binding.value
        if isinstance(binding, PlanSecretRefBinding):
            return secret_ref_resolved(binding.secret_id)
        if isinstance(binding, PlanInputBinding):
            return self._resolve_plan_input(
                execution, plan, binding.path, missing_ok=missing_ok
            )
        if isinstance(binding, PlanStepOutputBinding):
            return self._resolve_step_output(
                binding=binding,
                owning_step=owning_step,
                by_key=by_key,
                plan=plan,
                ancestors=ancestors,
                missing_ok=missing_ok,
            )
        if isinstance(binding, PlanExecutionContextBinding):
            root = build_execution_context_projection(execution)
            value = resolve_json_pointer(root, binding.path)
            if value is MISSING:
                if missing_ok:
                    return MISSING
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
        self,
        execution: Execution,
        plan: ExecutionPlanV1,
        path: str,
        *,
        missing_ok: bool = False,
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
            if missing_ok:
                return MISSING
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=f"PLAN_INPUT path {path!r} is MISSING (not JSON null).",
                status_code=409,
            )
        self._assert_plan_input_secret_safe(plan, path, root=root, value=value)
        if path == "/":
            # Project root: keep non-secrets; normalize declared secret refs.
            return self._project_plan_input_root(plan, root)
        if isinstance(value, dict) and value.get("kind") == BindingKind.SECRET_REF.value:
            # Any SECRET_REF-shaped leaf must be canonical before persistence.
            return canonicalize_secret_ref_value(value)
        return value

    def _assert_plan_input_secret_safe(
        self,
        plan: ExecutionPlanV1,
        path: str,
        *,
        root: dict[str, Any],
        value: Any,
    ) -> None:
        """Fail closed when a Plan input marked secret is not a SECRET_REF."""
        inputs = plan.inputs or {}
        if path == "/":
            for key, decl in inputs.items():
                if not decl.secret:
                    continue
                if key not in root:
                    if decl.required:
                        raise AppError(
                            code="EXECUTION_PRECONDITION_FAILED",
                            message=(
                                f"PLAN_INPUT '/' required secret input "
                                f"{key!r} is MISSING."
                            ),
                            status_code=409,
                        )
                    continue
                if not is_secret_ref_value(root[key]):
                    raise AppError(
                        code="EXECUTION_PRECONDITION_FAILED",
                        message=(
                            f"PLAN_INPUT '/' secret input {key!r} is not a "
                            "canonical SECRET_REF reference."
                        ),
                        status_code=409,
                    )
            return
        # First non-empty token after '/' is the top-level input key.
        first = path.split("/", 2)[1].replace("~1", "/").replace("~0", "~")
        decl = inputs.get(first)
        if decl is None or not decl.secret:
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

    def _project_plan_input_root(
        self, plan: ExecutionPlanV1, root: dict[str, Any]
    ) -> dict[str, Any]:
        """Copy root with declared-secret fields as normalized SECRET_REF."""
        inputs = plan.inputs or {}
        projected: dict[str, Any] = {}
        for key, value in root.items():
            decl = inputs.get(key)
            if decl is not None and decl.secret:
                projected[key] = canonicalize_secret_ref_value(value)
            elif (
                isinstance(value, dict)
                and value.get("kind") == BindingKind.SECRET_REF.value
            ):
                projected[key] = canonicalize_secret_ref_value(value)
            else:
                projected[key] = value
        return projected

    def _resolve_step_output(
        self,
        *,
        binding: PlanStepOutputBinding,
        owning_step: ExecutionStep,
        by_key: dict[str, ExecutionStep],
        plan: ExecutionPlanV1,
        ancestors: dict[str, set[str]],
        missing_ok: bool = False,
    ) -> Any:
        source = by_key.get(binding.step_id)
        if source is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=f"STEP_OUTPUT source step {binding.step_id!r} is missing.",
                status_code=409,
            )
        self._assert_step_output_source_lineage(
            binding=binding,
            source=source,
            plan=plan,
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
            if missing_ok:
                return MISSING
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    f"STEP_OUTPUT path {binding.path!r} on {binding.step_id!r} "
                    "is MISSING (not JSON null)."
                ),
                status_code=409,
            )
        return value

    def _assert_step_output_source_lineage(
        self,
        *,
        binding: PlanStepOutputBinding,
        source: ExecutionStep,
        plan: ExecutionPlanV1,
    ) -> None:
        """Fail closed when STEP_OUTPUT source drifts from immutable Plan."""
        if source.step_key != binding.step_id:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    f"STEP_OUTPUT source step_key {source.step_key!r} does not "
                    f"match binding.step_id {binding.step_id!r}."
                ),
                status_code=409,
            )
        expected = next((ps for ps in plan.steps if ps.id == binding.step_id), None)
        if expected is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    f"STEP_OUTPUT source {binding.step_id!r} has no matching "
                    "Plan Step."
                ),
                status_code=409,
            )
        try:
            parsed = ExecutionPlanStep.model_validate(source.step_snapshot)
        except Exception as exc:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    f"STEP_OUTPUT source {binding.step_id!r} step_snapshot "
                    "is invalid."
                ),
                status_code=409,
            ) from exc
        if parsed.id != binding.step_id:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    f"STEP_OUTPUT source snapshot id {parsed.id!r} does not "
                    f"match binding.step_id {binding.step_id!r}."
                ),
                status_code=409,
            )
        if expected.model_dump(mode="json") != source.step_snapshot:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    f"STEP_OUTPUT source {binding.step_id!r} step_snapshot "
                    "does not match Execution.plan_snapshot."
                ),
                status_code=409,
            )
        if parsed.type != expected.type or parsed.id != expected.id:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    f"STEP_OUTPUT source {binding.step_id!r} identity is "
                    "inconsistent with the Plan projection."
                ),
                status_code=409,
            )


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

