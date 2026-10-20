"""Limited entity-name alignment against authorized Tool required inputs.

Used by Tool Selector after Authorized Retrieval + rerank. Does not invent
entity values, does not hardcode synonyms, and never remaps without an
explicit alignment decision (LLM, max one call) that validates 1:1 against
missing required inputs and unused USER_EXPLICIT entities.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.enums import ParameterProvenance
from app.schemas.structured_request import (
    StructuredRequestEntity,
    StructuredRequestV1,
)

_ALIGNMENT_SYSTEM_CONTRACT = """\
You align StructuredRequest entity names to a Tool's required input names.

Return ONLY a JSON object:
{"mappings":[{"from_entity_name":"...","to_required_input":"...","rationale":"..."}]}

Rules:
- Map only when an existing USER_EXPLICIT entity value clearly fills a missing \
required input semantically.
- Never invent or change entity values.
- Never invent required input names that are not in MISSING_REQUIRED_INPUTS.
- Never map from an entity name that is not in UNUSED_ENTITY_NAMES.
- Use each from_entity_name and each to_required_input at most once.
- If unsure, return {"mappings":[]}.
- Do not return markdown or commentary.
"""


class EntityAlignmentMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_entity_name: str = Field(min_length=1)
    to_required_input: str = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=500)

    @field_validator("from_entity_name", "to_required_input", "rationale")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must be non-empty")
        return cleaned


class EntityAlignmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mappings: list[EntityAlignmentMapping] = Field(default_factory=list)


def _casefold_set(names: Sequence[str]) -> set[str]:
    return {name.strip().casefold() for name in names if name and name.strip()}


def _entity_name_casefold_counts(
    entities: Sequence[StructuredRequestEntity],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entity in entities:
        name = entity.name.strip() if entity.name else ""
        if not name:
            continue
        cf = name.casefold()
        counts[cf] = counts.get(cf, 0) + 1
    return counts


def duplicate_entity_name_casefolds(
    entities: Sequence[StructuredRequestEntity],
) -> set[str]:
    """Casefolded entity names that appear more than once (ambiguous sources)."""

    return {cf for cf, count in _entity_name_casefold_counts(entities).items() if count > 1}


def unused_user_explicit_entities(
    structured: StructuredRequestV1,
    *,
    required_inputs: Sequence[str],
    optional_inputs: Sequence[str] = (),
) -> list[StructuredRequestEntity]:
    """Entities whose names are not already Tool required/optional inputs."""

    claimed = _casefold_set(required_inputs) | _casefold_set(optional_inputs)
    unused: list[StructuredRequestEntity] = []
    for entity in structured.entities:
        name = entity.name.strip() if entity.name else ""
        if not name:
            continue
        if entity.source != ParameterProvenance.USER_EXPLICIT:
            continue
        if name.casefold() in claimed:
            continue
        unused.append(entity)
    return unused


def alignment_candidates_available(
    structured: StructuredRequestV1,
    *,
    required_inputs: Sequence[str],
    optional_inputs: Sequence[str] = (),
) -> bool:
    """True when recovery is worth attempting (unused explicit + missing required)."""

    from app.agent.selection_confidence import missing_required_inputs

    missing = missing_required_inputs(
        required_inputs=required_inputs, structured=structured
    )
    if not missing:
        return False
    unused = unused_user_explicit_entities(
        structured,
        required_inputs=required_inputs,
        optional_inputs=optional_inputs,
    )
    return bool(unused)


def build_alignment_messages(
    *,
    structured: StructuredRequestV1,
    required_inputs: Sequence[str],
    optional_inputs: Sequence[str],
    missing_required: Sequence[str],
    unused_entities: Sequence[StructuredRequestEntity],
) -> list[dict[str, str]]:
    import json

    payload = {
        "request_text": structured.request_text,
        "intent": structured.intent,
        "REQUIRED_INPUTS": [n for n in required_inputs if n and n.strip()],
        "OPTIONAL_INPUTS": [n for n in optional_inputs if n and n.strip()],
        "MISSING_REQUIRED_INPUTS": list(missing_required),
        "UNUSED_ENTITY_NAMES": [e.name for e in unused_entities],
        "UNUSED_ENTITIES": [
            {
                "name": e.name,
                "value": e.value,
                "source": e.source.value
                if hasattr(e.source, "value")
                else str(e.source),
            }
            for e in unused_entities
        ],
    }
    return [
        {"role": "system", "content": _ALIGNMENT_SYSTEM_CONTRACT},
        {
            "role": "user",
            "content": json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        },
    ]


def apply_entity_alignments(
    structured: StructuredRequestV1,
    mappings: Sequence[EntityAlignmentMapping],
    *,
    required_inputs: Sequence[str],
    optional_inputs: Sequence[str] = (),
) -> StructuredRequestV1 | None:
    """Apply validated 1:1 renames. Returns None on any fail-closed violation."""

    from app.agent.selection_confidence import missing_required_inputs

    if not mappings:
        return None

    duplicate_sources = duplicate_entity_name_casefolds(structured.entities)

    missing = missing_required_inputs(
        required_inputs=required_inputs, structured=structured
    )
    missing_cf = {name.casefold(): name for name in missing}
    unused = unused_user_explicit_entities(
        structured,
        required_inputs=required_inputs,
        optional_inputs=optional_inputs,
    )
    # Unique unused names only — duplicate casefolds are never mappable.
    unused_by_cf: dict[str, StructuredRequestEntity] = {}
    for entity in unused:
        cf = entity.name.strip().casefold()
        if cf in duplicate_sources:
            continue
        if cf in unused_by_cf:
            return None
        unused_by_cf[cf] = entity

    used_from: set[str] = set()
    used_to: set[str] = set()
    rename: dict[str, str] = {}  # old casefold -> new canonical required name

    for item in mappings:
        from_cf = item.from_entity_name.casefold()
        to_cf = item.to_required_input.casefold()
        if from_cf in used_from or to_cf in used_to:
            return None
        # Duplicate source names are ambiguous — reject that mapping set.
        if from_cf in duplicate_sources:
            return None
        if from_cf not in unused_by_cf:
            return None
        if to_cf not in missing_cf:
            return None
        used_from.add(from_cf)
        used_to.add(to_cf)
        rename[from_cf] = missing_cf[to_cf]

    # Target must not collide with any entity name that remains after rename.
    remaining_name_cfs: set[str] = set()
    for entity in structured.entities:
        name = entity.name.strip() if entity.name else ""
        if not name:
            continue
        name_cf = name.casefold()
        if name_cf in rename:
            continue
        remaining_name_cfs.add(name_cf)
    for to_cf in used_to:
        if to_cf in remaining_name_cfs:
            return None

    new_entities: list[dict[str, Any]] = []
    seen_result_names: set[str] = set()
    for entity in structured.entities:
        name = entity.name.strip() if entity.name else ""
        name_cf = name.casefold()
        if name_cf in rename:
            if entity.source != ParameterProvenance.USER_EXPLICIT:
                return None
            new_name = rename[name_cf]
            new_cf = new_name.casefold()
            if new_cf in seen_result_names:
                return None
            seen_result_names.add(new_cf)
            new_entities.append(
                {
                    "name": new_name,
                    "value": entity.value,
                    "source": ParameterProvenance.USER_EXPLICIT.value,
                }
            )
        else:
            if name_cf and name_cf in seen_result_names:
                return None
            if name_cf:
                seen_result_names.add(name_cf)
            new_entities.append(
                {
                    "name": entity.name,
                    "value": entity.value,
                    "source": entity.source.value
                    if hasattr(entity.source, "value")
                    else str(entity.source),
                }
            )

    data = structured.model_dump(mode="json")
    data["entities"] = new_entities
    try:
        return StructuredRequestV1.model_validate(data)
    except Exception:
        return None


def alignment_evidence(
    *,
    mappings: Sequence[EntityAlignmentMapping],
    tool_version_id: str,
) -> list[dict[str, str]]:
    return [
        {
            "from_entity_name": item.from_entity_name,
            "to_required_input": item.to_required_input,
            "rationale": item.rationale,
            "tool_version_id": tool_version_id,
            "value_source": ParameterProvenance.USER_EXPLICIT.value,
        }
        for item in mappings
    ]
