"""Tool selection contracts (docs/04 §7) — internal Agent Runtime only."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# Internal decision literals — not Domain enums / AgentRequest statuses.
SelectionDecision = Literal["AUTO_SELECT", "CONFIRM", "CLARIFY", "NO_MATCH"]
ProposedAction = Literal["AUTO_SELECT", "CONFIRM", "CLARIFY", "NO_MATCH"]

LLM_RERANK_INPUT_MAX = 12
# Canonical docs/04 §7 default margin gate (not an AgentVersion persisted field).
DEFAULT_AUTO_SELECT_MARGIN = 0.10
REASON_SUMMARY_MAX_LEN = 1000


def _require_non_blank(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


class LLMRerankCandidate(BaseModel):
    """One LLM-scored candidate (internal rerank response item)."""

    model_config = ConfigDict(extra="forbid")

    tool_version_id: uuid.UUID
    llm_fit_score: float = Field(ge=0.0, le=1.0)
    reason_summary: str = Field(min_length=1, max_length=REASON_SUMMARY_MAX_LEN)

    @field_validator("reason_summary")
    @classmethod
    def _reason_non_blank(cls, value: str) -> str:
        return _require_non_blank(value, field_name="reason_summary")


class LLMRerankResponse(BaseModel):
    """Internal LLM rerank payload — not persisted as Domain enum/status."""

    model_config = ConfigDict(extra="forbid")

    candidates: list[LLMRerankCandidate] = Field(min_length=1)
    ambiguities: list[str] = Field(default_factory=list)

    @field_validator("ambiguities")
    @classmethod
    def _ambiguities_non_blank(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in values:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("ambiguities items must be non-empty strings")
            cleaned.append(item)
        return cleaned

    @model_validator(mode="after")
    def _unique_candidate_ids(self) -> LLMRerankResponse:
        ids = [item.tool_version_id for item in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("LLMRerankResponse candidates must not contain duplicate IDs")
        return self


class ToolSelectionResult(BaseModel):
    """Canonical ToolSelectionResult shape (docs/04 §7) — exact field set."""

    model_config = ConfigDict(extra="forbid")

    selected_tool_version_id: uuid.UUID
    llm_fit_score: float = Field(ge=0.0, le=1.0)
    reason_summary: str = Field(min_length=1, max_length=REASON_SUMMARY_MAX_LEN)
    required_input_coverage: float = Field(ge=0.0, le=1.0)
    alternative_tool_ids: list[uuid.UUID]
    ambiguities: list[str]
    proposed_action: ProposedAction

    @field_validator("reason_summary")
    @classmethod
    def _reason_non_blank(cls, value: str) -> str:
        return _require_non_blank(value, field_name="reason_summary")

    @field_validator("ambiguities")
    @classmethod
    def _ambiguities_non_blank(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in values:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("ambiguities items must be non-empty strings")
            cleaned.append(item)
        return cleaned

    @model_validator(mode="after")
    def _alternatives_distinct(self) -> ToolSelectionResult:
        if self.selected_tool_version_id in self.alternative_tool_ids:
            raise ValueError("alternative_tool_ids must not include selected_tool_version_id")
        if len(self.alternative_tool_ids) != len(set(self.alternative_tool_ids)):
            raise ValueError("alternative_tool_ids must not contain duplicates")
        return self


class ConfidenceBreakdown(BaseModel):
    """Immutable confidence components (docs/04 §7)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    retrieval: float = Field(ge=0.0, le=1.0)
    task_fit: float = Field(ge=0.0, le=1.0)
    candidate_margin: float = Field(ge=0.0, le=1.0)
    required_input_coverage: float = Field(ge=0.0, le=1.0)
    total: float = Field(ge=0.0, le=1.0)
