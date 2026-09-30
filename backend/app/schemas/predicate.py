"""Restricted Predicate AST for Execution Plan v1 (docs/04 §10).

Recursive structure — not a flat left/op/right-only model.
Static foundation validates structure/operators/bindings only;
runtime evaluation is out of scope.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from app.domain.enums import PredicateOperator
from app.schemas.plan_binding import PlanBindingValue


class ComparisonPredicate(BaseModel):
    """eq/ne/gt/gte/lt/lte/in/contains — left Binding + right Binding."""

    model_config = ConfigDict(extra="forbid")

    op: Literal[
        PredicateOperator.EQ,
        PredicateOperator.NE,
        PredicateOperator.GT,
        PredicateOperator.GTE,
        PredicateOperator.LT,
        PredicateOperator.LTE,
        PredicateOperator.IN,
        PredicateOperator.CONTAINS,
    ]
    left: PlanBindingValue
    right: PlanBindingValue


class UnaryPredicate(BaseModel):
    """exists / is_null — single operand Binding."""

    model_config = ConfigDict(extra="forbid")

    op: Literal[PredicateOperator.EXISTS, PredicateOperator.IS_NULL]
    operand: PlanBindingValue


class NotPredicate(BaseModel):
    """not — single child Predicate."""

    model_config = ConfigDict(extra="forbid")

    op: Literal[PredicateOperator.NOT] = PredicateOperator.NOT
    child: Predicate


class LogicalPredicate(BaseModel):
    """and / or — non-empty children Predicate list."""

    model_config = ConfigDict(extra="forbid")

    op: Literal[PredicateOperator.AND, PredicateOperator.OR]
    children: list[Predicate] = Field(min_length=1)

    @model_validator(mode="after")
    def _non_empty_children(self) -> LogicalPredicate:
        if not self.children:
            raise ValueError("and/or children must be non-empty")
        return self


Predicate = Annotated[
    ComparisonPredicate | UnaryPredicate | NotPredicate | LogicalPredicate,
    Field(discriminator="op"),
]

# Rebuild forward refs for NotPredicate / LogicalPredicate recursive fields.
NotPredicate.model_rebuild()
LogicalPredicate.model_rebuild()

_PREDICATE_ADAPTER: TypeAdapter[Predicate] = TypeAdapter(Predicate)


def parse_predicate(payload: dict[str, Any]) -> Predicate:
    return _PREDICATE_ADAPTER.validate_python(payload)


def iter_plan_bindings_in_predicate(predicate: Predicate) -> list[PlanBindingValue]:
    """Collect all PlanBindingValue leaves under a Predicate AST."""
    if isinstance(predicate, ComparisonPredicate):
        return [predicate.left, predicate.right]
    if isinstance(predicate, UnaryPredicate):
        return [predicate.operand]
    if isinstance(predicate, NotPredicate):
        return iter_plan_bindings_in_predicate(predicate.child)
    if isinstance(predicate, LogicalPredicate):
        out: list[PlanBindingValue] = []
        for child in predicate.children:
            out.extend(iter_plan_bindings_in_predicate(child))
        return out
    return []
