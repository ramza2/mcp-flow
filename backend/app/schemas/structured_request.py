"""StructuredRequest v1 contract (docs/04 §5).

Internal Agent Runtime schema — not a public HTTP DTO.
"""

from __future__ import annotations

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    JsonValue,
    field_validator,
    model_validator,
)

from app.domain.enums import ParameterProvenance, RiskClass

STRUCTURED_REQUEST_SCHEMA_VERSION: Literal["1.0"] = "1.0"


def _require_non_blank(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


class StructuredRequestEntity(BaseModel):
    """Entity extracted from the user request (docs/04 §5)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    value: JsonValue
    source: ParameterProvenance

    @field_validator("name")
    @classmethod
    def _name_non_blank(cls, value: str) -> str:
        return _require_non_blank(value, field_name="name")


class StructuredRequestV1(BaseModel):
    """Canonical StructuredRequest v1 — exact top-level field set only."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    request_text: str
    intent: str
    entities: list[StructuredRequestEntity]
    constraints: list[str]
    expected_outputs: list[str]
    required_capabilities: list[str]
    risk_hints: list[RiskClass]
    missing_inputs: list[str]
    ambiguities: list[str]
    needs_clarification: bool

    @field_validator("request_text")
    @classmethod
    def _request_text_non_blank(cls, value: str) -> str:
        return _require_non_blank(value, field_name="request_text")

    @field_validator("intent")
    @classmethod
    def _intent_non_blank(cls, value: str) -> str:
        return _require_non_blank(value, field_name="intent")

    @field_validator("required_capabilities", "missing_inputs", "ambiguities")
    @classmethod
    def _items_non_blank(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in values:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("list items must be non-empty strings")
            cleaned.append(item)
        return cleaned

    @model_validator(mode="after")
    def _clarification_consistency(self) -> StructuredRequestV1:
        expected = bool(self.missing_inputs or self.ambiguities)
        if self.needs_clarification != expected:
            raise ValueError(
                "needs_clarification must equal "
                "bool(missing_inputs or ambiguities)"
            )
        return self

    @classmethod
    def prompt_json_schema(cls) -> dict:
        """Deterministic JSON Schema fragment for Analyzer prompts."""

        return cls.model_json_schema()
