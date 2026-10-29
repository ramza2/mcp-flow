"""SCHEDULE_OCCURRENCE workflow vs agent lineage helpers."""

from __future__ import annotations

from app.domain.enums import ExecutionSourceType
from app.models.execution import Execution


def is_workflow_schedule_occurrence(execution: Execution) -> bool:
    return (
        execution.source_type == ExecutionSourceType.SCHEDULE_OCCURRENCE.value
        and execution.schedule_occurrence_id is not None
        and execution.workflow_version_id is not None
        and execution.agent_request_id is None
        and execution.agent_version_id is None
        and execution.plan_validation_run_id is None
    )


def is_agent_schedule_occurrence(execution: Execution) -> bool:
    return (
        execution.source_type == ExecutionSourceType.SCHEDULE_OCCURRENCE.value
        and execution.schedule_occurrence_id is not None
        and execution.workflow_version_id is None
        and execution.agent_request_id is not None
        and execution.agent_version_id is not None
        and execution.plan_validation_run_id is not None
    )
