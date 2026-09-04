"""Exact Canonical Domain contract tests — docs/04, docs/05."""

from enum import Enum

import pytest
from app.domain.enums import (
    CURRENT_MCP_PROTOCOL_VERSION,
    AgentRequestStatus,
    AgentStatus,
    AgentVersionStatus,
    ApprovalStatus,
    AuthorableStepType,
    BindingKind,
    ExecutionSourceType,
    ExecutionStatus,
    JobStatus,
    JoinPolicy,
    LoopMode,
    MCPAuthType,
    MCPCheckStatus,
    MCPCheckType,
    MCPDiscoveryMode,
    MCPProtocolEra,
    MCPServerStatus,
    MCPToolStatus,
    MCPTransportType,
    OccurrenceStatus,
    ParameterProvenance,
    PredicateOperator,
    RiskClass,
    ScheduleMisfirePolicy,
    ScheduleOverlapPolicy,
    ScheduleStatus,
    ScheduleTargetType,
    StepStatus,
    ToolVerificationStatus,
    ToolVersionValidationStatus,
    WorkflowStatus,
    WorkflowVersionStatus,
)


def _values(enum_cls: type[Enum]) -> set[str]:
    return {member.value for member in enum_cls}


def expect_exact(enum_cls: type[Enum], expected: set[str]) -> None:
    actual = _values(enum_cls)
    assert actual == expected
    assert len(actual) == len(expected)


