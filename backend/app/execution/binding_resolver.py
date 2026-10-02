"""Deterministic runtime Plan Binding resolution (docs/04 §8.4).

Side-effect free: no MCP / LLM / SecretResolver / DB writes.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.core.errors import AppError
from app.domain.enums import AuthorableStepType, BindingKind, StepStatus
from app.execution.json_pointer import MISSING, resolve_json_pointer
from app.execution.loop_runtime import (
    LOOP_COLLECTION_TYPE_MISMATCH,
    build_loop_body_ownership,
    build_loop_context_projection,
    build_previous_iteration_projection,
    build_while_loop_context_projection,
    hash_collection,
    template_id_from_step_snapshot,
)
from app.models.execution import Execution, ExecutionStep
from app.schemas.execution_plan import ExecutionPlanStep, ExecutionPlanV1, LoopStepConfigV1
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
        loop_context_override: Mapping[str, Any] | None = None,
    ) -> Any:
        """Resolve one Binding.

        When ``missing_ok`` is True (Predicate evaluation), missing JSON Pointer
        paths return the ``MISSING`` sentinel. TOOL Binding resolution keeps
        ``missing_ok=False`` and still fails closed on MISSING.

        ``loop_context_override`` supplies an explicit WHILE candidate
        LOOP_CONTEXT projection (parent Predicate only). Default None preserves
        existing body-instance / fail-closed behavior.
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
            return self._resolve_loop_context(
                binding=binding,
                execution=execution,
                owning_step=owning_step,
                by_key=by_key,
                plan=plan,
                ancestors=ancestors,
                missing_ok=missing_ok,
                loop_context_override=loop_context_override,
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

    def _resolve_loop_context(
        self,
        *,
        binding: PlanLoopContextBinding,
        execution: Execution,
        owning_step: ExecutionStep,
        by_key: dict[str, ExecutionStep],
        plan: ExecutionPlanV1,
        ancestors: dict[str, set[str]],
        missing_ok: bool = False,
        loop_context_override: Mapping[str, Any] | None = None,
    ) -> Any:
        """Resolve LOOP_CONTEXT for FOR_EACH/WHILE body or WHILE parent override."""
        # Explicit WHILE parent Predicate candidate context (no body instance).
        if loop_context_override is not None:
            if not isinstance(loop_context_override, Mapping):
                raise AppError(
                    code="EXECUTION_PRECONDITION_FAILED",
                    message="loop_context_override must be a mapping.",
                    status_code=409,
                )
            projection = dict(loop_context_override)
            value = resolve_json_pointer(projection, binding.path)
            if value is MISSING:
                if missing_ok:
                    return MISSING
                raise AppError(
                    code="EXECUTION_PRECONDITION_FAILED",
                    message=(
                        f"LOOP_CONTEXT path {binding.path!r} is MISSING "
                        "(not JSON null)."
                    ),
                    status_code=409,
                )
            return value

        if getattr(owning_step, "parent_step_id", None) is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    "LOOP_CONTEXT is only valid inside a LOOP body instance "
                    "(owning Step has no parent_step_id)."
                ),
                status_code=409,
            )
        parent = next(
            (
                s
                for s in by_key.values()
                if s.id == owning_step.parent_step_id
            ),
            None,
        )
        if parent is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="LOOP_CONTEXT owning LOOP parent Step is missing.",
                status_code=409,
            )
        if parent.execution_id != owning_step.execution_id:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="LOOP_CONTEXT parent belongs to another Execution.",
                status_code=409,
            )
        if parent.parent_step_id is not None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="LOOP_CONTEXT parent must be a top-level LOOP Step.",
                status_code=409,
            )
        if parent.step_type != AuthorableStepType.LOOP.value:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="LOOP_CONTEXT parent Step must be type LOOP.",
                status_code=409,
            )
        if owning_step.iteration_no is None or owning_step.iteration_no < 1:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="LOOP_CONTEXT requires owning Step iteration_no >= 1.",
                status_code=409,
            )

        try:
            parent_plan_step = ExecutionPlanStep.model_validate(parent.step_snapshot)
            child_plan_step = ExecutionPlanStep.model_validate(
                owning_step.step_snapshot
            )
            loop_cfg = LoopStepConfigV1.model_validate(parent_plan_step.config)
        except Exception as exc:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="LOOP_CONTEXT parent/child Plan snapshot is invalid.",
                status_code=409,
            ) from exc

        if parent_plan_step.id != parent.step_key:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="LOOP_CONTEXT parent step_key does not match Plan LOOP id.",
                status_code=409,
            )
        if child_plan_step.id not in loop_cfg.body_step_ids:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    f"LOOP_CONTEXT child template {child_plan_step.id!r} is not "
                    f"in LOOP {parent_plan_step.id!r} body_step_ids."
                ),
                status_code=409,
            )

        if loop_cfg.mode.value == "WHILE":
            projection = self._build_while_body_context(
                parent=parent,
                parent_plan_step=parent_plan_step,
                loop_cfg=loop_cfg,
                by_key=by_key,
                iteration_no=owning_step.iteration_no,
                plan=plan,
            )
        else:
            projection = self._build_foreach_body_context(
                binding=binding,
                execution=execution,
                owning_step=owning_step,
                parent=parent,
                parent_plan_step=parent_plan_step,
                loop_cfg=loop_cfg,
                by_key=by_key,
                plan=plan,
                ancestors=ancestors,
            )

        value = resolve_json_pointer(projection, binding.path)
        if value is MISSING:
            if missing_ok:
                return MISSING
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    f"LOOP_CONTEXT path {binding.path!r} is MISSING "
                    "(not JSON null)."
                ),
                status_code=409,
            )
        return value

    def _build_while_body_context(
        self,
        *,
        parent: ExecutionStep,
        parent_plan_step: ExecutionPlanStep,
        loop_cfg: LoopStepConfigV1,
        by_key: dict[str, ExecutionStep],
        iteration_no: int,
        plan: ExecutionPlanV1,
    ) -> dict[str, Any]:
        if iteration_no == 1:
            previous: dict[str, Any] | None = None
        else:
            previous = build_previous_iteration_projection(
                steps=list(by_key.values()),
                parent_step_id=parent.id,
                previous_iteration_no=iteration_no - 1,
                body_step_ids=loop_cfg.body_step_ids,
                plan=plan,
            )
        return build_while_loop_context_projection(
            loop_plan_step_id=parent_plan_step.id,
            iteration_no=iteration_no,
            max_iterations=loop_cfg.max_iterations,
            previous_iteration=previous,
        )

    def _build_foreach_body_context(
        self,
        *,
        binding: PlanLoopContextBinding,
        execution: Execution,
        owning_step: ExecutionStep,
        parent: ExecutionStep,
        parent_plan_step: ExecutionPlanStep,
        loop_cfg: LoopStepConfigV1,
        by_key: dict[str, ExecutionStep],
        plan: ExecutionPlanV1,
        ancestors: dict[str, set[str]],
    ) -> dict[str, Any]:
        del binding  # path applied by caller
        if loop_cfg.collection is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="LOOP_CONTEXT requires FOR_EACH collection on parent LOOP.",
                status_code=409,
            )
        if isinstance(loop_cfg.collection, PlanLoopContextBinding):
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="LOOP collection binding must not use LOOP_CONTEXT.",
                status_code=409,
            )

        collection = self.resolve_binding(
            binding=loop_cfg.collection,
            execution=execution,
            owning_step=parent,
            by_key=by_key,
            plan=plan,
            ancestors=ancestors,
            missing_ok=False,
        )
        if not isinstance(collection, list):
            raise AppError(
                code=LOOP_COLLECTION_TYPE_MISMATCH,
                message=(
                    f"LOOP {parent_plan_step.id!r} collection must resolve to a "
                    f"JSON array (got {type(collection).__name__})."
                ),
                status_code=409,
            )

        parent_ri = parent.resolved_input
        if not isinstance(parent_ri, dict):
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="LOOP parent resolved_input is missing for LOOP_CONTEXT.",
                status_code=409,
            )
        expected_hash = parent_ri.get("collection_hash")
        expected_size = parent_ri.get("collection_size")
        actual_hash = hash_collection(collection)
        actual_size = len(collection)
        if expected_hash != actual_hash or expected_size != actual_size:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    f"LOOP {parent_plan_step.id!r} collection hash/size drifted "
                    "from pinned parent.resolved_input."
                ),
                status_code=409,
            )
        if owning_step.iteration_no > actual_size:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    f"LOOP_CONTEXT iteration_no={owning_step.iteration_no} exceeds "
                    f"collection_size={actual_size}."
                ),
                status_code=409,
            )

        return build_loop_context_projection(
            loop_plan_step_id=parent_plan_step.id,
            mode=loop_cfg.mode.value,
            iteration_no=owning_step.iteration_no,
            item=collection[owning_step.iteration_no - 1],
            collection_size=actual_size,
        )

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
        ownership = build_loop_body_ownership(plan)

        if getattr(owning_step, "parent_step_id", None) is None:
            # Top-level consumer: reject body-template targets explicitly.
            if binding.step_id in ownership.body_to_loop:
                raise AppError(
                    code="EXECUTION_PRECONDITION_FAILED",
                    message=(
                        f"STEP_OUTPUT source {binding.step_id!r} is a LOOP body "
                        "template and cannot be referenced from a top-level Step."
                    ),
                    status_code=409,
                )
            source = by_key.get(binding.step_id)
            if source is None:
                raise AppError(
                    code="EXECUTION_PRECONDITION_FAILED",
                    message=(
                        f"STEP_OUTPUT source step {binding.step_id!r} is missing."
                    ),
                    status_code=409,
                )
            if getattr(source, "parent_step_id", None) is not None:
                raise AppError(
                    code="EXECUTION_PRECONDITION_FAILED",
                    message=(
                        f"STEP_OUTPUT source {binding.step_id!r} must be a "
                        "top-level ExecutionStep."
                    ),
                    status_code=409,
                )
            if binding.step_id not in ancestors.get(owning_step.step_key, set()):
                raise AppError(
                    code="EXECUTION_PRECONDITION_FAILED",
                    message=(
                        f"STEP_OUTPUT source {binding.step_id!r} is not a "
                        f"transitive dependency ancestor of "
                        f"{owning_step.step_key!r}."
                    ),
                    status_code=409,
                )
        else:
            source = self._resolve_body_instance_step_output_source(
                binding=binding,
                owning_step=owning_step,
                by_key=by_key,
                plan=plan,
                ancestors=ancestors,
                ownership_body_to_loop=ownership.body_to_loop,
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

    def _resolve_body_instance_step_output_source(
        self,
        *,
        binding: PlanStepOutputBinding,
        owning_step: ExecutionStep,
        by_key: dict[str, ExecutionStep],
        plan: ExecutionPlanV1,
        ancestors: dict[str, set[str]],
        ownership_body_to_loop: Mapping[str, str],
    ) -> ExecutionStep:
        """Locate STEP_OUTPUT source for a LOOP body instance consumer."""
        owning_template = template_id_from_step_snapshot(owning_step)
        parent_loop_id = ownership_body_to_loop.get(owning_template)
        if parent_loop_id is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    f"STEP_OUTPUT consumer template {owning_template!r} is not "
                    "a LOOP body template."
                ),
                status_code=409,
            )

        source_loop_id = ownership_body_to_loop.get(binding.step_id)
        if source_loop_id == parent_loop_id:
            # Same-body source: match parent + iteration; never by_key[template].
            if owning_step.iteration_no is None or owning_step.iteration_no < 1:
                raise AppError(
                    code="EXECUTION_PRECONDITION_FAILED",
                    message=(
                        "STEP_OUTPUT same-body resolve requires iteration_no >= 1."
                    ),
                    status_code=409,
                )
            if binding.step_id not in ancestors.get(owning_template, set()):
                raise AppError(
                    code="EXECUTION_PRECONDITION_FAILED",
                    message=(
                        f"STEP_OUTPUT source {binding.step_id!r} is not a "
                        f"same-iteration dependency ancestor of "
                        f"{owning_template!r}."
                    ),
                    status_code=409,
                )
            matches = [
                s
                for s in by_key.values()
                if s.parent_step_id == owning_step.parent_step_id
                and s.iteration_no == owning_step.iteration_no
                and template_id_from_step_snapshot(s) == binding.step_id
            ]
            if not matches:
                raise AppError(
                    code="EXECUTION_PRECONDITION_FAILED",
                    message=(
                        f"STEP_OUTPUT same-body source {binding.step_id!r} is "
                        f"missing for iteration_no={owning_step.iteration_no}."
                    ),
                    status_code=409,
                )
            if len(matches) > 1:
                raise AppError(
                    code="EXECUTION_PRECONDITION_FAILED",
                    message=(
                        f"STEP_OUTPUT same-body source {binding.step_id!r} is "
                        "ambiguous within the iteration."
                    ),
                    status_code=409,
                )
            return matches[0]

        # Outside-body: top-level Step that is a transitive ancestor of the LOOP.
        source = by_key.get(binding.step_id)
        if source is None or getattr(source, "parent_step_id", None) is not None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    f"STEP_OUTPUT outside-body source step {binding.step_id!r} "
                    "is missing."
                ),
                status_code=409,
            )
        if binding.step_id not in ancestors.get(parent_loop_id, set()):
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    f"STEP_OUTPUT source {binding.step_id!r} is not a transitive "
                    f"dependency ancestor of owning LOOP {parent_loop_id!r}."
                ),
                status_code=409,
            )
        return source

    def _assert_step_output_source_lineage(
        self,
        *,
        binding: PlanStepOutputBinding,
        source: ExecutionStep,
        plan: ExecutionPlanV1,
    ) -> None:
        """Fail closed when STEP_OUTPUT source drifts from immutable Plan."""
        if getattr(source, "parent_step_id", None) is None:
            if source.step_key != binding.step_id:
                raise AppError(
                    code="EXECUTION_PRECONDITION_FAILED",
                    message=(
                        f"STEP_OUTPUT source step_key {source.step_key!r} does not "
                        f"match binding.step_id {binding.step_id!r}."
                    ),
                    status_code=409,
                )
        else:
            # Body instance: identity is step_snapshot.id, not synthetic step_key.
            try:
                snapshot_id = template_id_from_step_snapshot(source)
            except AppError as exc:
                raise AppError(
                    code="EXECUTION_PRECONDITION_FAILED",
                    message=(
                        f"STEP_OUTPUT source {binding.step_id!r} step_snapshot "
                        "is invalid."
                    ),
                    status_code=409,
                ) from exc
            if snapshot_id != binding.step_id:
                raise AppError(
                    code="EXECUTION_PRECONDITION_FAILED",
                    message=(
                        f"STEP_OUTPUT source snapshot id {snapshot_id!r} does not "
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

