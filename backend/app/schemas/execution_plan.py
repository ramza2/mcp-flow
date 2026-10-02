"""Execution Plan v1 contracts (docs/04 §9).

Internal Agent Runtime schema — not a public HTTP DTO.

Step ``config`` remains a plain dict in the serialized plan; typed helpers
validate exact shapes per Step Type. AgentRequest single-TOOL plans keep
``ToolStepConfigV1`` with LITERAL/SECRET_REF bindings only.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from app.domain.enums import AuthorableStepType, JoinPolicy, LoopMode
from app.schemas.parameter_binding import BindingValue
from app.schemas.plan_binding import PlanBindingValue
from app.schemas.predicate import Predicate

EXECUTION_PLAN_SCHEMA_VERSION: Literal["1.0"] = "1.0"

PlanOnError = Literal["FAIL_EXECUTION", "MARK_PARTIAL", "CONTINUE"]
PlanSuccessPolicy = Literal["ALL_REQUIRED"]

DEFAULT_MAX_STEPS = 20
DEFAULT_MAX_DURATION_SECONDS = 300
DEFAULT_MAX_PARALLELISM = 4
DEFAULT_MAX_LOOP_ITERATIONS = 50
DEFAULT_TOOL_TIMEOUT_SECONDS = 30
DETERMINISTIC_TOOL_STEP_ID = "tool_1"
DETERMINISTIC_TOOL_STEP_NAME = "Tool Step 1"
MAX_LOOP_NESTING_DEPTH = 3

# System hard bounds for Plan limits (docs/04 §9.7).
# max_parallelism is a runtime concurrency cap, not a DAG wave-width ceiling.
SYSTEM_HARD_MAX_STEPS = 100
SYSTEM_HARD_MAX_DURATION_SECONDS = 3600
SYSTEM_HARD_MAX_PARALLELISM = 32
SYSTEM_HARD_MAX_LOOP_ITERATIONS = 500


def _require_non_blank(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


class PlanSourceAgent(BaseModel):
    """AgentRequest-generated plan source (docs/04 §9)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["AGENT"] = "AGENT"
    agent_version_id: uuid.UUID


