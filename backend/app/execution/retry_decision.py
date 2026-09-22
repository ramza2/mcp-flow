"""Bounded safe MCP retry decision (FNC-EXE-006).

Normal Execution-engine retry for transient MCP failures. Celery retry is not
used for MCP tool calls. Recovery (FNC-EXE-011) shares ``can_safe_retry`` /
risk helpers; this module owns the in-process runner decision for a completed
Attempt with durable MCPClientError evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.domain.enums import RiskClass, StepStatus
from app.execution.claim import _as_utc
from app.execution.recovery import can_safe_retry
from app.mcp.errors import MCPClientError

# This vertical slice auto-retries READ_ONLY only. IDEMPOTENT_WRITE remains in
# Recovery's orphan STARTED path via ``is_safe_retry_risk``, but normal runner
# retry stays fail-closed until a canonical remote idempotency contract exists
# beyond StepAttempt.idempotency_key (lineage/dedup only).
_NORMAL_AUTO_RETRY_RISKS = frozenset({RiskClass.READ_ONLY.value})


@dataclass(frozen=True, slots=True)
class SafeRetryDecision:
    schedule_retry: bool
    reason: str


def is_normal_auto_retry_risk(risk_class: str) -> bool:
    return risk_class in _NORMAL_AUTO_RETRY_RISKS


def pinned_tool_policy(policy_snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(policy_snapshot, dict):
        return {}
    tool_policy = policy_snapshot.get("tool_policy")
    return tool_policy if isinstance(tool_policy, dict) else {}


def pinned_max_attempts(policy_snapshot: dict[str, Any] | None) -> int | None:
    raw = pinned_tool_policy(policy_snapshot).get("max_attempts")
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
        return None
    return raw


def pinned_risk_class(policy_snapshot: dict[str, Any] | None) -> str | None:
    raw = pinned_tool_policy(policy_snapshot).get("risk_class")
    return raw if isinstance(raw, str) and raw else None


def pinned_backoff_policy(policy_snapshot: dict[str, Any] | None) -> Any:
    return pinned_tool_policy(policy_snapshot).get("backoff_policy")


def step_timeout_budget_exhausted(
    *,
    step_started_at: datetime | None,
    timeout_seconds: int | None,
    now: datetime,
) -> bool:
    """True when the total Step timeout budget (not per-Attempt) is exhausted."""
    if step_started_at is None or timeout_seconds is None:
        return False
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
        return True
    if timeout_seconds < 1:
        return True
    elapsed = (_as_utc(now) - _as_utc(step_started_at)).total_seconds()
    return elapsed >= float(timeout_seconds)


def remaining_step_timeout_ms(
    *,
    step_started_at: datetime | None,
    timeout_seconds: int | None,
    policy_timeout_ms: int,
    now: datetime,
) -> int:
    """Clamp the next call timeout to the remaining total Step budget."""
    if (
        not isinstance(policy_timeout_ms, int)
        or isinstance(policy_timeout_ms, bool)
        or policy_timeout_ms < 1
    ):
        return 1
    if step_started_at is None or timeout_seconds is None:
        return policy_timeout_ms
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
        return 1
    elapsed_ms = int((_as_utc(now) - _as_utc(step_started_at)).total_seconds() * 1000)
    remaining = int(timeout_seconds) * 1000 - elapsed_ms
    if remaining < 1:
        return 0
    return min(policy_timeout_ms, remaining)


def decide_safe_transient_retry(
    *,
    call_error: MCPClientError | None,
    classified_terminal: str,
    risk_class: str,
    attempt_count: int,
    max_attempts: int,
    backoff_policy: Any,
    step_started_at: datetime | None,
    timeout_seconds: int | None,
    now: datetime | None = None,
) -> SafeRetryDecision:
    """Decide whether Phase C should checkpoint for another Attempt.

    Authority is pinned policy (caller supplies snapshot-derived values) plus
    durable MCPClientError flags. Mutable live policy is revalidated only on the
    next Attempt start / final pre-send gate.
    """
    ts = now or datetime.now(UTC)

    if call_error is None:
        return SafeRetryDecision(False, "NO_ERROR")
    if classified_terminal == StepStatus.UNKNOWN_OUTCOME.value:
        return SafeRetryDecision(False, "UNKNOWN_OUTCOME")
    if classified_terminal == StepStatus.SUCCEEDED.value:
        return SafeRetryDecision(False, "SUCCEEDED")
    if not call_error.retryable:
        return SafeRetryDecision(False, "NOT_RETRYABLE")
    if not is_normal_auto_retry_risk(risk_class):
        return SafeRetryDecision(False, "UNSAFE_OR_UNSUPPORTED_RISK")
    if backoff_policy is not None:
        # No canonical backoff JSON schema exists yet — refuse to invent one.
        return SafeRetryDecision(False, "BACKOFF_POLICY_UNSUPPORTED")
    if not can_safe_retry(attempt_count=attempt_count, max_attempts=max_attempts):
        return SafeRetryDecision(False, "MAX_ATTEMPTS_EXHAUSTED")
    if step_timeout_budget_exhausted(
        step_started_at=step_started_at,
        timeout_seconds=timeout_seconds,
        now=ts,
    ):
        return SafeRetryDecision(False, "STEP_TIMEOUT_EXHAUSTED")
    return SafeRetryDecision(True, "SAFE_RETRY")
