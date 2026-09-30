"""Plan BindingValue for complex Execution Plan v1 (docs/04 §8.2 / §10.1).

Distinct from AgentRequest Parameter Builder BindingValue
(LITERAL | SECRET_REF only in ``parameter_binding.py``).

LITERAL / SECRET_REF serialization remains byte-compatible with AgentRequest
BindingValue so existing single-TOOL plans stay semantic-compatible.
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from app.domain.enums import BindingKind

# RFC 6901 subset: non-empty, starts with '/', tokens use ~0/~1 only, no '//', no '#'.
_JSON_POINTER_RE = re.compile(r"^/(?:(?:[^~/]|~0|~1)+(?:/(?:[^~/]|~0|~1)+)*)?$")


def is_valid_json_pointer_subset(path: str) -> bool:
    """Return True if path is a non-empty RFC 6901 JSON Pointer subset."""
    if not isinstance(path, str) or not path:
        return False
    if path[0] != "/":
        return False
    if "#" in path:
        return False
    if "//" in path:
        return False
    # Allow root pointer "/" and deeper paths with valid escapes only.
    if path == "/":
        return True
    return _JSON_POINTER_RE.fullmatch(path) is not None


def _require_json_pointer(path: str) -> str:
    if not is_valid_json_pointer_subset(path):
        raise ValueError(
            "path must be a non-empty RFC 6901 JSON Pointer subset "
            "(start with '/', ~0/~1 escapes only, no empty tokens, no '#')"
        )
    return path


class PlanLiteralBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal[BindingKind.LITERAL] = BindingKind.LITERAL
    value: Any


class PlanSecretRefBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal[BindingKind.SECRET_REF] = BindingKind.SECRET_REF
    secret_id: uuid.UUID


class PlanInputBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal[BindingKind.PLAN_INPUT] = BindingKind.PLAN_INPUT
    path: str

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return _require_json_pointer(value)


class PlanStepOutputBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal[BindingKind.STEP_OUTPUT] = BindingKind.STEP_OUTPUT
    step_id: str
    path: str

    @field_validator("step_id")
    @classmethod
    def _step_id(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("step_id must be a non-empty string")
        return value

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return _require_json_pointer(value)


class PlanExecutionContextBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal[BindingKind.EXECUTION_CONTEXT] = BindingKind.EXECUTION_CONTEXT
    path: str

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return _require_json_pointer(value)


class PlanLoopContextBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal[BindingKind.LOOP_CONTEXT] = BindingKind.LOOP_CONTEXT
    path: str

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return _require_json_pointer(value)


PlanBindingValue = Annotated[
    PlanLiteralBinding
    | PlanSecretRefBinding
    | PlanInputBinding
    | PlanStepOutputBinding
    | PlanExecutionContextBinding
    | PlanLoopContextBinding,
    Field(discriminator="kind"),
]

_PLAN_BINDING_ADAPTER: TypeAdapter[PlanBindingValue] = TypeAdapter(PlanBindingValue)


def parse_plan_binding_value(payload: dict[str, Any]) -> PlanBindingValue:
    return _PLAN_BINDING_ADAPTER.validate_python(payload)