class PlanSourceWorkflow(BaseModel):
    """WorkflowVersion-owned plan source (docs/04 §9).

    Pins the owning logical Workflow only. Exact WorkflowVersion is pinned by
    Execution.workflow_version_id, not by Plan content (avoids self-referential
    create/hash semantics when cloning versions).
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["WORKFLOW"] = "WORKFLOW"
    workflow_id: uuid.UUID


PlanSource = Annotated[
    Union[PlanSourceAgent, PlanSourceWorkflow],
    Field(discriminator="type"),
]


class PlanInputDefinition(BaseModel):
    """Plan input declaration. AgentRequest foundation uses inputs={}."""

    model_config = ConfigDict(extra="forbid")

    type: str
    required: bool
    secret: bool = False


class PlanLimits(BaseModel):
    """Execution Plan v1 limits (docs/04 §9 foundation defaults)."""

    model_config = ConfigDict(extra="forbid")

    max_steps: int = Field(ge=1)
    max_duration_seconds: int = Field(ge=1)
    max_parallelism: int = Field(ge=1)
    max_loop_iterations: int = Field(ge=1)


class ToolStepConfigV1(BaseModel):
    """AgentRequest TOOL step config — executable BindingValue only.

    Provenance is tracked via PlanGenerationRun → ParameterBuildRun,
    not copied into Plan config.
    """

    model_config = ConfigDict(extra="forbid")

    tool_version_id: uuid.UUID
    bindings: dict[str, BindingValue]


class ComplexToolStepConfigV1(BaseModel):
    """Complex/Workflow TOOL step config — full Plan BindingKind set (docs/04 §9.6)."""

    model_config = ConfigDict(extra="forbid")

    tool_version_id: uuid.UUID
    bindings: dict[str, PlanBindingValue]


class JoinStepConfigV1(BaseModel):
    """JOIN step config — exact field set (docs/04 §9.3)."""

    model_config = ConfigDict(extra="forbid")

    policy: JoinPolicy


class ApprovalStepConfigV1(BaseModel):
    """APPROVAL step config — exact field set (docs/04 §9.4)."""

    model_config = ConfigDict(extra="forbid")

    approval_policy_id: uuid.UUID


class ConditionStepConfigV1(BaseModel):
    """CONDITION step config — exact field set (docs/04 §9.6)."""

    model_config = ConfigDict(extra="forbid")

    predicate: Predicate


class LoopStepConfigV1(BaseModel):
    """LOOP step config — exact field set (docs/04 §9.5)."""

    model_config = ConfigDict(extra="forbid")

    mode: LoopMode
    max_iterations: int = Field(ge=1)
    body_step_ids: list[str] = Field(min_length=1)
    collection: PlanBindingValue | None = None
    predicate: Predicate | None = None

    @field_validator("body_step_ids")
    @classmethod
    def _body_ids(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("body_step_ids must be non-empty")
        seen: set[str] = set()
        for sid in value:
            if not isinstance(sid, str) or not sid.strip():
                raise ValueError("body_step_ids entries must be non-empty strings")
            if sid in seen:
                raise ValueError(f"duplicate body_step_id={sid!r}")
            seen.add(sid)
        return value

    @model_validator(mode="after")
    def _mode_fields(self) -> LoopStepConfigV1:
        if self.mode == LoopMode.FOR_EACH:
            if self.collection is None:
                raise ValueError("FOR_EACH requires collection")
            if self.predicate is not None:
                raise ValueError("FOR_EACH forbids predicate")
        elif self.mode == LoopMode.WHILE:
            if self.predicate is None:
                raise ValueError("WHILE requires predicate")
            if self.collection is not None:
                raise ValueError("WHILE forbids collection")
        return self


def parse_tool_step_config(config: dict[str, Any]) -> ToolStepConfigV1:
    return ToolStepConfigV1.model_validate(config)


def parse_complex_tool_step_config(config: dict[str, Any]) -> ComplexToolStepConfigV1:
    return ComplexToolStepConfigV1.model_validate(config)


def parse_join_step_config(config: dict[str, Any]) -> JoinStepConfigV1:
    return JoinStepConfigV1.model_validate(config)


def parse_approval_step_config(config: dict[str, Any]) -> ApprovalStepConfigV1:
    return ApprovalStepConfigV1.model_validate(config)


def parse_condition_step_config(config: dict[str, Any]) -> ConditionStepConfigV1:
    return ConditionStepConfigV1.model_validate(config)


def parse_loop_step_config(config: dict[str, Any]) -> LoopStepConfigV1:
    return LoopStepConfigV1.model_validate(config)


class ExecutionPlanStep(BaseModel):
    """Execution Plan v1 step common structure (docs/04 §9.1)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    type: AuthorableStepType
    required: bool
    depends_on: list[str]
    when: dict[str, Any] | None = None
    timeout_seconds: int = Field(ge=1)
    on_error: PlanOnError
    config: dict[str, Any]

    @field_validator("id", "name")
    @classmethod
    def _non_blank(cls, value: str, info: ValidationInfo) -> str:
        return _require_non_blank(value, field_name=str(info.field_name))


class PlanCompletion(BaseModel):
    """Plan completion contract (docs/04 §9)."""

    model_config = ConfigDict(extra="forbid")

    success_policy: PlanSuccessPolicy
    response_step_ids: list[str]


class ExecutionPlanV1(BaseModel):
    """Canonical Execution Plan v1 — exact top-level field set only."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    goal: str
    source: PlanSource
    inputs: dict[str, PlanInputDefinition]
    limits: PlanLimits
    steps: list[ExecutionPlanStep]
    completion: PlanCompletion

    @field_validator("goal")
    @classmethod
    def _goal_non_blank(cls, value: str) -> str:
        return _require_non_blank(value, field_name="goal")


def default_plan_limits() -> PlanLimits:
    return PlanLimits(
        max_steps=DEFAULT_MAX_STEPS,
        max_duration_seconds=DEFAULT_MAX_DURATION_SECONDS,
        max_parallelism=DEFAULT_MAX_PARALLELISM,
        max_loop_iterations=DEFAULT_MAX_LOOP_ITERATIONS,
    )


def compute_plan_hash(plan_snapshot: dict[str, Any]) -> str:
    """Deterministic SHA-256 over canonical JSON of plan_snapshot only."""
    from app.core.canonical_hash import compute_canonical_json_hash

    return compute_canonical_json_hash(plan_snapshot)
