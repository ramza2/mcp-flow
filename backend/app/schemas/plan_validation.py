"""Plan validation contracts — internal Agent Runtime evidence (docs/04 §11)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

VALIDATOR_VERSION: Literal["1.0"] = "1.0"

# DB-local validation decision literals — same strings as AgentRequest terminal statuses.
PlanValidationDecision = Literal[
    "READY",
    "WAITING_CONFIRMATION",
    "REJECTED",
    "FAILED",
]

PLAN_CONFIRMATION_QUESTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "confirmed": {
            "type": "boolean",
        }
    },
    "required": ["confirmed"],
    "additionalProperties": False,
}

PLAN_CONFIRMATION_PROMPT_TEXT = "생성된 실행 계획을 진행하시겠습니까?"


class PlanValidationIssue(BaseModel):
    """Structured validation issue — no chain-of-thought or internal reasoning."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    step_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class PlanValidationResult(BaseModel):
    """In-memory validation outcome before durable persistence."""

    model_config = ConfigDict(extra="forbid")

    decision: PlanValidationDecision
    errors: list[PlanValidationIssue]
    warnings: list[PlanValidationIssue] = Field(default_factory=list)
    checks_snapshot: dict[str, Any] = Field(default_factory=dict)
    policy_snapshot: dict[str, Any] = Field(default_factory=dict)
    confirmation_required: bool = False
