"""Canonical Domain enums — Source of Truth: docs/04, docs/05 (cross-checked with Frontend).

Do not invent new values here. Change docs/04–05 first when a new Canonical value is required.
"""

from __future__ import annotations

from enum import StrEnum

# Current MCP protocol version (docs/04).
CURRENT_MCP_PROTOCOL_VERSION = "2026-07-28"


class MCPServerStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    ERROR = "ERROR"


class MCPToolStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    MISSING = "MISSING"
    BLOCKED = "BLOCKED"


class ToolVersionValidationStatus(StrEnum):
    VALID = "VALID"
    INVALID = "INVALID"
    WARNING = "WARNING"


class ToolVerificationStatus(StrEnum):
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


class AgentStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    ARCHIVED = "ARCHIVED"


class AgentVersionStatus(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    DEPRECATED = "DEPRECATED"


class WorkflowStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    ARCHIVED = "ARCHIVED"


class WorkflowVersionStatus(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    DEPRECATED = "DEPRECATED"


class AgentRequestStatus(StrEnum):
    RECEIVED = "RECEIVED"
    ANALYZING = "ANALYZING"
    RETRIEVING = "RETRIEVING"
    SELECTING = "SELECTING"
    BUILDING_PARAMETERS = "BUILDING_PARAMETERS"
    PLANNING = "PLANNING"
    VALIDATING = "VALIDATING"
    WAITING_INPUT = "WAITING_INPUT"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    READY = "READY"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ExecutionStatus(StrEnum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_INPUT = "WAITING_INPUT"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    SUCCEEDED = "SUCCEEDED"
    PARTIALLY_SUCCEEDED = "PARTIALLY_SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


class StepStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_INPUT = "WAITING_INPUT"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


class ScheduleStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"


class OccurrenceStatus(StrEnum):
    PLANNED = "PLANNED"
    SKIPPED = "SKIPPED"
    ENQUEUED = "ENQUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class RiskClass(StrEnum):
    READ_ONLY = "READ_ONLY"
    IDEMPOTENT_WRITE = "IDEMPOTENT_WRITE"
    NON_IDEMPOTENT_WRITE = "NON_IDEMPOTENT_WRITE"
    DESTRUCTIVE = "DESTRUCTIVE"
    UNKNOWN = "UNKNOWN"


class ExecutionSourceType(StrEnum):
    AGENT_REQUEST = "AGENT_REQUEST"
    WORKFLOW_VERSION = "WORKFLOW_VERSION"
    SCHEDULE_OCCURRENCE = "SCHEDULE_OCCURRENCE"
    MANUAL_TOOL_TEST = "MANUAL_TOOL_TEST"
    FACTORY_TEST = "FACTORY_TEST"


class ScheduleTargetType(StrEnum):
    AGENT_VERSION = "AGENT_VERSION"
    WORKFLOW_VERSION = "WORKFLOW_VERSION"


class ScheduleOverlapPolicy(StrEnum):
    ALLOW = "ALLOW"
    SKIP = "SKIP"
    QUEUE = "QUEUE"
    REPLACE = "REPLACE"


class ScheduleMisfirePolicy(StrEnum):
    SKIP = "SKIP"
    RUN_ONCE = "RUN_ONCE"
    CATCH_UP_LIMITED = "CATCH_UP_LIMITED"


class MCPDiscoveryMode(StrEnum):
    EXPLICIT_DISCOVERY = "EXPLICIT_DISCOVERY"
    INFERRED_CURRENT = "INFERRED_CURRENT"
    LEGACY_HANDSHAKE = "LEGACY_HANDSHAKE"


class MCPTransportType(StrEnum):
    """docs/05 mcp_servers.transport_type."""

    STDIO = "STDIO"
    STREAMABLE_HTTP = "STREAMABLE_HTTP"
    LEGACY_HTTP_SSE = "LEGACY_HTTP_SSE"


class MCPProtocolEra(StrEnum):
    """docs/05 mcp_servers.protocol_era."""

    CURRENT = "CURRENT"
    LEGACY = "LEGACY"


class MCPCheckType(StrEnum):
    """docs/05 mcp_server_checks.check_type."""

    MANUAL = "MANUAL"
    SCHEDULED = "SCHEDULED"
    PRE_ACTIVATION = "PRE_ACTIVATION"


class MCPCheckStatus(StrEnum):
    """docs/05 mcp_server_checks.status."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"


class MCPAuthType(StrEnum):
    NONE = "NONE"
    BEARER = "BEARER"
    API_KEY_HEADER = "API_KEY_HEADER"
    BASIC = "BASIC"
    OAUTH2 = "OAUTH2"
    CUSTOM_HEADERS = "CUSTOM_HEADERS"
    STDIO_ENV = "STDIO_ENV"


class AuthorableStepType(StrEnum):
    """Persisted Execution Plan v1 step types. Visual PARALLEL/END are not authorable."""

    TOOL = "TOOL"
    CONDITION = "CONDITION"
    JOIN = "JOIN"
    APPROVAL = "APPROVAL"
    LOOP = "LOOP"


class JoinPolicy(StrEnum):
    ALL_SUCCESS = "ALL_SUCCESS"
    ALL_COMPLETE = "ALL_COMPLETE"
    ANY_SUCCESS = "ANY_SUCCESS"


class LoopMode(StrEnum):
    FOR_EACH = "FOR_EACH"
    WHILE = "WHILE"


class BindingKind(StrEnum):
    LITERAL = "LITERAL"
    PLAN_INPUT = "PLAN_INPUT"
    STEP_OUTPUT = "STEP_OUTPUT"
    EXECUTION_CONTEXT = "EXECUTION_CONTEXT"
    LOOP_CONTEXT = "LOOP_CONTEXT"
    SECRET_REF = "SECRET_REF"


class ParameterProvenance(StrEnum):
    USER_EXPLICIT = "USER_EXPLICIT"
    WORKFLOW_INPUT = "WORKFLOW_INPUT"
    CONVERSATION_CONFIRMED = "CONVERSATION_CONFIRMED"
    STEP_OUTPUT = "STEP_OUTPUT"
    POLICY_DEFAULT = "POLICY_DEFAULT"
    MODEL_DERIVED = "MODEL_DERIVED"
    SECRET_REFERENCE = "SECRET_REFERENCE"


class PredicateOperator(StrEnum):
    """Restricted Predicate AST operators (docs/04). Recursive, not flat left/op/right only."""

    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    CONTAINS = "contains"
    EXISTS = "exists"
    IS_NULL = "is_null"
    AND = "and"
    OR = "or"
    NOT = "not"
