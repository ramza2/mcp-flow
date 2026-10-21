"""Frozen rubric for approved L3-02 natural-language analysis (not Tool mapping)."""

from __future__ import annotations

import re
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


# Only explicit temporal entities count. Repeating the user's message in the
# intent/constraints is NOT sufficient evidence of temporal extraction.
_TEMPORAL_NAMES = frozenset({
    "when", "date", "time", "datetime", "date_time", "schedule",
    "scheduled_at", "start", "start_at", "start_date", "start_time",
    "start_datetime", "start_date_time", "end", "end_at", "end_date",
    "end_time", "end_datetime", "period", "date_range", "time_range",
    "range", "from", "to",
})
_DATE_RE = re.compile(
    r"(?<!\d)(20\d{2})\s*(?:년|[-./])\s*(\d{1,2})"
    r"\s*(?:월|[-./])\s*(\d{1,2})(?:일)?(?!\d)"
)
_MONTH_RE = re.compile(
    r"(?<!\d)(20\d{2})\s*(?:년\s*(\d{1,2})월(?!\s*\d{1,2}\s*일)"
    r"|[-./]\s*(\d{1,2})(?!\d|[-./]\d))"
)
# Ignore explicit timezone offsets (e.g. +09:00), which are not local clock times.
_HHMM_RE = re.compile(r"(?<![+\d-])([01]?\d|2[0-3]):([0-5]\d)(?!\d)")
_KO_CLOCK_RE = re.compile(r"(오전|오후)?\s*(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분)?")


def _time_entities(structured: StructuredRequestV1) -> str:
    return " ".join(
        str(entity.value) for entity in structured.entities
        if entity.name.casefold().strip() in _TEMPORAL_NAMES
        and getattr(entity.source, "value", str(entity.source)) == "USER_EXPLICIT"
    )


def _observed_dates(text: str) -> set[str]:
    return {
        f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        for y, m, d in _DATE_RE.findall(text)
        if 1 <= int(m) <= 12 and 1 <= int(d) <= 31
    }


def _observed_months(text: str) -> set[str]:
    return {
        f"{int(y):04d}-{int(m):02d}"
        for y, month_ko, month_iso in _MONTH_RE.findall(text)
        for m in [month_ko or month_iso]
        if 1 <= int(m) <= 12
    }


def _observed_times(text: str) -> set[str]:
    clocks = {
        f"{int(h):02d}:{int(m):02d}"
        for h, m in _HHMM_RE.findall(text)
    }
    for am_pm, h, m in _KO_CLOCK_RE.findall(text):
        hour = int(h)
        minute = int(m) if m else 0
        if minute > 59 or hour > 23:
            continue
        if am_pm == "오전":
            if hour > 12:
                continue
            hour = hour % 12
        elif am_pm == "오후":
            if hour > 12:
                continue
            hour = hour % 12 + 12
        clocks.add(f"{hour:02d}:{minute:02d}")
    return clocks


def _temporal_checks(case: dict[str, Any], structured: StructuredRequestV1) -> dict[str, bool]:
    expected = case.get("expected_temporal") or {}
    if not expected:
        return {}
    extracted = _time_entities(structured)
    dates = _observed_dates(extracted)
    months = _observed_months(extracted)
    times = _observed_times(extracted)
    result = {
        f"date:{date}": date in dates
        for date in expected.get("dates", [])
    }
    result.update({
        f"month:{month}": month in months
        for month in expected.get("months", [])
    })
    if "time_24h" in expected:
        result["time_24h"] = expected["time_24h"] in times
    return result


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
    temporal_checks = _temporal_checks(case, structured)
    checks = {
        "temporal": all(temporal_checks.values()),
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
        "temporal_checks": temporal_checks,
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
