"""Parameter binding contracts for AgentRequest Parameter Builder (docs/04 §8)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, TypeAdapter

from app.domain.enums import BindingKind, ParameterProvenance


class LiteralBindingValue(BaseModel):
    """Materialized non-secret value — BindingKind.LITERAL."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[BindingKind.LITERAL] = BindingKind.LITERAL
    value: Any


class SecretRefBindingValue(BaseModel):
    """Opaque secret reference — BindingKind.SECRET_REF. No material."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[BindingKind.SECRET_REF] = BindingKind.SECRET_REF
    secret_id: uuid.UUID


BindingValue = Annotated[
    LiteralBindingValue | SecretRefBindingValue,
    Field(discriminator="kind"),
]


class ParameterBinding(BaseModel):
    """One Tool parameter binding with separate provenance."""

    model_config = ConfigDict(extra="forbid")

    provenance: ParameterProvenance
    binding: BindingValue


class ParameterBuildSnapshot(RootModel[dict[str, ParameterBinding]]):
    """Tool property name → ParameterBinding.

    Durable ``ParameterBuildRun.bindings_snapshot`` is a plain map
    (no wrapper key). Validate DB JSONB with ``model_validate(snapshot)``.
    """


_BINDING_ADAPTER: TypeAdapter[BindingValue] = TypeAdapter(BindingValue)


def parse_binding_value(payload: dict[str, Any]) -> BindingValue:
    return _BINDING_ADAPTER.validate_python(payload)
