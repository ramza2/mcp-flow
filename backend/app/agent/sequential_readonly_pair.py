"""AgentRequest sequential READ_ONLY Tool pair (chat phase-2).

Builds an optional second TOOL step that depends on ``tool_1`` and receives
inputs via ``STEP_OUTPUT`` bindings. Reuses Execution Plan v1 + runtime
``STEP_OUTPUT`` resolution; does not invent new Step Types or Binding kinds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.domain.enums import BindingKind, RiskClass
from app.schemas.execution_plan import (
    DETERMINISTIC_TOOL_STEP_ID,
    DETERMINISTIC_TOOL_STEP_NAME,
)

DETERMINISTIC_TOOL_STEP_ID_2 = "tool_2"
DETERMINISTIC_TOOL_STEP_NAME_2 = "Tool Step 2"

# Pinned Execution.policy_snapshot for AgentRequest 2-TOOL sequential plans.
AGENT_REQUEST_SEQUENTIAL_POLICY_SCHEMA = "agent_request_sequential_policy.v1"

_ID_PARAM_NAMES = frozenset(
    {
        "id",
        "petid",
        "pet_id",
        "itemid",
        "item_id",
        "entityid",
        "entity_id",
        "objectid",
        "object_id",
    }
)


@dataclass(frozen=True, slots=True)
class SequentialHandoff:
    """One companion input filled from primary Tool result_inline."""

    target_param: str
    from_path: str  # JSON Pointer into Step.result_inline


@dataclass(frozen=True, slots=True)
class SequentialReadOnlyPairSpec:
    companion_tool_version_id: UUID
    companion_display_name: str | None
    handoffs: tuple[SequentialHandoff, ...]
    uses_factory_envelope: bool


def _normalize_param(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch not in {"_", "-", " "})


def _schema_properties(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return {}
    props = schema.get("properties")
    return props if isinstance(props, dict) else {}


def _schema_required(schema: Any) -> list[str]:
    if not isinstance(schema, dict):
        return []
    required = schema.get("required")
    if not isinstance(required, list):
        return []
    return [name for name in required if isinstance(name, str)]


def _has_pets_array(schema: Any) -> bool:
    props = _schema_properties(schema)
    pets = props.get("pets")
    if not isinstance(pets, dict):
        return False
    return pets.get("type") == "array"


def discover_id_handoffs(
    *,
    primary_output_schema: Any,
    companion_input_schema: Any,
    uses_factory_envelope: bool,
) -> list[SequentialHandoff] | None:
    """Map companion required id-like params to primary pets[0].id output.

    Returns None when the pair is not a unique, safe READ_ONLY handoff.
    """
    required = _schema_required(companion_input_schema)
    if not required:
        return None
    if not _has_pets_array(primary_output_schema):
        return None

    id_params = [name for name in required if _normalize_param(name) in _ID_PARAM_NAMES]
    if len(id_params) != 1:
        # Phase-2: only a single id-like required param is auto-wired.
        return None
    # All required companion params must be covered by handoffs (id only for now).
    if set(required) != {id_params[0]}:
        return None

    if uses_factory_envelope:
        path = "/structured_content/body/pets/0/id"
    else:
        path = "/structured_content/pets/0/id"
    return [SequentialHandoff(target_param=id_params[0], from_path=path)]


def companion_step_bindings(
    handoffs: tuple[SequentialHandoff, ...] | list[SequentialHandoff],
) -> dict[str, Any]:
    """Serialize STEP_OUTPUT bindings for tool_2 config."""
    return {
        item.target_param: {
            "kind": BindingKind.STEP_OUTPUT.value,
            "step_id": DETERMINISTIC_TOOL_STEP_ID,
            "path": item.from_path,
        }
        for item in handoffs
    }


def build_sequential_pair_steps(
    *,
    primary_tool_version_id: UUID,
    primary_bindings: dict[str, Any],
    primary_timeout_seconds: int,
    companion_tool_version_id: UUID,
    companion_timeout_seconds: int,
    handoffs: tuple[SequentialHandoff, ...] | list[SequentialHandoff],
) -> list[dict[str, Any]]:
    """Return ExecutionPlanV1 ``steps`` for tool_1 → tool_2."""
    return [
        {
            "id": DETERMINISTIC_TOOL_STEP_ID,
            "name": DETERMINISTIC_TOOL_STEP_NAME,
            "type": "TOOL",
            "required": True,
            "depends_on": [],
            "when": None,
            "timeout_seconds": primary_timeout_seconds,
            "on_error": "FAIL_EXECUTION",
            "config": {
                "tool_version_id": str(primary_tool_version_id),
                "bindings": primary_bindings,
            },
        },
        {
            "id": DETERMINISTIC_TOOL_STEP_ID_2,
            "name": DETERMINISTIC_TOOL_STEP_NAME_2,
            "type": "TOOL",
            "required": True,
            "depends_on": [DETERMINISTIC_TOOL_STEP_ID],
            "when": None,
            "timeout_seconds": companion_timeout_seconds,
            "on_error": "FAIL_EXECUTION",
            "config": {
                "tool_version_id": str(companion_tool_version_id),
                "bindings": companion_step_bindings(handoffs),
            },
        },
    ]


def is_safe_readonly_policy(policy: Any) -> bool:
    if policy is None:
        return False
    if getattr(policy, "risk_class", None) != RiskClass.READ_ONLY.value:
        return False
    if bool(getattr(policy, "requires_confirmation", True)):
        return False
    if bool(getattr(policy, "requires_approval", True)):
        return False
    return True


def build_agent_request_sequential_policy_snapshot(
    *,
    agent_version_id: UUID,
    tool_steps: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Per-TOOL pinned policy snapshot for AgentRequest sequential pairs."""
    return {
        "schema_version": AGENT_REQUEST_SEQUENTIAL_POLICY_SCHEMA,
        "agent_version_id": str(agent_version_id),
        "tool_steps": tool_steps,
    }
