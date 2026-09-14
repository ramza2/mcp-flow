"""Parameter binding contracts for AgentRequest Parameter Builder (docs/04 §8)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

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


class ParameterBuildSnapshot(BaseModel):
    """Validated bindings map stored on ParameterBuildRun.bindings_snapshot."""

    model_config = ConfigDict(extra="forbid")

    # Tool schema property name → binding. Kept as a plain dict in DB;
    # this model validates each entry when needed.
    root: dict[str, ParameterBinding]


_BINDING_ADAPTER: TypeAdapter[BindingValue] = TypeAdapter(BindingValue)


def parse_binding_value(payload: dict[str, Any]) -> BindingValue:
    return _BINDING_ADAPTER.validate_python(payload)
