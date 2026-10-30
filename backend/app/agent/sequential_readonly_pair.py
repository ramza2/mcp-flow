"""AgentRequest sequential READ_ONLY Tool pair (chat phase-2).

Builds an optional second TOOL step that depends on ``tool_1`` and receives
inputs via ``STEP_OUTPUT`` bindings. Reuses Execution Plan v1 + runtime
``STEP_OUTPUT`` resolution; does not invent new Step Types or Binding kinds.

Pair emission is opt-in only when StructuredRequest clearly asks for a
follow-up detail lookup and exactly one schema-compatible companion exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.core.errors import AppError
from app.domain.enums import BindingKind, ParameterProvenance, RiskClass
from app.schemas.execution_plan import (
    DETERMINISTIC_TOOL_STEP_ID,
    DETERMINISTIC_TOOL_STEP_NAME,
)
from app.schemas.structured_request import StructuredRequestV1

_EXPLICIT_ID_PROVENANCE = frozenset(
    {
        ParameterProvenance.USER_EXPLICIT,
        ParameterProvenance.CONVERSATION_CONFIRMED,
    }
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

# Explicit follow-up detail signals (StructuredRequest fields + request_text).
_DETAIL_SIGNAL_TOKENS = (
    "detail",
    "details",
    "상세",
    "by_id",
    "by-id",
    "byid",
    "get_by_id",
    "getbyid",
    "lookup_one",
    "lookupone",
    "first_item",
    "first-item",
    "첫 번째",
    "첫번째",
    "첫번",
    "단건",
    "pet.detail",
    "pets.detail",
    "item.detail",
    "get_pet",
    "getpet",
)

# First-list-item handoff only — never used to invent an id when the user
# already named a specific pet/entity id.
_FIRST_ITEM_HANDOFF_TOKENS = (
    "first_item",
    "first-item",
    "firstitem",
    "첫 번째",
    "첫번째",
    "첫번",
)

# Plural / list-only intent that must not alone trigger a pair.
_LIST_ONLY_TOKENS = (
    "list",
    "lists",
    "목록",
    "리스트",
    "find_all",
    "findall",
    "search_all",
)



@dataclass(frozen=True, slots=True)
class SequentialHandoff:
    """One companion input filled from primary Tool result_inline."""

    target_param: str
    from_path: str  # JSON Pointer into Step.result_inline
    source_json_type: str
    target_json_type: str


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


def _json_type(schema: Any) -> str | None:
    if not isinstance(schema, dict):
        return None
    raw = schema.get("type")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _pets_item_id_schema(primary_output_schema: Any) -> dict[str, Any] | None:
    """Return ``pets.items.properties.id`` schema object when present."""
    props = _schema_properties(primary_output_schema)
    pets = props.get("pets")
    if not isinstance(pets, dict) or pets.get("type") != "array":
        return None
    items = pets.get("items")
    if not isinstance(items, dict):
        return None
    item_props = items.get("properties")
    if not isinstance(item_props, dict):
        return None
    id_schema = item_props.get("id")
    return id_schema if isinstance(id_schema, dict) else None


def _types_compatible(source_type: str, target_type: str) -> bool:
    if source_type == target_type:
        return True
    # JSON Schema number/integer widenings used by OpenAPI pets ids.
    if source_type == "integer" and target_type == "number":
        return True
    if source_type == "number" and target_type == "integer":
        # Narrowing is unsafe for auto-wiring without runtime coercion rules.
        return False
    return False


def _structured_signal_blob(structured: StructuredRequestV1) -> str:
    return " ".join(
        [
            structured.intent,
            " ".join(structured.expected_outputs),
            " ".join(structured.required_capabilities),
            " ".join(structured.constraints),
        ]
    ).lower()


def requests_sequential_detail_lookup(structured: StructuredRequestV1) -> bool:
    """True only when StructuredRequest clearly asks for a follow-up detail.

    Unclear / list-only / clarifying requests stay single TOOL (list-only).
    Composite detail intent must not silently shrink when pair construction fails
    — Plan Generator fail-closes instead.
    Uses existing StructuredRequest fields only (no new schema keys).
    Structured fields (intent / expected_outputs / capabilities) take priority
    over free-form ``request_text`` alone.
    """
    if structured.needs_clarification or structured.ambiguities:
        return False

    structured_blob = _structured_signal_blob(structured)
    text_l = structured.request_text.lower()

    if any(token in structured_blob for token in _DETAIL_SIGNAL_TOKENS):
        return True

    # List-classified structured signals never upgrade to a pair from text alone.
    if any(token in structured_blob for token in _LIST_ONLY_TOKENS):
        return False

    # Neutral structured fields may still confirm via explicit first/detail text.
    return any(token in text_l for token in _DETAIL_SIGNAL_TOKENS)


def requests_first_item_id_handoff(structured: StructuredRequestV1) -> bool:
    """True when the request explicitly asks for the first list item's detail."""
    if structured.needs_clarification or structured.ambiguities:
        return False
    blob = f"{_structured_signal_blob(structured)} {structured.request_text.lower()}"
    return any(token in blob for token in _FIRST_ITEM_HANDOFF_TOKENS)


