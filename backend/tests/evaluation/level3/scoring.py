"""Pure scoring helpers for Level 3 L3-05 (no I/O, no secrets)."""

from __future__ import annotations

from typing import Any

JUDGMENT_MET = "MET"
JUDGMENT_NOT_MET = "NOT_MET"
JUDGMENT_INCOMPLETE = "INCOMPLETE"


def elapsed_ms_from_ns(elapsed_ns: int) -> float:
    return elapsed_ns / 1_000_000.0


def required_official_call_count(
    *,
    query_count: int,
    official_repeats_per_query: int,
) -> int:
    return query_count * official_repeats_per_query


def judge_l3_05(
    *,
    official_samples: list[dict[str, Any]],
    required_calls: int,
    target_max_seconds: float = 3.0,
) -> dict[str, Any]:
    """Compute average seconds and MET / NOT_MET / INCOMPLETE.

    Warm-up samples must not be passed here. Any unsuccessful required sample
    yields INCOMPLETE — never average successes alone.
    """

    if required_calls < 1:
        return {
            "judgment": JUDGMENT_INCOMPLETE,
            "reason": "REQUIRED_CALLS_INVALID",
            "required_calls": required_calls,
            "completed_calls": 0,
            "failed_calls": 0,
            "average_seconds": None,
            "target_max_seconds": target_max_seconds,
        }

    if len(official_samples) != required_calls:
        return {
            "judgment": JUDGMENT_INCOMPLETE,
            "reason": "EVIDENCE_COUNT_MISMATCH",
            "required_calls": required_calls,
            "completed_calls": sum(
                1 for s in official_samples if s.get("ok") is True
            ),
            "failed_calls": sum(
                1 for s in official_samples if s.get("ok") is not True
            ),
            "recorded_samples": len(official_samples),
            "average_seconds": None,
            "target_max_seconds": target_max_seconds,
        }

    failed = [s for s in official_samples if s.get("ok") is not True]
    if failed:
        return {
            "judgment": JUDGMENT_INCOMPLETE,
            "reason": "REQUIRED_CALL_FAILED",
            "required_calls": required_calls,
            "completed_calls": required_calls - len(failed),
            "failed_calls": len(failed),
            "average_seconds": None,
            "target_max_seconds": target_max_seconds,
        }

    elapsed_ms = [float(s["elapsed_ms"]) for s in official_samples]
    average_seconds = (sum(elapsed_ms) / len(elapsed_ms)) / 1000.0
    met = average_seconds <= float(target_max_seconds)
    return {
        "judgment": JUDGMENT_MET if met else JUDGMENT_NOT_MET,
        "reason": None,
        "required_calls": required_calls,
        "completed_calls": required_calls,
        "failed_calls": 0,
        "average_seconds": average_seconds,
        "average_ms": average_seconds * 1000.0,
        "target_max_seconds": target_max_seconds,
        "min_elapsed_ms": min(elapsed_ms),
        "max_elapsed_ms": max(elapsed_ms),
    }
