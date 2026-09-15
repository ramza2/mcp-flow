"""Clarification / Confirmation response contracts (docs/04, docs/05 §10.4)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue

# Shared confirmation question schema used by TOOL_CONFIRMATION and PLAN_CONFIRMATION.
CONFIRMATION_QUESTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "confirmed": {
            "type": "boolean",
        }
    },
    "required": ["confirmed"],
    "additionalProperties": False,
}

TOOL_CONFIRMATION_PROMPT_TEXT = "선택된 도구의 사용을 확인해 주세요."
PLAN_CONFIRMATION_PROMPT_TEXT = "생성된 실행 계획을 진행하시겠습니까?"


class ClarificationResponseSubmit(BaseModel):
    """POST .../clarifications/{id}/responses request body."""

    model_config = ConfigDict(extra="forbid")

    response_payload: dict[str, JsonValue]


class ClarificationResponseResult(BaseModel):
    """POST .../clarifications/{id}/responses response body."""

    model_config = ConfigDict(extra="forbid")

    agent_request_id: UUID
    clarification_id: UUID
    clarification_status: str
    agent_request_status: str
