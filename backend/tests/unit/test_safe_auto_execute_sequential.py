"""safe_auto_execute eligibility for single and sequential READ_ONLY plans."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from app.api.v1.agent_requests import (
    AgentRunPlanView,
    _safe_auto_execute,
)


def _plan(*, step_count: int) -> AgentRunPlanView:
    return AgentRunPlanView(
        id=uuid.uuid4(),
        plan_hash="abc",
        goal="test",
        step_names=["Tool Step 1"] if step_count == 1 else ["Tool Step 1", "Tool Step 2"],
        step_count=step_count,
        created_at=datetime.now(UTC),
    )


def test_safe_auto_execute_single_readonly() -> None:
    request = SimpleNamespace(status="READY")
    validation = SimpleNamespace(
        confirmation_required=False,
        policy_snapshot={
            "tool_policy": {
                "risk_class": "READ_ONLY",
                "requires_approval": False,
                "requires_confirmation": False,
            }
        },
    )
    assert _safe_auto_execute(request, _plan(step_count=1), validation) is True


def test_safe_auto_execute_sequential_pair() -> None:
    request = SimpleNamespace(status="READY")
    validation = SimpleNamespace(
        confirmation_required=False,
        policy_snapshot={
            "schema_version": "agent_request_sequential_policy.v1",
            "agent_version_id": str(uuid.uuid4()),
            "tool_steps": {
                "tool_1": {
                    "tool_version_id": str(uuid.uuid4()),
                    "policy": {
                        "tool_policy": {
                            "risk_class": "READ_ONLY",
                            "requires_approval": False,
                            "requires_confirmation": False,
                        }
                    },
                },
                "tool_2": {
                    "tool_version_id": str(uuid.uuid4()),
                    "policy": {
                        "tool_policy": {
                            "risk_class": "READ_ONLY",
                            "requires_approval": False,
                            "requires_confirmation": False,
                        }
                    },
                },
            },
        },
    )
    assert _safe_auto_execute(request, _plan(step_count=2), validation) is True


def test_safe_auto_execute_rejects_pair_with_approval() -> None:
    request = SimpleNamespace(status="READY")
    validation = SimpleNamespace(
        confirmation_required=False,
        policy_snapshot={
            "schema_version": "agent_request_sequential_policy.v1",
            "agent_version_id": str(uuid.uuid4()),
            "tool_steps": {
                "tool_1": {
                    "tool_version_id": str(uuid.uuid4()),
                    "policy": {
                        "tool_policy": {
                            "risk_class": "READ_ONLY",
                            "requires_approval": False,
                            "requires_confirmation": False,
                        }
                    },
                },
                "tool_2": {
                    "tool_version_id": str(uuid.uuid4()),
                    "policy": {
                        "tool_policy": {
                            "risk_class": "READ_ONLY",
                            "requires_approval": True,
                            "requires_confirmation": False,
                        }
                    },
                },
            },
        },
    )
    assert _safe_auto_execute(request, _plan(step_count=2), validation) is False
