"""Execution Plan source discriminated union (AGENT / WORKFLOW)."""

from __future__ import annotations

import uuid

import pytest
from app.domain.enums import AuthorableStepType, BindingKind
from app.schemas.execution_plan import (
    EXECUTION_PLAN_SCHEMA_VERSION,
    ExecutionPlanV1,
    PlanSourceAgent,
    PlanSourceWorkflow,
    compute_plan_hash,
    default_plan_limits,
)
from pydantic import ValidationError


def _agent_plan_dict(*, agent_version_id: uuid.UUID | None = None) -> dict:
    av = agent_version_id or uuid.UUID("11111111-1111-1111-1111-111111111111")
    tv = uuid.UUID("22222222-2222-2222-2222-222222222222")
    return {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "goal": "agent plan regression",
        "source": {"type": "AGENT", "agent_version_id": str(av)},
        "inputs": {},
        "limits": default_plan_limits().model_dump(mode="json"),
        "steps": [
            {
                "id": "tool_1",
                "name": "Tool Step 1",
                "type": AuthorableStepType.TOOL.value,
                "required": True,
                "depends_on": [],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {
                    "tool_version_id": str(tv),
                    "bindings": {
                        "location": {
                            "kind": BindingKind.LITERAL.value,
                            "value": "Seoul",
                        }
                    },
                },
            }
        ],
        "completion": {
            "success_policy": "ALL_REQUIRED",
            "response_step_ids": ["tool_1"],
        },
    }


def test_plan_source_agent_parses() -> None:
    av = uuid.uuid4()
    source = PlanSourceAgent.model_validate(
        {"type": "AGENT", "agent_version_id": str(av)}
    )
    assert source.type == "AGENT"
    assert source.agent_version_id == av


def test_plan_source_workflow_parses() -> None:
    wf = uuid.uuid4()
    source = PlanSourceWorkflow.model_validate(
        {"type": "WORKFLOW", "workflow_id": str(wf)}
    )
    assert source.type == "WORKFLOW"
    assert source.workflow_id == wf


def test_plan_source_mixed_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        PlanSourceWorkflow.model_validate(
            {
                "type": "WORKFLOW",
                "workflow_id": str(uuid.uuid4()),
                "agent_version_id": str(uuid.uuid4()),
            }
        )
    with pytest.raises(ValidationError):
        PlanSourceAgent.model_validate(
            {
                "type": "AGENT",
                "agent_version_id": str(uuid.uuid4()),
                "workflow_id": str(uuid.uuid4()),
            }
        )
    with pytest.raises(ValidationError):
        ExecutionPlanV1.model_validate(
            {
                **_agent_plan_dict(),
                "source": {
                    "type": "WORKFLOW",
                    "workflow_id": str(uuid.uuid4()),
                    "agent_version_id": str(uuid.uuid4()),
                },
            }
        )
    with pytest.raises(ValidationError):
        ExecutionPlanV1.model_validate(
            {
                **_agent_plan_dict(),
                "source": {
                    "type": "AGENT",
                    "agent_version_id": str(uuid.uuid4()),
                    "workflow_id": str(uuid.uuid4()),
                },
            }
        )


def test_agent_plan_still_validates_as_execution_plan_v1() -> None:
    av = uuid.UUID("11111111-1111-1111-1111-111111111111")
    plan = ExecutionPlanV1.model_validate(_agent_plan_dict(agent_version_id=av))
    assert plan.source.type == "AGENT"
    assert plan.source.agent_version_id == av

    dumped = plan.model_dump(mode="json")
    assert dumped["source"] == {
        "type": "AGENT",
        "agent_version_id": str(av),
    }
    assert "workflow_id" not in dumped["source"]

    reconstructed = PlanSourceAgent(
        type="AGENT",
        agent_version_id=av,
    )
    assert reconstructed.agent_version_id == av

    h1 = compute_plan_hash(dumped)
    h2 = compute_plan_hash(
        ExecutionPlanV1.model_validate(_agent_plan_dict(agent_version_id=av)).model_dump(
            mode="json"
        )
    )
    assert h1 == h2
    assert len(h1) == 64


def test_workflow_plan_source_round_trip() -> None:
    wf = uuid.uuid4()
    body = _agent_plan_dict()
    body["source"] = {"type": "WORKFLOW", "workflow_id": str(wf)}
    plan = ExecutionPlanV1.model_validate(body)
    assert plan.source.type == "WORKFLOW"
    assert plan.source.workflow_id == wf
    dumped = plan.model_dump(mode="json")
    assert dumped["source"] == {"type": "WORKFLOW", "workflow_id": str(wf)}
    assert "agent_version_id" not in dumped["source"]
