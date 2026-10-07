"""Regression: build_result_summary must not copy Tool result payloads."""

from __future__ import annotations

from types import SimpleNamespace

from app.domain.enums import ExecutionStatus, StepStatus
from app.execution.completion import build_result_summary


def test_build_result_summary_excludes_tool_result_payload() -> None:
    step = SimpleNamespace(
        step_key="tool-1",
        status=StepStatus.SUCCEEDED.value,
        result_inline={
            "secret": "super-secret-value",
            "structured_content": {"password": "x"},
            "metadata": {"Authorization": "Bearer abc"},
            "content": [{"type": "text", "text": "leak"}],
        },
    )
    plan = SimpleNamespace(
        completion=SimpleNamespace(response_step_ids=["tool-1"]),
    )
    summary = build_result_summary(
        status=ExecutionStatus.SUCCEEDED.value,
        steps=[step],  # type: ignore[list-item]
        plan=plan,  # type: ignore[arg-type]
    )
    dumped = str(summary)
    assert "super-secret-value" not in dumped
    assert "structured_content" not in dumped
    assert "Authorization" not in dumped
    assert "Bearer abc" not in dumped
    assert summary["response_steps"]["tool-1"] == {
        "status": StepStatus.SUCCEEDED.value
    }
    assert set(summary["response_steps"]["tool-1"].keys()) == {"status"}
    assert "secret" not in summary
    assert summary["status"] == ExecutionStatus.SUCCEEDED.value