def extract_explicit_id_entity_value(
    structured: StructuredRequestV1,
) -> Any | None:
    """Return the sole user-confirmed id-like entity value, else None.

    Multiple distinct id-like entities → ambiguous → None (do not guess).
    """
    matches: list[Any] = []
    for entity in structured.entities:
        if entity.source not in _EXPLICIT_ID_PROVENANCE:
            continue
        if _normalize_param(entity.name) not in _ID_PARAM_NAMES:
            continue
        matches.append(entity.value)
    if len(matches) != 1:
        return None
    return matches[0]


def _coerce_id_literal(value: Any, *, target_json_type: str) -> Any | None:
    """Coerce an explicit entity value to the companion JSON type without guessing."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if target_json_type == "integer":
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
        return None
    if target_json_type == "number":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return None
        return None
    if target_json_type == "string":
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        return None
    return None


def resolve_companion_id_bindings(
    *,
    structured: StructuredRequestV1,
    handoffs: tuple[SequentialHandoff, ...] | list[SequentialHandoff],
) -> dict[str, Any] | None:
    """Build tool_2 id bindings from explicit entity or first-item STEP_OUTPUT.

    Returns None when the id cannot be resolved without inventing meaning
    (no explicit id and no first-item signal).
    """
    if len(handoffs) != 1:
        return None
    handoff = handoffs[0]
    explicit = extract_explicit_id_entity_value(structured)
    if explicit is not None:
        coerced = _coerce_id_literal(
            explicit, target_json_type=handoff.target_json_type
        )
        if coerced is None:
            return None
        return {
            handoff.target_param: {
                "kind": BindingKind.LITERAL.value,
                "value": coerced,
            }
        }
    if requests_first_item_id_handoff(structured):
        return companion_step_bindings(handoffs)
    return None


def discover_id_handoffs(
    *,
    primary_output_schema: Any,
    companion_input_schema: Any,
    uses_factory_envelope: bool,
) -> list[SequentialHandoff] | None:
    """Map companion required id-like params to primary pets[0].id output.

    Returns None when the pair is not a unique, type-safe READ_ONLY handoff.
    Requires ``pets.items.properties.id`` with a JSON type compatible with the
    companion id input schema.
    """
    required = _schema_required(companion_input_schema)
    if not required:
        return None

    id_schema = _pets_item_id_schema(primary_output_schema)
    if id_schema is None:
        return None
    source_type = _json_type(id_schema)
    if source_type is None:
        return None

    id_params = [name for name in required if _normalize_param(name) in _ID_PARAM_NAMES]
    if len(id_params) != 1:
        # Phase-2: only a single id-like required param is auto-wired.
        return None
    # All required companion params must be covered by handoffs (id only for now).
    if set(required) != {id_params[0]}:
        return None

    companion_props = _schema_properties(companion_input_schema)
    target_schema = companion_props.get(id_params[0])
    target_type = _json_type(target_schema)
    if target_type is None or not _types_compatible(source_type, target_type):
        return None

    if uses_factory_envelope:
        path = "/structured_content/body/pets/0/id"
    else:
        path = "/structured_content/pets/0/id"
    return [
        SequentialHandoff(
            target_param=id_params[0],
            from_path=path,
            source_json_type=source_type,
            target_json_type=target_type,
        )
    ]


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
    companion_bindings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return ExecutionPlanV1 ``steps`` for tool_1 → tool_2.

    ``companion_bindings`` overrides default STEP_OUTPUT handoffs when the
    caller already resolved an explicit LITERAL id (or equivalent).
    """
    bindings = (
        companion_bindings
        if companion_bindings is not None
        else companion_step_bindings(handoffs)
    )
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
                "bindings": bindings,
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


def assert_usable_sequential_handoff_values(
    resolved_input: dict[str, Any],
    *,
    handoff_params: set[str] | frozenset[str],
) -> None:
    """Fail closed before MCP when a STEP_OUTPUT id handoff is empty/null.

    Missing JSON Pointer paths already fail in RuntimeBindingResolver; this
    rejects null / blank / empty-collection values that would otherwise call
    tool_2 with an unusable id.
    """
    for name in handoff_params:
        if name not in resolved_input:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    f"순차 pair tool_2 handoff {name!r}가 해석되지 않아 "
                    f"상세 조회를 시작하지 않습니다."
                ),
                status_code=409,
            )
        value = resolved_input[name]
        if value is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    f"순차 pair tool_2 handoff {name!r}가 null이라 "
                    f"상세 조회를 시작하지 않습니다."
                ),
                status_code=409,
            )
        if isinstance(value, str) and not value.strip():
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    f"순차 pair tool_2 handoff {name!r}가 비어 있어 "
                    f"상세 조회를 시작하지 않습니다."
                ),
                status_code=409,
            )
        if isinstance(value, (list, dict)) and len(value) == 0:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    f"순차 pair tool_2 handoff {name!r}가 비어 있어 "
                    f"상세 조회를 시작하지 않습니다."
                ),
                status_code=409,
            )