@pytest.mark.parametrize(
    ("enum_cls", "expected"),
    [
        (MCPServerStatus, {"DRAFT", "ACTIVE", "INACTIVE", "ERROR"}),
        (MCPToolStatus, {"DISCOVERED", "ACTIVE", "INACTIVE", "MISSING", "BLOCKED"}),
        (ToolVersionValidationStatus, {"VALID", "INVALID", "WARNING"}),
        (ToolVerificationStatus, {"PENDING", "VERIFIED", "FAILED", "EXPIRED"}),
        (AgentStatus, {"DRAFT", "ACTIVE", "INACTIVE", "ARCHIVED"}),
        (AgentVersionStatus, {"DRAFT", "PUBLISHED", "DEPRECATED"}),
        (WorkflowStatus, {"DRAFT", "ACTIVE", "INACTIVE", "ARCHIVED"}),
        (WorkflowVersionStatus, {"DRAFT", "PUBLISHED", "DEPRECATED"}),
        (
            AgentRequestStatus,
            {
                "RECEIVED",
                "ANALYZING",
                "RETRIEVING",
                "SELECTING",
                "BUILDING_PARAMETERS",
                "PLANNING",
                "VALIDATING",
                "WAITING_INPUT",
                "WAITING_CONFIRMATION",
                "READY",
                "REJECTED",
                "FAILED",
                "CANCELLED",
            },
        ),
        (
            ExecutionStatus,
            {
                "CREATED",
                "QUEUED",
                "RUNNING",
                "WAITING_INPUT",
                "WAITING_APPROVAL",
                "CANCEL_REQUESTED",
                "SUCCEEDED",
                "PARTIALLY_SUCCEEDED",
                "FAILED",
                "CANCELLED",
                "TIMED_OUT",
            },
        ),
        (
            StepStatus,
            {
                "PENDING",
                "READY",
                "RUNNING",
                "WAITING_INPUT",
                "WAITING_APPROVAL",
                "SUCCEEDED",
                "FAILED",
                "SKIPPED",
                "TIMED_OUT",
                "CANCELLED",
                "UNKNOWN_OUTCOME",
            },
        ),
        (ApprovalStatus, {"PENDING", "APPROVED", "REJECTED", "EXPIRED", "CANCELLED"}),
        (
            JobStatus,
            {"PENDING", "QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"},
        ),
        (ScheduleStatus, {"ACTIVE", "PAUSED", "COMPLETED", "ERROR"}),
        (
            OccurrenceStatus,
            {"PLANNED", "SKIPPED", "ENQUEUED", "RUNNING", "COMPLETED", "FAILED"},
        ),
        (
            RiskClass,
            {
                "READ_ONLY",
                "IDEMPOTENT_WRITE",
                "NON_IDEMPOTENT_WRITE",
                "DESTRUCTIVE",
                "UNKNOWN",
            },
        ),
        (
            ExecutionSourceType,
            {
                "AGENT_REQUEST",
                "WORKFLOW_VERSION",
                "SCHEDULE_OCCURRENCE",
                "MANUAL_TOOL_TEST",
                "FACTORY_TEST",
            },
        ),
        (ScheduleTargetType, {"AGENT_VERSION", "WORKFLOW_VERSION"}),
        (ScheduleOverlapPolicy, {"ALLOW", "SKIP", "QUEUE", "REPLACE"}),
        (ScheduleMisfirePolicy, {"SKIP", "RUN_ONCE", "CATCH_UP_LIMITED"}),
        (
            MCPDiscoveryMode,
            {"EXPLICIT_DISCOVERY", "INFERRED_CURRENT", "LEGACY_HANDSHAKE"},
        ),
        (
            MCPTransportType,
            {"STDIO", "STREAMABLE_HTTP", "LEGACY_HTTP_SSE"},
        ),
        (
            MCPProtocolEra,
            {"CURRENT", "LEGACY"},
        ),
        (
            MCPCheckType,
            {"MANUAL", "SCHEDULED", "PRE_ACTIVATION"},
        ),
        (
            MCPCheckStatus,
            {"SUCCEEDED", "FAILED", "TIMED_OUT"},
        ),
        (
            MCPAuthType,
            {
                "NONE",
                "BEARER",
                "API_KEY_HEADER",
                "BASIC",
                "OAUTH2",
                "CUSTOM_HEADERS",
                "STDIO_ENV",
            },
        ),
        (AuthorableStepType, {"TOOL", "CONDITION", "JOIN", "APPROVAL", "LOOP"}),
        (JoinPolicy, {"ALL_SUCCESS", "ALL_COMPLETE", "ANY_SUCCESS"}),
        (LoopMode, {"FOR_EACH", "WHILE"}),
        (
            BindingKind,
            {
                "LITERAL",
                "PLAN_INPUT",
                "STEP_OUTPUT",
                "EXECUTION_CONTEXT",
                "LOOP_CONTEXT",
                "SECRET_REF",
            },
        ),
        (
            ParameterProvenance,
            {
                "USER_EXPLICIT",
                "WORKFLOW_INPUT",
                "CONVERSATION_CONFIRMED",
                "STEP_OUTPUT",
                "POLICY_DEFAULT",
                "MODEL_DERIVED",
                "SECRET_REFERENCE",
            },
        ),
        (
            PredicateOperator,
            {
                "eq",
                "ne",
                "gt",
                "gte",
                "lt",
                "lte",
                "in",
                "contains",
                "exists",
                "is_null",
                "and",
                "or",
                "not",
            },
        ),
    ],
)
def test_canonical_enum_exact_set(enum_cls: type[Enum], expected: set[str]) -> None:
    expect_exact(enum_cls, expected)


def test_current_mcp_protocol_version() -> None:
    assert CURRENT_MCP_PROTOCOL_VERSION == "2026-07-28"


@pytest.mark.parametrize(
    "forbidden",
    ["PLANNING", "WAITING_CONFIRMATION", "REJECTED", "EXPIRED", "PARTIAL"],
)
def test_execution_excludes_forbidden_statuses(forbidden: str) -> None:
    assert forbidden not in _values(ExecutionStatus)


def test_schedule_excludes_removed_values() -> None:
    assert "INACTIVE" not in _values(ScheduleStatus)
    assert "CANCEL_RUNNING" not in _values(ScheduleOverlapPolicy)
    assert "RUN_ALL" not in _values(ScheduleMisfirePolicy)


def test_logical_agent_workflow_exclude_published() -> None:
    assert "PUBLISHED" not in _values(AgentStatus)
    assert "PUBLISHED" not in _values(WorkflowStatus)


def test_approval_excludes_waiting_approval() -> None:
    assert "WAITING_APPROVAL" not in _values(ApprovalStatus)


@pytest.mark.parametrize(
    "forbidden",
    ["USER_INPUT", "PARALLEL", "END", "SCRIPT", "PYTHON", "JAVASCRIPT"],
)
def test_authorable_step_excludes_visual_or_script_types(forbidden: str) -> None:
    assert forbidden not in _values(AuthorableStepType)
