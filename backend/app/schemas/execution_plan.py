"""Execution Plan v1 contracts (docs/04 §9).

Internal Agent Runtime schema — not a public HTTP DTO.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from app.domain.enums import AuthorableStepType
from app.schemas.parameter_binding import BindingValue

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


def _require_non_blank(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


class PlanSourceAgent(BaseModel):
    """AgentRequest-generated plan source (docs/04 §9)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["AGENT"] = "AGENT"
    agent_version_id: uuid.UUID


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
    source: PlanSourceAgent
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
    payload = json.dumps(
        plan_snapshot,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
