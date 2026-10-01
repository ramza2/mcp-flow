"""Deterministic Predicate AST runtime evaluator (docs/04 §10).

Pure: no DB writes, MCP, LLM, SecretResolver, or expression/eval.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from app.core.errors import AppError
from app.domain.enums import PredicateOperator
from app.execution.binding_resolver import (
    RuntimeBindingResolver,
    is_secret_ref_value,
)
from app.execution.json_pointer import MISSING, PointerMissing
from app.models.execution import Execution, ExecutionStep
from app.schemas.execution_plan import ExecutionPlanV1
from app.schemas.plan_binding import PlanBindingValue, PlanSecretRefBinding
from app.schemas.predicate import (
    ComparisonPredicate,
    LogicalPredicate,
    NotPredicate,
    Predicate,
    UnaryPredicate,
    parse_predicate,
)

_CODE_OPERAND_MISSING = "PREDICATE_OPERAND_MISSING"
_CODE_TYPE_MISMATCH = "PREDICATE_TYPE_MISMATCH"
_CODE_EVAL_FAILED = "PREDICATE_EVALUATION_FAILED"


def _fail(code: str, message: str) -> AppError:
    return AppError(code=code, message=message, status_code=409)


def _json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "number"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _reject_non_finite_number(value: Any) -> None:
    """Fail closed: NaN / ±Infinity are not comparable JSON numbers."""
    if isinstance(value, float) and not isinstance(value, bool) and not math.isfinite(
        value
    ):
        raise _fail(
            _CODE_TYPE_MISMATCH,
            "Non-finite float (NaN / ±Infinity) is not a comparable JSON number.",
        )


def json_strict_equal(left: Any, right: Any) -> bool:
    """Strict JSON-type-aware equality (``True != 1``, ``\"1\" != 1``).

    Numeric equality uses Python's exact-safe ``==`` after excluding bool.
    Does not coerce through binary ``float()``. Non-finite floats fail closed.
    """
    if left is None and right is None:
        return True
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if _is_number(left) or _is_number(right):
        if _is_number(left):
            _reject_non_finite_number(left)
        if _is_number(right):
            _reject_non_finite_number(right)
        if _is_number(left) and _is_number(right):
            # Exact-safe: 1 == 1.0; large ints remain distinct from neighbors.
            return left == right
        return False
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return False
        return all(json_strict_equal(a, b) for a, b in zip(left, right, strict=True))
    if isinstance(left, dict) and isinstance(right, dict):
        if set(left) != set(right):
            return False
        return all(json_strict_equal(left[k], right[k]) for k in left)
    return False


class RuntimePredicateEvaluator:
    """Evaluate a Predicate AST against immutable Execution/Plan/Step evidence."""

    def __init__(self, binding_resolver: RuntimeBindingResolver | None = None) -> None:
        self._bindings = binding_resolver or RuntimeBindingResolver()

    def evaluate(
        self,
        predicate: Predicate | dict[str, Any],
        *,
        execution: Execution,
        owning_step: ExecutionStep,
        steps: Sequence[ExecutionStep],
        plan: ExecutionPlanV1,
    ) -> bool:
        if isinstance(predicate, dict):
            try:
                parsed = parse_predicate(predicate)
            except Exception as exc:
                raise _fail(
                    _CODE_EVAL_FAILED,
                    "Predicate evidence is malformed and cannot be evaluated.",
                ) from exc
        else:
            parsed = predicate

        by_key = {s.step_key: s for s in steps}
        if (
            owning_step.step_key not in by_key
            or by_key[owning_step.step_key].id != owning_step.id
        ):
            raise _fail(
                _CODE_EVAL_FAILED,
                "Owning Step missing from Execution Step set for Predicate.",
            )
        ancestors = self._bindings.transitive_ancestors(plan)
        return self._eval(
            parsed,
            execution=execution,
            owning_step=owning_step,
            by_key=by_key,
            plan=plan,
            ancestors=ancestors,
        )

    def _eval(
        self,
        predicate: Predicate,
        *,
        execution: Execution,
        owning_step: ExecutionStep,
        by_key: dict[str, ExecutionStep],
        plan: ExecutionPlanV1,
        ancestors: dict[str, set[str]],
    ) -> bool:
        if isinstance(predicate, LogicalPredicate):
            if predicate.op == PredicateOperator.AND:
                for child in predicate.children:
                    if not self._eval(
                        child,
                        execution=execution,
                        owning_step=owning_step,
                        by_key=by_key,
                        plan=plan,
                        ancestors=ancestors,
                    ):
                        return False
                return True
            # OR
            for child in predicate.children:
                if self._eval(
                    child,
                    execution=execution,
                    owning_step=owning_step,
                    by_key=by_key,
                    plan=plan,
                    ancestors=ancestors,
                ):
                    return True
            return False

        if isinstance(predicate, NotPredicate):
            return not self._eval(
                predicate.child,
                execution=execution,
                owning_step=owning_step,
                by_key=by_key,
                plan=plan,
                ancestors=ancestors,
            )

        if isinstance(predicate, UnaryPredicate):
            return self._eval_unary(
                predicate,
                execution=execution,
                owning_step=owning_step,
                by_key=by_key,
                plan=plan,
                ancestors=ancestors,
            )

        if isinstance(predicate, ComparisonPredicate):
            return self._eval_comparison(
                predicate,
                execution=execution,
                owning_step=owning_step,
                by_key=by_key,
                plan=plan,
                ancestors=ancestors,
            )

        raise _fail(_CODE_EVAL_FAILED, "Unsupported Predicate node.")

    def _resolve_operand(
        self,
        binding: PlanBindingValue,
        *,
        execution: Execution,
        owning_step: ExecutionStep,
        by_key: dict[str, ExecutionStep],
        plan: ExecutionPlanV1,
        ancestors: dict[str, set[str]],
    ) -> Any:
        try:
            return self._bindings.resolve_binding(
                binding=binding,
                execution=execution,
                owning_step=owning_step,
                by_key=by_key,
                plan=plan,
                ancestors=ancestors,
                missing_ok=True,
            )
        except AppError as exc:
            if exc.code in {
                _CODE_OPERAND_MISSING,
                _CODE_TYPE_MISMATCH,
                _CODE_EVAL_FAILED,
            }:
                raise
            raise _fail(_CODE_EVAL_FAILED, exc.message) from exc

    def _eval_unary(
        self,
        predicate: UnaryPredicate,
        *,
        execution: Execution,
        owning_step: ExecutionStep,
        by_key: dict[str, ExecutionStep],
        plan: ExecutionPlanV1,
        ancestors: dict[str, set[str]],
    ) -> bool:
        binding = predicate.operand
        if isinstance(binding, PlanSecretRefBinding):
            # Reference-only: exists → true, is_null → false. Never plaintext.
            if predicate.op == PredicateOperator.EXISTS:
                return True
            if predicate.op == PredicateOperator.IS_NULL:
                return False
            raise _fail(
                _CODE_TYPE_MISMATCH,
                "SECRET_REF is unsupported for this Predicate operator.",
            )

        value = self._resolve_operand(
            binding,
            execution=execution,
            owning_step=owning_step,
            by_key=by_key,
            plan=plan,
            ancestors=ancestors,
        )
        if predicate.op == PredicateOperator.EXISTS:
            if value is MISSING or isinstance(value, PointerMissing):
                return False
            return True
        if predicate.op == PredicateOperator.IS_NULL:
            if value is MISSING or isinstance(value, PointerMissing):
                return False
            return value is None
        raise _fail(_CODE_EVAL_FAILED, f"Unsupported unary op {predicate.op!r}.")

    def _eval_comparison(
        self,
        predicate: ComparisonPredicate,
        *,
        execution: Execution,
        owning_step: ExecutionStep,
        by_key: dict[str, ExecutionStep],
        plan: ExecutionPlanV1,
        ancestors: dict[str, set[str]],
    ) -> bool:
        if isinstance(predicate.left, PlanSecretRefBinding) or isinstance(
            predicate.right, PlanSecretRefBinding
        ):
            raise _fail(
                _CODE_TYPE_MISMATCH,
                "SECRET_REF cannot be used in binary Predicate comparisons.",
            )

        left = self._resolve_operand(
            predicate.left,
            execution=execution,
            owning_step=owning_step,
            by_key=by_key,
            plan=plan,
            ancestors=ancestors,
        )
        right = self._resolve_operand(
            predicate.right,
            execution=execution,
            owning_step=owning_step,
            by_key=by_key,
            plan=plan,
            ancestors=ancestors,
        )

        if left is MISSING or isinstance(left, PointerMissing):
            raise _fail(
                _CODE_OPERAND_MISSING,
                "Predicate binary operand resolved to MISSING.",
            )
        if right is MISSING or isinstance(right, PointerMissing):
            raise _fail(
                _CODE_OPERAND_MISSING,
                "Predicate binary operand resolved to MISSING.",
            )
        if is_secret_ref_value(left) or is_secret_ref_value(right):
            raise _fail(
                _CODE_TYPE_MISMATCH,
                "SECRET_REF values cannot be used in binary Predicate comparisons.",
            )
        # Non-finite floats are never normal comparable JSON numbers.
        if _is_number(left):
            _reject_non_finite_number(left)
        if _is_number(right):
            _reject_non_finite_number(right)

        op = predicate.op
        if op == PredicateOperator.EQ:
            return json_strict_equal(left, right)
        if op == PredicateOperator.NE:
            return not json_strict_equal(left, right)
        if op in {
            PredicateOperator.GT,
            PredicateOperator.GTE,
            PredicateOperator.LT,
            PredicateOperator.LTE,
        }:
            return self._ordered_compare(op, left, right)
        if op == PredicateOperator.IN:
            if not isinstance(right, list):
                raise _fail(
                    _CODE_TYPE_MISMATCH,
                    f"`in` right operand must be an array (got {_json_type_name(right)}).",
                )
            return any(json_strict_equal(left, item) for item in right)
        if op == PredicateOperator.CONTAINS:
            if isinstance(left, str) and isinstance(right, str):
                return right in left
            if isinstance(left, list):
                return any(json_strict_equal(item, right) for item in left)
            raise _fail(
                _CODE_TYPE_MISMATCH,
                (
                    "`contains` allows string⊃string or array⊃value "
                    f"(got {_json_type_name(left)} / {_json_type_name(right)})."
                ),
            )
        raise _fail(_CODE_EVAL_FAILED, f"Unsupported comparison op {op!r}.")

    def _ordered_compare(self, op: PredicateOperator, left: Any, right: Any) -> bool:
        if _is_number(left) and _is_number(right):
            _reject_non_finite_number(left)
            _reject_non_finite_number(right)
            # Exact-safe ordering on original numeric values (no float()).
            lv, rv = left, right
        elif isinstance(left, str) and isinstance(right, str):
            lv, rv = left, right
        else:
            raise _fail(
                _CODE_TYPE_MISMATCH,
                (
                    f"Ordered compare requires number↔number or string↔string "
                    f"(got {_json_type_name(left)} / {_json_type_name(right)})."
                ),
            )
        if op == PredicateOperator.GT:
            return lv > rv
        if op == PredicateOperator.GTE:
            return lv >= rv
        if op == PredicateOperator.LT:
            return lv < rv
        if op == PredicateOperator.LTE:
            return lv <= rv
        raise _fail(_CODE_EVAL_FAILED, f"Unsupported ordered op {op!r}.")
