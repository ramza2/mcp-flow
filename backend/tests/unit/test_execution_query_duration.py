"""Unit tests for Operations duration fail-closed helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.domain.enums import (
    ExecutionStatus,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
)
from app.services.execution_query import ExecutionQueryService

_NOW = datetime(2026, 10, 7, 18, 0, 0, tzinfo=UTC)
_START = _NOW - timedelta(seconds=30)


def test_execution_active_uses_now_when_unfinished() -> None:
    ms = ExecutionQueryService.duration_ms(
        _START,
        None,
        now=_NOW,
        status=ExecutionStatus.RUNNING.value,
        active_statuses=frozenset({ExecutionStatus.RUNNING.value}),
    )
    assert ms == 30_000


def test_execution_terminal_missing_finished_at_is_null() -> None:
    for status in (
        ExecutionStatus.SUCCEEDED.value,
        ExecutionStatus.PARTIALLY_SUCCEEDED.value,
        ExecutionStatus.FAILED.value,
        ExecutionStatus.CANCELLED.value,
        ExecutionStatus.TIMED_OUT.value,
    ):
        assert (
            ExecutionQueryService.duration_ms(
                _START,
                None,
                now=_NOW,
                status=status,
                active_statuses=frozenset(
                    {
                        ExecutionStatus.RUNNING.value,
                        ExecutionStatus.WAITING_INPUT.value,
                        ExecutionStatus.WAITING_APPROVAL.value,
                        ExecutionStatus.CANCEL_REQUESTED.value,
                    }
                ),
            )
            is None
        )


def test_execution_created_queued_without_started_at_is_null() -> None:
    assert (
        ExecutionQueryService.duration_ms(
            None,
            None,
            now=_NOW,
            status=ExecutionStatus.CREATED.value,
            active_statuses=frozenset({ExecutionStatus.RUNNING.value}),
        )
        is None
    )


def test_negative_duration_is_null() -> None:
    assert (
        ExecutionQueryService.duration_ms(
            _NOW,
            _START,
            now=_NOW,
            status=ExecutionStatus.SUCCEEDED.value,
            active_statuses=frozenset(),
        )
        is None
    )


def test_step_attempt_toolcall_active_vs_terminal() -> None:
    step_active = frozenset(
        {
            StepStatus.READY.value,
            StepStatus.RUNNING.value,
            StepStatus.WAITING_INPUT.value,
            StepStatus.WAITING_APPROVAL.value,
        }
    )
    assert (
        ExecutionQueryService.duration_ms(
            _START,
            None,
            now=_NOW,
            status=StepStatus.RUNNING.value,
            active_statuses=step_active,
        )
        == 30_000
    )
    assert (
        ExecutionQueryService.duration_ms(
            _START,
            None,
            now=_NOW,
            status=StepStatus.FAILED.value,
            active_statuses=step_active,
        )
        is None
    )

    attempt_active = frozenset({StepAttemptStatus.STARTED.value})
    assert (
        ExecutionQueryService.duration_ms(
            _START,
            None,
            now=_NOW,
            status=StepAttemptStatus.STARTED.value,
            active_statuses=attempt_active,
        )
        == 30_000
    )
    assert (
        ExecutionQueryService.duration_ms(
            _START,
            None,
            now=_NOW,
            status=StepAttemptStatus.SUCCEEDED.value,
            active_statuses=attempt_active,
        )
        is None
    )

    tc_active = frozenset({ToolCallNormalizedStatus.STARTED.value})
    assert (
        ExecutionQueryService.duration_ms(
            _START,
            None,
            now=_NOW,
            status=ToolCallNormalizedStatus.STARTED.value,
            active_statuses=tc_active,
        )
        == 30_000
    )
    assert (
        ExecutionQueryService.duration_ms(
            _START,
            None,
            now=_NOW,
            status=ToolCallNormalizedStatus.FAILED.value,
            active_statuses=tc_active,
        )
        is None
    )


def test_time_to_first_byte_requires_valid_order() -> None:
    assert ExecutionQueryService.time_to_first_byte_ms(None, _NOW) is None
    assert ExecutionQueryService.time_to_first_byte_ms(_START, None) is None
    assert (
        ExecutionQueryService.time_to_first_byte_ms(
            _START, _START - timedelta(milliseconds=1)
        )
        is None
    )
    assert (
        ExecutionQueryService.time_to_first_byte_ms(
            _START, _START + timedelta(milliseconds=250)
        )
        == 250
    )
