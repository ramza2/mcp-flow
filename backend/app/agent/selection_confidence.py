"""Deterministic confidence + coverage helpers (docs/04 §7)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.schemas.structured_request import StructuredRequestV1
from app.schemas.tool_selection import ConfidenceBreakdown
from app.search.tool_retrieval import RetrievedToolCandidate


def clamp_unit(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return float(value)


def require_unit(value: float, *, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    number = float(value)
    if number < 0.0 or number > 1.0:
        raise ValueError(f"{name} must be in [0.0, 1.0]")
    return number


def compute_required_input_coverage(
    *,
    required_inputs: Sequence[str],
    structured: StructuredRequestV1,
) -> float:
    """Presence-based coverage using StructuredRequest entity names (casefold)."""

    required = [item.strip() for item in required_inputs if item and item.strip()]
    if not required:
        return 1.0
    present = {
        entity.name.strip().casefold()
        for entity in structured.entities
        if entity.name and entity.name.strip()
    }
    matched = sum(1 for name in required if name.casefold() in present)
    return matched / len(required)


def compute_candidate_margin(*, top1_fit: float, top2_fit: float | None) -> float:
    """Canonical margin: top1 - top2; if no top2, margin = top1_fit."""

    top1 = require_unit(top1_fit, name="top1_fit")
    if top2_fit is None:
        return top1
    top2 = require_unit(top2_fit, name="top2_fit")
    return clamp_unit(top1 - top2)


def compute_confidence(
    *,
    retrieval: float,
    task_fit: float,
    candidate_margin: float,
    required_input_coverage: float,
) -> ConfidenceBreakdown:
    """C = 0.25*retrieval + 0.45*task_fit + 0.15*margin + 0.15*coverage."""

    r = require_unit(retrieval, name="retrieval")
    t = require_unit(task_fit, name="task_fit")
    m = require_unit(candidate_margin, name="candidate_margin")
    c = require_unit(required_input_coverage, name="required_input_coverage")
    total = clamp_unit(0.25 * r + 0.45 * t + 0.15 * m + 0.15 * c)
    return ConfidenceBreakdown(
        retrieval=r,
        task_fit=t,
        candidate_margin=m,
        required_input_coverage=c,
        total=total,
    )


def sort_reranked_candidates(
    *,
    scored: Sequence[tuple[uuid.UUID, float]],
    retrieval_by_id: dict[uuid.UUID, float],
) -> list[tuple[uuid.UUID, float]]:
    """Deterministic order: llm_fit DESC, retrieval DESC, tool_version_id ASC."""

    return sorted(
        scored,
        key=lambda item: (
            -item[1],
            -retrieval_by_id.get(item[0], 0.0),
            item[0],
        ),
    )


def missing_required_inputs(
    *,
    required_inputs: Sequence[str],
    structured: StructuredRequestV1,
) -> list[str]:
    present = {
        entity.name.strip().casefold()
        for entity in structured.entities
        if entity.name and entity.name.strip()
    }
    missing: list[str] = []
    for name in required_inputs:
        cleaned = name.strip()
        if cleaned and cleaned.casefold() not in present:
            missing.append(cleaned)
    return missing


def merge_missing_fields(
    existing: Sequence[str] | None,
    newly_missing: Sequence[str],
) -> list[str]:
    values = [str(item).strip() for item in (existing or []) if str(item).strip()]
    values.extend(item.strip() for item in newly_missing if item and item.strip())
    return sorted(set(values))


def candidate_by_tool_version_id(
    candidates: Sequence[RetrievedToolCandidate],
    tool_version_id: uuid.UUID,
) -> RetrievedToolCandidate | None:
    for candidate in candidates:
        if candidate.descriptor.tool_version_id == tool_version_id:
            return candidate
    return None
