"""AgentRequest sequential READ_ONLY Tool pair (chat phase-2).

Builds an optional second TOOL step that depends on ``tool_1`` and receives
inputs via ``STEP_OUTPUT`` bindings from an unambiguous list item id.
Reuses Execution Plan v1 and runtime ``STEP_OUTPUT`` resolution; does not invent
new Step Types or Binding kinds.

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


def _is_id_like_param(name: str) -> bool:
    """Recognize known ids and common OpenAPI entityId / entity_id forms.

    Do not treat arbitrary words ending with lowercase "id" (e.g. "grid")
    as identifier parameters.
    """
    if _normalize_param(name) in _ID_PARAM_NAMES:
        return True
    return name.endswith(("_id", "-id", "Id")) and len(name) > 2


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


def _has_detail_signal(structured: StructuredRequestV1) -> bool:
    structured_blob = _structured_signal_blob(structured)
    text_l = structured.request_text.lower()
    if any(token in structured_blob for token in _DETAIL_SIGNAL_TOKENS):
        return True
    if any(token in structured_blob for token in _LIST_ONLY_TOKENS):
        # List-classified structured signals never upgrade from text alone.
        return False
    return any(token in text_l for token in _DETAIL_SIGNAL_TOKENS)


def _has_list_signal(structured: StructuredRequestV1) -> bool:
    structured_blob = _structured_signal_blob(structured)
    text_l = structured.request_text.lower()
    if any(token in structured_blob for token in _LIST_ONLY_TOKENS):
        return True
    return any(token in text_l for token in _LIST_ONLY_TOKENS)


def requests_sequential_detail_lookup(structured: StructuredRequestV1) -> bool:
    """True when StructuredRequest asks for a detail (pair or single by-id).

    Unclear / list-only / clarifying requests stay False.
    Uses existing StructuredRequest fields only (no new schema keys).
    """
    if structured.needs_clarification or structured.ambiguities:
        return False
    return _has_detail_signal(structured)


def requests_sequential_list_detail_pair(structured: StructuredRequestV1) -> bool:
    """True when list→detail pair is required (not detail-by-id alone).

    Pair-eligible:
    - first-item detail handoff, or
    - both list and detail signals (composite).

    Detail-only with an explicit id stays single TOOL (GET-by-id).
    List-only stays single TOOL.
    """
    if structured.needs_clarification or structured.ambiguities:
        return False
    if requests_first_item_id_handoff(structured):
        return True
    return _has_detail_signal(structured) and _has_list_signal(structured)


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
        if not _is_id_like_param(entity.name):
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


def companion_literal_matches_explicit_id(
    structured: StructuredRequestV1,
    *,
    param_name: str,
    literal_value: Any,
    target_json_type: str,
) -> bool:
    """True when a tool_2 LITERAL equals the sole evidenced explicit id entity.

    Rejects arbitrary LITERAL values that are not grounded in
    USER_EXPLICIT / CONVERSATION_CONFIRMED id-like StructuredRequest entities.
    """
    if not _is_id_like_param(param_name):
        return False
    explicit = extract_explicit_id_entity_value(structured)
    if explicit is None:
        return False
    expected = _coerce_id_literal(explicit, target_json_type=target_json_type)
    actual = _coerce_id_literal(literal_value, target_json_type=target_json_type)
    if expected is None or actual is None:
        return False
    return expected == actual


def companion_param_json_type(input_schema: Any, param_name: str) -> str | None:
    """JSON Schema type for a companion input property, if present."""
    props = _schema_properties(input_schema)
    return _json_type(props.get(param_name))


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


def _json_pointer_token(name: str) -> str:
    """Escape a JSON object key according to RFC 6901."""
    return name.replace("~", "~0").replace("/", "~1")


def discover_id_handoffs(
    *,
    primary_output_schema: Any,
    companion_input_schema: Any,
    uses_factory_envelope: bool,
) -> list[SequentialHandoff] | None:
    """Discover an unambiguous collection[0] id → detail id handoff.

    Supports any top-level object array with an object item whose id field is
    named "id" or exactly matches the companion id parameter, including
    common camelCase/snake_case names. No nested traversal, ref guessing,
    non-id parameters, or selection among multiple eligible paths.
    """
    required = _schema_required(companion_input_schema)
    if len(required) != 1 or not _is_id_like_param(required[0]):
        return None
    target_param = required[0]
    target_type = _json_type(
        _schema_properties(companion_input_schema).get(target_param)
    )
    if target_type not in {"string", "integer", "number"}:
        return None

    source_candidates: list[tuple[str, str, str | None]] = []
    for collection_name, collection_schema in _schema_properties(
        primary_output_schema
    ).items():
        if not isinstance(collection_schema, dict):
            continue
        if collection_schema.get("type") != "array":
            continue
        items = collection_schema.get("items")
        item_props = _schema_properties(items)
        for id_name, id_schema in item_props.items():
            if _normalize_param(id_name) not in {
                "id",
                _normalize_param(target_param),
            }:
                continue
            source_candidates.append(
                (collection_name, id_name, _json_type(id_schema))
            )

    # Never infer which collection or id property the user meant.
    if len(source_candidates) != 1:
        return None
    collection_name, id_name, source_type = source_candidates[0]
    if source_type is None or not _types_compatible(source_type, target_type):
        return None

    prefix = (
        "/structured_content/body"
        if uses_factory_envelope
        else "/structured_content"
    )
    path = (
        f"{prefix}/{_json_pointer_token(collection_name)}"
        f"/0/{_json_pointer_token(id_name)}"
    )
    return [
        SequentialHandoff(
            target_param=target_param,
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
