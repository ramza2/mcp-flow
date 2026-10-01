"""Sequential TOOL ErrorPolicy + ALL_REQUIRED completion (docs/04 §9.2).

Internal disposition / aggregation only — not a public API enum.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from app.domain.enums import ExecutionStatus, StepStatus
from app.execution.loop_runtime import top_level_plan_steps
from app.models.execution import ExecutionStep
from app.schemas.execution_plan import ExecutionPlanStep, ExecutionPlanV1

# Internal runner→orchestrator disposition (not persisted / not API-visible).
StepRunDisposition = Literal[
    "SUCCESS",
    "KNOWN_STEP_FAILURE",
    "FATAL_EXECUTION_FAILURE",
    "WAIT",
    "RETRY",
    "NOOP",
]

DISPOSITION_SUCCESS: StepRunDisposition = "SUCCESS"
DISPOSITION_KNOWN_STEP_FAILURE: StepRunDisposition = "KNOWN_STEP_FAILURE"
DISPOSITION_FATAL_EXECUTION_FAILURE: StepRunDisposition = "FATAL_EXECUTION_FAILURE"
DISPOSITION_WAIT: StepRunDisposition = "WAIT"
DISPOSITION_RETRY: StepRunDisposition = "RETRY"
DISPOSITION_NOOP: StepRunDisposition = "NOOP"

_SKIP_REASON_CODE = "UPSTREAM_EXECUTION_STOPPED"
_SKIP_REASON_MESSAGE = "Upstream Execution stopped; downstream Step was not started."

_FAILURE_LIKE = frozenset(
    {
        StepStatus.FAILED.value,
        StepStatus.TIMED_OUT.value,
        StepStatus.UNKNOWN_OUTCOME.value,
        StepStatus.CANCELLED.value,
    }
)

_CONTINUABLE_ON_ERROR = frozenset({"MARK_PARTIAL", "CONTINUE"})
_KNOWN_FAILURE_STATUSES = frozenset(
    {
        StepStatus.FAILED.value,
        StepStatus.TIMED_OUT.value,
    }
)


@dataclass(frozen=True, slots=True)
class CompletionDecision:
    status: str
    error_code: str | None = None
    error_message: str | None = None


def is_continuable_known_failure(*, on_error: str, step_status: str) -> bool:
    """True when linear progression may continue after this Step terminal."""
    return (
        on_error in _CONTINUABLE_ON_ERROR
        and step_status in _KNOWN_FAILURE_STATUSES
    )


def fail_fast_execution_status(step_status: str) -> str:
    """Map FAIL_EXECUTION Step terminal → Execution terminal."""
    if step_status == StepStatus.TIMED_OUT.value:
        return ExecutionStatus.TIMED_OUT.value
    return ExecutionStatus.FAILED.value


def skip_remaining_pending(
    *,
    ordered_step_keys: Sequence[str],
    after_step_key: str,
    by_key: Mapping[str, ExecutionStep],
    now: Any,
) -> list[str]:
    """PENDING → SKIPPED for untouched downstream Steps. Returns skipped keys."""
    skipped: list[str] = []
    seen_after = False
    for key in ordered_step_keys:
        if key == after_step_key:
            seen_after = True
            continue
        if not seen_after:
            continue
        step = by_key[key]
        if step.status != StepStatus.PENDING.value:
            continue
        step.status = StepStatus.SKIPPED.value
        step.error_code = _SKIP_REASON_CODE
        step.error_message = _SKIP_REASON_MESSAGE
        step.finished_at = now
        step.lock_version += 1
        skipped.append(key)
    return skipped


_INTENTIONAL_CONDITIONAL_SKIP_CODES = frozenset(
    {
        "STEP_WHEN_FALSE",
        "UPSTREAM_CONDITION_SKIPPED",
    }
)


def is_intentional_conditional_skip_for_completion(step: ExecutionStep) -> bool:
    """True when SKIPPED is intentional control-flow (neutral for ALL_REQUIRED)."""
    return (
        step.status == StepStatus.SKIPPED.value
        and step.error_code in _INTENTIONAL_CONDITIONAL_SKIP_CODES
    )


def aggregate_all_required(
    *,
    plan: ExecutionPlanV1,
    steps: Sequence[ExecutionStep],
) -> CompletionDecision:
    """Natural end-of-chain aggregation under ``success_policy=ALL_REQUIRED``.

    ``response_step_ids`` are ignored for success criteria.

    Required Steps intentionally skipped by conditional control flow
    (``STEP_WHEN_FALSE`` / ``UPSTREAM_CONDITION_SKIPPED``) are treated as
    neutral/satisfied for required-completion. Fail-fast
    ``UPSTREAM_EXECUTION_STOPPED`` and other unexpected SKIPPED remain
    non-success.

    Top-level Plan Steps (not LOOP body templates) are matched by
    ``step_key == plan id``. Dynamic LOOP body instances are evaluated from
    their ``step_snapshot`` required/on_error — missing body-template rows
    do not fail aggregation.
    """
    any_succeeded = any(s.status == StepStatus.SUCCEEDED.value for s in steps)
    mark_partial_failure = False
    continue_required_failure = False
    required_all_succeeded = True

    def _apply(ps: ExecutionPlanStep, step: ExecutionStep) -> None:
        nonlocal mark_partial_failure, continue_required_failure, required_all_succeeded
        failed = step.status in _FAILURE_LIKE
        if ps.required:
            if step.status == StepStatus.SUCCEEDED.value:
                pass
            elif is_intentional_conditional_skip_for_completion(step):
                # Intentional branch not selected — do not fail ALL_REQUIRED.
                pass
            else:
                required_all_succeeded = False
        if not failed:
            return
        if ps.on_error == "MARK_PARTIAL":
            mark_partial_failure = True
        if ps.required and ps.on_error == "CONTINUE":
            continue_required_failure = True

    # 1) Top-level ExecutionSteps ↔ top-level Plan Steps (exclude body templates).
    top_level_by_key = {
        s.step_key: s
        for s in steps
        if getattr(s, "parent_step_id", None) is None
    }
    for ps in top_level_plan_steps(plan):
        step = top_level_by_key.get(ps.id)
        if step is None:
            required_all_succeeded = False
            continue
        _apply(ps, step)

    # 2) Dynamic LOOP body instances — use step_snapshot required/on_error.
    for step in steps:
        if getattr(step, "parent_step_id", None) is None:
            continue
        try:
            ps = ExecutionPlanStep.model_validate(step.step_snapshot)
        except Exception:
            required_all_succeeded = False
            continue
        _apply(ps, step)

    if required_all_succeeded and not mark_partial_failure:
        return CompletionDecision(status=ExecutionStatus.SUCCEEDED.value)

    if any_succeeded and (mark_partial_failure or continue_required_failure):
        return CompletionDecision(status=ExecutionStatus.PARTIALLY_SUCCEEDED.value)

    # Unsatisfied ALL_REQUIRED without partial success evidence.
    return CompletionDecision(
        status=ExecutionStatus.FAILED.value,
        error_code="ALL_REQUIRED_UNSATISFIED",
        error_message="ALL_REQUIRED completion policy was not satisfied.",
    )


def build_result_summary(
    *,
    status: str,
    steps: Sequence[ExecutionStep],
    plan: ExecutionPlanV1 | None = None,
) -> dict[str, Any]:
    """Minimal safe Execution.result_summary (no secrets / policy / MRTR)."""
    summary: dict[str, Any] = {
        "status": status,
        "step_keys": [s.step_key for s in steps],
        "step_count": len(steps),
        "step_statuses": {s.step_key: s.status for s in steps},
    }
    if plan is not None and plan.completion.response_step_ids:
        # Response selection metadata only — never success criteria.
        selected: dict[str, Any] = {}
        by_key = {s.step_key: s for s in steps}
        for sid in plan.completion.response_step_ids:
            step = by_key.get(sid)
            if step is None or step.status != StepStatus.SUCCEEDED.value:
                continue
            if step.result_inline is None:
                continue
            selected[sid] = {"status": step.status}
        if selected:
            summary["response_steps"] = selected
    return summary


def plan_step_for(step: ExecutionStep) -> ExecutionPlanStep:
    return ExecutionPlanStep.model_validate(step.step_snapshot)
