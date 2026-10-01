"""Static complex-plan validator foundation (docs/04 §9.7).

Deterministic structure/DAG/typed-config/Predicate/binding/limits checks only.
Does not create Execution/ExecutionStep, call MCP, create ApprovalRequests,
evaluate runtime predicates, resolve STEP_OUTPUT, or orchestrate parallelism.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from app.domain.enums import AuthorableStepType, BindingKind
from app.schemas.execution_plan import (
    MAX_LOOP_NESTING_DEPTH,
    SYSTEM_HARD_MAX_DURATION_SECONDS,
    SYSTEM_HARD_MAX_LOOP_ITERATIONS,
    SYSTEM_HARD_MAX_PARALLELISM,
    SYSTEM_HARD_MAX_STEPS,
    ApprovalStepConfigV1,
    ComplexToolStepConfigV1,
    ConditionStepConfigV1,
    ExecutionPlanStep,
    ExecutionPlanV1,
    JoinStepConfigV1,
    LoopStepConfigV1,
)
from app.schemas.plan_binding import PlanBindingValue, PlanStepOutputBinding
from app.schemas.predicate import (
    iter_plan_bindings_in_predicate,
    parse_predicate,
)


@dataclass(frozen=True, slots=True)
class ComplexPlanIssue:
    code: str
    message: str
    step_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ComplexPlanValidationResult:
    ok: bool
    errors: list[ComplexPlanIssue]

    @property
    def error_codes(self) -> list[str]:
        return [e.code for e in self.errors]


class StaticComplexPlanValidator:
    """Validate complex Execution Plan v1 graphs for Workflow multi-step readiness."""

    def validate(self, plan: ExecutionPlanV1) -> ComplexPlanValidationResult:
        errors: list[ComplexPlanIssue] = []

        if plan.schema_version != "1.0":
            errors.append(
                _issue(
                    "PLAN_SCHEMA_INVALID",
                    f"unsupported schema_version={plan.schema_version!r}",
                )
            )

        step_ids = [s.id for s in plan.steps]
        id_set = set(step_ids)

        if not plan.steps:
            errors.append(_issue("PLAN_SCHEMA_INVALID", "steps must be non-empty"))

        blank_ids = [s.id for s in plan.steps if not s.id.strip()]
        for sid in blank_ids:
            errors.append(
                _issue("PLAN_SCHEMA_INVALID", "blank step id is not allowed", step_id=sid)
            )

        if len(id_set) != len(step_ids):
            seen: set[str] = set()
            for sid in step_ids:
                if sid in seen:
                    errors.append(
                        _issue(
                            "PLAN_STEP_DUPLICATE",
                            f"duplicate step id={sid!r}",
                            step_id=sid,
                        )
                    )
                seen.add(sid)

        # Dependency existence + self/duplicate deps
        for step in plan.steps:
            seen_deps: set[str] = set()
            for dep in step.depends_on:
                if dep not in id_set:
                    errors.append(
                        _issue(
                            "PLAN_DEPENDENCY_MISSING",
                            f"depends_on={dep!r} does not exist",
                            step_id=step.id,
                        )
                    )
                if dep == step.id:
                    errors.append(
                        _issue(
                            "PLAN_DEPENDENCY_MISSING",
                            "self dependency is not allowed",
                            step_id=step.id,
                        )
                    )
                if dep in seen_deps:
                    errors.append(
                        _issue(
                            "PLAN_DEPENDENCY_MISSING",
                            f"duplicate dependency {dep!r}",
                            step_id=step.id,
                        )
                    )
                seen_deps.add(dep)

        if _has_dependency_cycle(plan.steps):
            errors.append(
                _issue("PLAN_CYCLE_DETECTED", "plan dependency cycle detected")
            )

        # Reachability from roots (steps with no depends_on)
        if plan.steps and id_set:
            reachable = _reachable_from_roots(plan.steps)
            unreachable = id_set - reachable
            for sid in sorted(unreachable):
                errors.append(
                    _issue(
                        "PLAN_SCHEMA_INVALID",
                        f"step {sid!r} is not reachable from root steps",
                        step_id=sid,
                    )
                )

        # Limits: configured values vs system hard bounds
        # max_parallelism is a runtime concurrency cap — do NOT reject DAG wave width.
        errors.extend(_validate_limit_hard_bounds(plan))

        # Limits: max_steps vs actual step count
        if len(plan.steps) > plan.limits.max_steps:
            errors.append(
                _issue(
                    "PLAN_LIMIT_EXCEEDED",
                    f"step count {len(plan.steps)} exceeds max_steps="
                    f"{plan.limits.max_steps}",
                )
            )

        # First pass: collect LOOP body ownership (needed before binding ancestry rules)
        loop_body_owner: dict[str, str] = {}
        loop_configs: dict[str, LoopStepConfigV1] = {}
        ancestors = _transitive_ancestors(plan.steps)

        for step in plan.steps:
            if step.type != AuthorableStepType.LOOP:
                continue
            try:
                cfg = LoopStepConfigV1.model_validate(step.config)
            except Exception:
                continue
            loop_configs[step.id] = cfg
            for body_id in cfg.body_step_ids:
                if body_id not in id_set:
                    errors.append(
                        _issue(
                            "PLAN_SCHEMA_INVALID",
                            f"LOOP body_step_id={body_id!r} does not exist",
                            step_id=step.id,
                        )
                    )
                    continue
                if body_id == step.id:
                    errors.append(
                        _issue(
                            "PLAN_SCHEMA_INVALID",
                            "LOOP cannot include itself in body_step_ids",
                            step_id=step.id,
                        )
                    )
                    continue
                if body_id in loop_body_owner:
                    errors.append(
                        _issue(
                            "PLAN_SCHEMA_INVALID",
                            f"step {body_id!r} belongs to multiple LOOP bodies",
                            step_id=body_id,
                        )
                    )
                else:
                    loop_body_owner[body_id] = step.id

        body_templates = set(loop_body_owner)

        # Completion response_step_ids — reject direct LOOP body templates.
        response_ids = plan.completion.response_step_ids
        if not response_ids:
            errors.append(
                _issue("PLAN_SCHEMA_INVALID", "response_step_ids must be non-empty")
            )
        elif len(set(response_ids)) != len(response_ids):
            errors.append(
                _issue("PLAN_SCHEMA_INVALID", "response_step_ids contains duplicates")
            )
        else:
            for rid in response_ids:
                if rid not in id_set:
                    errors.append(
                        _issue(
                            "PLAN_SCHEMA_INVALID",
                            f"response_step_id={rid!r} does not exist",
                        )
                    )
                elif rid in body_templates:
                    errors.append(
                        _issue(
                            "PLAN_SCHEMA_INVALID",
                            f"response_step_id={rid!r} must not name a LOOP "
                            "body template",
                        )
                    )

        # Top-level Steps must not depend directly on LOOP body templates.
        for step in plan.steps:
            if step.id in body_templates:
                continue
            for dep in step.depends_on:
                if dep in body_templates:
                    errors.append(
                        _issue(
                            "PLAN_SCHEMA_INVALID",
                            f"top-level step depends_on={dep!r} must not "
                            "reference a LOOP body template; depend on the "
                            "owning LOOP instead",
                            step_id=step.id,
                        )
                    )

        # Per-step typed config + when + bindings
        for step in plan.steps:
            self._validate_when(
                step,
                id_set=id_set,
                ancestors=ancestors,
                loop_body_owner=loop_body_owner,
                errors=errors,
            )
            self._validate_step_config(
                step,
                id_set=id_set,
                plan=plan,
                ancestors=ancestors,
                loop_body_owner=loop_body_owner,
                errors=errors,
            )

        # LOOP body depends_on scope + nesting depth
        for loop_id, cfg in loop_configs.items():
            body_set = set(cfg.body_step_ids)
            for body_id in cfg.body_step_ids:
                body_step = next((s for s in plan.steps if s.id == body_id), None)
                if body_step is None:
                    continue
                for dep in body_step.depends_on:
                    if dep != loop_id and dep not in body_set:
                        errors.append(
                            _issue(
                                "PLAN_SCHEMA_INVALID",
                                f"LOOP body step depends_on={dep!r} must reference "
                                f"the LOOP or another body step",
                                step_id=body_id,
                            )
                        )

        nesting_errors = _validate_loop_nesting(loop_configs, loop_body_owner)
        errors.extend(nesting_errors)

        # JOIN requires ≥1 upstream dependency
        for step in plan.steps:
            if step.type == AuthorableStepType.JOIN and not step.depends_on:
                errors.append(
                    _issue(
                        "PLAN_SCHEMA_INVALID",
                        "JOIN requires one or more depends_on upstream steps",
                        step_id=step.id,
                    )
                )

        return ComplexPlanValidationResult(ok=not errors, errors=errors)

    def _validate_when(
        self,
        step: ExecutionPlanStep,
        *,
        id_set: set[str],
        ancestors: dict[str, set[str]],
        loop_body_owner: dict[str, str],
        errors: list[ComplexPlanIssue],
    ) -> None:
        if step.when is None:
            return
        if not isinstance(step.when, dict):
            errors.append(
                _issue(
                    "PLAN_CONDITION_INVALID",
                    "when must be a Predicate object or null",
                    step_id=step.id,
                )
            )
            return
        try:
            pred = parse_predicate(step.when)
        except Exception as exc:
            errors.append(
                _issue(
                    "PLAN_CONDITION_INVALID",
                    f"invalid when Predicate AST: {exc}",
                    step_id=step.id,
                )
            )
            return
        self._validate_bindings(
            iter_plan_bindings_in_predicate(pred),
            owner_step_id=step.id,
            id_set=id_set,
            ancestors=ancestors,
            loop_body_owner=loop_body_owner,
            errors=errors,
            code="PLAN_BINDING_INVALID",
        )

    def _validate_step_config(
        self,
        step: ExecutionPlanStep,
        *,
        id_set: set[str],
        plan: ExecutionPlanV1,
        ancestors: dict[str, set[str]],
        loop_body_owner: dict[str, str],
        errors: list[ComplexPlanIssue],
    ) -> Any:
        if not isinstance(step.config, dict):
            errors.append(
                _issue(
                    "PLAN_SCHEMA_INVALID",
                    "step config must be an object",
                    step_id=step.id,
                )
            )
            return None

        try:
            if step.type == AuthorableStepType.TOOL:
                cfg = ComplexToolStepConfigV1.model_validate(step.config)
                self._validate_bindings(
                    list(cfg.bindings.values()),
                    owner_step_id=step.id,
                    id_set=id_set,
                    ancestors=ancestors,
                    loop_body_owner=loop_body_owner,
                    errors=errors,
                    code="PLAN_BINDING_INVALID",
                )
                return cfg
            if step.type == AuthorableStepType.JOIN:
                return JoinStepConfigV1.model_validate(step.config)
            if step.type == AuthorableStepType.APPROVAL:
                return ApprovalStepConfigV1.model_validate(step.config)
            if step.type == AuthorableStepType.CONDITION:
                cfg = ConditionStepConfigV1.model_validate(step.config)
                self._validate_bindings(
                    iter_plan_bindings_in_predicate(cfg.predicate),
                    owner_step_id=step.id,
                    id_set=id_set,
                    ancestors=ancestors,
                    loop_body_owner=loop_body_owner,
                    errors=errors,
                    code="PLAN_BINDING_INVALID",
                )
                return cfg
            if step.type == AuthorableStepType.LOOP:
                cfg = LoopStepConfigV1.model_validate(step.config)
                if cfg.max_iterations > plan.limits.max_loop_iterations:
                    errors.append(
                        _issue(
                            "PLAN_LIMIT_EXCEEDED",
                            f"LOOP max_iterations={cfg.max_iterations} exceeds "
                            f"limits.max_loop_iterations="
                            f"{plan.limits.max_loop_iterations}",
                            step_id=step.id,
                        )
                    )
                bindings: list[PlanBindingValue] = []
                if cfg.collection is not None:
                    bindings.append(cfg.collection)
                if cfg.predicate is not None:
                    bindings.extend(iter_plan_bindings_in_predicate(cfg.predicate))
                self._validate_bindings(
                    bindings,
                    owner_step_id=step.id,
                    id_set=id_set,
                    ancestors=ancestors,
                    loop_body_owner=loop_body_owner,
                    errors=errors,
                    code="PLAN_BINDING_INVALID",
                )
                return cfg
        except Exception as exc:
            code = (
                "PLAN_CONDITION_INVALID"
                if step.type
                in (AuthorableStepType.CONDITION, AuthorableStepType.LOOP)
                and "predicate" in str(exc).lower()
                else "PLAN_SCHEMA_INVALID"
            )
            if step.type == AuthorableStepType.JOIN and "policy" in str(exc).lower():
                code = "PLAN_SCHEMA_INVALID"
            errors.append(
                _issue(
                    code,
                    f"invalid {step.type.value} config: {exc}",
                    step_id=step.id,
                )
            )
            return None

        errors.append(
            _issue(
                "PLAN_SCHEMA_INVALID",
                f"unsupported step type={step.type!r}",
                step_id=step.id,
            )
        )
        return None

    def _validate_bindings(
        self,
        bindings: list[PlanBindingValue],
        *,
        owner_step_id: str,
        id_set: set[str],
        ancestors: dict[str, set[str]],
        loop_body_owner: dict[str, str],
        errors: list[ComplexPlanIssue],
        code: str,
    ) -> None:
        owner_ancestors = ancestors.get(owner_step_id, set())
        owner_loop = loop_body_owner.get(owner_step_id)
        body_templates = set(loop_body_owner)
        for binding in bindings:
            if isinstance(binding, PlanStepOutputBinding):
                if binding.step_id == owner_step_id:
                    errors.append(
                        _issue(
                            code,
                            "STEP_OUTPUT must not reference the owning step",
                            step_id=owner_step_id,
                        )
                    )
                elif binding.step_id not in id_set:
                    errors.append(
                        _issue(
                            code,
                            f"STEP_OUTPUT step_id={binding.step_id!r} does not exist",
                            step_id=owner_step_id,
                        )
                    )
                elif owner_loop is None:
                    # Top-level / non-body owner.
                    if binding.step_id in body_templates:
                        errors.append(
                            _issue(
                                code,
                                f"STEP_OUTPUT step_id={binding.step_id!r} must "
                                "not reference a LOOP body template from a "
                                "non-body Step",
                                step_id=owner_step_id,
                            )
                        )
                    elif binding.step_id not in owner_ancestors:
                        errors.append(
                            _issue(
                                code,
                                f"STEP_OUTPUT step_id={binding.step_id!r} is not "
                                f"in the transitive dependency ancestry of "
                                f"{owner_step_id!r}",
                                step_id=owner_step_id,
                            )
                        )
                else:
                    # Body template owner — same-loop ancestor or top-level
                    # transitive ancestor of the owning LOOP.
                    source_loop = loop_body_owner.get(binding.step_id)
                    if source_loop is not None and source_loop != owner_loop:
                        errors.append(
                            _issue(
                                code,
                                f"STEP_OUTPUT step_id={binding.step_id!r} belongs "
                                "to a different LOOP body",
                                step_id=owner_step_id,
                            )
                        )
                    elif source_loop == owner_loop:
                        if binding.step_id not in owner_ancestors:
                            errors.append(
                                _issue(
                                    code,
                                    f"STEP_OUTPUT step_id={binding.step_id!r} is "
                                    "not a same-iteration body dependency "
                                    f"ancestor of {owner_step_id!r}",
                                    step_id=owner_step_id,
                                )
                            )
                    else:
                        # Outside-body source must be a transitive ancestor of
                        # the owning LOOP (not an arbitrary top-level Step).
                        loop_ancestors = ancestors.get(owner_loop, set())
                        if binding.step_id != owner_loop and (
                            binding.step_id not in loop_ancestors
                        ):
                            errors.append(
                                _issue(
                                    code,
                                    f"STEP_OUTPUT step_id={binding.step_id!r} is "
                                    "not a transitive top-level ancestor of "
                                    f"owning LOOP {owner_loop!r}",
                                    step_id=owner_step_id,
                                )
                            )
            # path syntax already enforced by PlanBindingValue parsers;
            # LITERAL / SECRET_REF / PLAN_INPUT / EXECUTION_CONTEXT / LOOP_CONTEXT
            # need no additional graph checks here.
            _ = BindingKind(binding.kind)


def _issue(
    code: str,
    message: str,
    *,
    step_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> ComplexPlanIssue:
    return ComplexPlanIssue(
        code=code,
        message=message,
        step_id=step_id,
        details=details or {},
    )


def _has_dependency_cycle(steps: list[ExecutionPlanStep]) -> bool:
    ids = {s.id for s in steps}
    adj: dict[str, list[str]] = defaultdict(list)
    for step in steps:
        for dep in step.depends_on:
            if dep in ids:
                adj[dep].append(step.id)

    visited: set[str] = set()
    stack: set[str] = set()

    def visit(node: str) -> bool:
        if node in stack:
            return True
        if node in visited:
            return False
        visited.add(node)
        stack.add(node)
        for nxt in adj.get(node, []):
            if visit(nxt):
                return True
        stack.remove(node)
        return False

    return any(visit(node) for node in ids)


def _reachable_from_roots(steps: list[ExecutionPlanStep]) -> set[str]:
    ids = {s.id for s in steps}
    adj: dict[str, list[str]] = defaultdict(list)
    for step in steps:
        for dep in step.depends_on:
            if dep in ids:
                adj[dep].append(step.id)

    roots = [s.id for s in steps if not s.depends_on]
    if not roots:
        # Every step has a dependency — either a cycle or no entry; treat none reachable.
        return set()

    seen: set[str] = set()
    queue: deque[str] = deque(roots)
    while queue:
        node = queue.popleft()
        if node in seen:
            continue
        seen.add(node)
        for nxt in adj.get(node, []):
            if nxt not in seen:
                queue.append(nxt)
    return seen


def _validate_limit_hard_bounds(plan: ExecutionPlanV1) -> list[ComplexPlanIssue]:
    """Reject configured limit values outside system hard bounds."""
    errors: list[ComplexPlanIssue] = []
    checks = (
        ("max_steps", plan.limits.max_steps, SYSTEM_HARD_MAX_STEPS),
        (
            "max_duration_seconds",
            plan.limits.max_duration_seconds,
            SYSTEM_HARD_MAX_DURATION_SECONDS,
        ),
        ("max_parallelism", plan.limits.max_parallelism, SYSTEM_HARD_MAX_PARALLELISM),
        (
            "max_loop_iterations",
            plan.limits.max_loop_iterations,
            SYSTEM_HARD_MAX_LOOP_ITERATIONS,
        ),
    )
    for name, value, hard_max in checks:
        if value > hard_max:
            errors.append(
                _issue(
                    "PLAN_LIMIT_EXCEEDED",
                    f"limits.{name}={value} exceeds system hard max {hard_max}",
                )
            )
    return errors


def _transitive_ancestors(steps: list[ExecutionPlanStep]) -> dict[str, set[str]]:
    """Map each step id → set of transitive upstream dependency ids."""
    ids = {s.id for s in steps}
    direct: dict[str, list[str]] = {
        s.id: [d for d in s.depends_on if d in ids] for s in steps
    }
    cache: dict[str, set[str]] = {}

    def ancestors_of(sid: str, stack: set[str]) -> set[str]:
        if sid in cache:
            return cache[sid]
        if sid in stack:
            return set()
        stack.add(sid)
        result: set[str] = set()
        for dep in direct.get(sid, []):
            result.add(dep)
            result |= ancestors_of(dep, stack)
        stack.remove(sid)
        cache[sid] = result
        return result

    return {s.id: ancestors_of(s.id, set()) for s in steps}


def _validate_loop_nesting(
    loop_configs: dict[str, LoopStepConfigV1],
    loop_body_owner: dict[str, str],
) -> list[ComplexPlanIssue]:
    """Ensure LOOP nesting depth ≤ MAX_LOOP_NESTING_DEPTH."""
    errors: list[ComplexPlanIssue] = []

    def depth_of(loop_id: str, stack: set[str]) -> int:
        if loop_id in stack:
            return MAX_LOOP_NESTING_DEPTH + 1  # cycle among loops
        owner = loop_body_owner.get(loop_id)
        if owner is None:
            return 1
        stack.add(loop_id)
        try:
            return 1 + depth_of(owner, stack)
        finally:
            stack.remove(loop_id)

    for loop_id in loop_configs:
        depth = depth_of(loop_id, set())
        if depth > MAX_LOOP_NESTING_DEPTH:
            errors.append(
                _issue(
                    "PLAN_LIMIT_EXCEEDED",
                    f"LOOP nesting depth {depth} exceeds max {MAX_LOOP_NESTING_DEPTH}",
                    step_id=loop_id,
                )
            )
    return errors
