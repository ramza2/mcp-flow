"""Frozen rubric for approved L3-02 natural-language analysis (not Tool mapping)."""

from __future__ import annotations

from typing import Any

from app.schemas.structured_request import StructuredRequestV1

# Reviewable capability equivalence sets. Never extend aliases based on test output.
_RUBRIC: dict[str, dict[str, Any]] = {
    "weather_lookup": {
        "capabilities": {
            "weather.lookup", "weather.get", "weather.read", "weather.query",
            "weather.check", "weather.current", "weather.info", "weather.forecast",
        },
        "intent_terms": ("날씨", "기상", "weather"),
        "risk": {"READ_ONLY"},
        "clarification": False,
    },
    "calendar_create": {
        "capabilities": {
            "calendar.create", "calendar.event.create", "calendar.events.create",
            "schedule.create", "schedule.event.create", "calendar.add",
            "calendar.event.add", "schedule.add",
        },
        "intent_terms": ("일정", "회의", "미팅", "약속", "이벤트", "calendar", "schedule", "event"),
        "risk": {"IDEMPOTENT_WRITE", "NON_IDEMPOTENT_WRITE"},
        "clarification": False,
    },
    "calendar_list": {
        "capabilities": {
            "calendar.list", "calendar.event.list", "calendar.events.list",
            "calendar.lookup", "calendar.search", "calendar.event.search",
            "schedule.list", "schedule.lookup", "schedule.search",
            "schedule.events.list",
        },
        "intent_terms": ("일정", "회의", "캘린더", "이벤트", "calendar", "schedule", "event"),
        "risk": {"READ_ONLY"},
        "clarification": False,
    },
    "echo_text": {
        "capabilities": {
            "text.echo", "message.echo", "echo", "text.repeat", "text.return",
            "message.repeat", "text.display", "text.output",
        },
        "intent_terms": ("반환", "그대로", "에코", "echo", "반복", "출력", "보여", "재출력"),
        "risk": {"READ_ONLY"},
        "clarification": False,
    },
    "ambiguous": {
        "capabilities": set(),
        "intent_terms": (),
        "risk": set(),
        "clarification": True,
    },
}


def known_families() -> set[str]:
    return set(_RUBRIC)


def _normalized_capability(value: str) -> str:
    return value.casefold().strip().replace("_", ".").replace("/", ".")


def score_case(case: dict[str, Any], structured: StructuredRequestV1) -> dict[str, Any]:
    """All-or-nothing case score with deterministic, inspectable subchecks."""
    rubric = _RUBRIC[case["family"]]
    expected_clarification = bool(rubric["clarification"])
    actual_caps = {_normalized_capability(x) for x in structured.required_capabilities}
    accepted_caps = rubric["capabilities"]
    capability_ok = (
        not actual_caps
        if expected_clarification
        else bool(actual_caps & accepted_caps)
    )
    intent_ok = (
        True
        if expected_clarification
        else any(term in structured.intent.casefold() for term in rubric["intent_terms"])
    )
    actual_risks = {getattr(x, "value", str(x)) for x in structured.risk_hints}
    risk_ok = (
        True
        if expected_clarification
        else bool(actual_risks & rubric["risk"])
    )
    clarification_ok = structured.needs_clarification == expected_clarification
    if expected_clarification:
        clarification_ok = clarification_ok and bool(
            structured.missing_inputs or structured.ambiguities
        )

    entities: dict[str, list[Any]] = {}
    for entity in structured.entities:
        entities.setdefault(entity.name.casefold().strip(), []).append(entity)
    entity_checks: dict[str, bool] = {}
    for name, expected_substring in (case.get("expected_entities") or {}).items():
        entity_checks[name] = any(
            getattr(entity.source, "value", str(entity.source)) == "USER_EXPLICIT"
            and expected_substring.casefold() in str(entity.value).casefold()
            for entity in entities.get(name, [])
        )
    checks = {
        "intent": intent_ok,
        "capability": capability_ok,
        "risk": risk_ok,
        "clarification": clarification_ok,
        "entities": all(entity_checks.values()),
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "entity_checks": entity_checks,
        "observed": {
            "intent": structured.intent,
            "required_capabilities": list(structured.required_capabilities),
            "risk_hints": sorted(actual_risks),
            "entity_names": sorted(entities),
            "needs_clarification": structured.needs_clarification,
            "missing_inputs_count": len(structured.missing_inputs),
            "ambiguities_count": len(structured.ambiguities),
        },
    }


def judge_l3_02(
    results: list[dict[str, Any]], *, expected_count: int, target_min_percent: float
) -> dict[str, Any]:
    if expected_count < 1 or len(results) != expected_count:
        return {
            "judgment": "INCOMPLETE",
            "reason": "EVIDENCE_COUNT_MISMATCH",
            "required_cases": expected_count,
            "observed_cases": len(results),
            "success_count": None,
            "success_rate_percent": None,
        }
    success = sum(x.get("passed") is True for x in results)
    rate = round(success / expected_count * 100.0, 4)
    return {
        "judgment": "MET" if rate >= target_min_percent else "NOT_MET",
        "reason": None,
        "required_cases": expected_count,
        "observed_cases": len(results),
        "success_count": success,
        "failed_cases": expected_count - success,
        "success_rate_percent": rate,
    }
