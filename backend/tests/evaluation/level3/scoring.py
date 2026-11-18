"""Pure scoring helpers for Level 3 L3-05 / L3-06 (no I/O, no secrets)."""

from __future__ import annotations

import re
from typing import Any

JUDGMENT_MET = "MET"
JUDGMENT_NOT_MET = "NOT_MET"
JUDGMENT_INCOMPLETE = "INCOMPLETE"

_ALIAS_SPLIT = re.compile(r"[\s_\-./]+")


def elapsed_ms_from_ns(elapsed_ns: int) -> float:
    return elapsed_ns / 1_000_000.0


def required_official_call_count(
    *,
    query_count: int,
    official_repeats_per_query: int,
) -> int:
    return query_count * official_repeats_per_query


def normalize_tool_alias(value: str | None) -> str | None:
    """Normalize remote/display names for duplicate-suspicion grouping only."""

    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    parts = [p for p in _ALIAS_SPLIT.split(text) if p]
    if not parts:
        return None
    return "_".join(parts)


def find_duplicate_suspect_groups(
    tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build alias/clone suspicion groups. Never auto-excludes tools.

    A group is emitted when ≥2 distinct tool_ids share the same normalized
    ``remote_name`` or the same non-empty normalized ``display_name``.
    """

    by_remote: dict[str, list[dict[str, Any]]] = {}
    by_display: dict[str, list[dict[str, Any]]] = {}
    for tool in tools:
        tool_id = str(tool.get("tool_id") or "").strip()
        if not tool_id:
            continue
        remote_key = normalize_tool_alias(
            tool.get("remote_name") if isinstance(tool.get("remote_name"), str) else None
        )
        display_key = normalize_tool_alias(
            tool.get("display_name")
            if isinstance(tool.get("display_name"), str)
            else None
        )
        entry = {
            "tool_id": tool_id,
            "server_id": str(tool.get("server_id") or "") or None,
            "remote_name": tool.get("remote_name"),
            "display_name": tool.get("display_name"),
            "tool_version_id": str(tool.get("tool_version_id") or "") or None,
            "verification_id": str(tool.get("verification_id") or "") or None,
        }
        if remote_key is not None:
            by_remote.setdefault(remote_key, []).append(entry)
        if display_key is not None:
            by_display.setdefault(display_key, []).append(entry)

    groups: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()

    def _emit(kind: str, key: str, members: list[dict[str, Any]]) -> None:
        unique_ids = sorted({m["tool_id"] for m in members})
        if len(unique_ids) < 2:
            return
        marker = (kind, key)
        if marker in seen_keys:
            return
        seen_keys.add(marker)
        # Deduplicate member rows by tool_id while preserving first-seen order.
        by_id: dict[str, dict[str, Any]] = {}
        for member in members:
            by_id.setdefault(member["tool_id"], member)
        groups.append(
            {
                "kind": kind,
                "normalized_key": key,
                "tool_ids": unique_ids,
                "member_count": len(unique_ids),
                "members": [by_id[tid] for tid in unique_ids],
            }
        )

    for key, members in sorted(by_remote.items()):
        _emit("remote_name", key, members)
    for key, members in sorted(by_display.items()):
        _emit("display_name", key, members)
    return groups


def unique_count_after_collapsing_suspects(
    *,
    tool_ids: list[str],
    suspect_groups: list[dict[str, Any]],
) -> int:
    """Lower-bound unique count if every suspect group collapses to one tool."""

    ids = [str(tid) for tid in tool_ids if str(tid).strip()]
    if not ids:
        return 0
    parent: dict[str, str] = {tid: tid for tid in ids}

    def _find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: str, b: str) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[rb] = ra

    for group in suspect_groups:
        members = [str(tid) for tid in (group.get("tool_ids") or []) if str(tid) in parent]
        if len(members) < 2:
            continue
        head = members[0]
        for other in members[1:]:
            _union(head, other)
    return len({_find(tid) for tid in ids})


def judge_l3_06(
    *,
    counted: int,
    target_min_count: int,
    suspect_groups: list[dict[str, Any]],
    unique_lower_bound: int | None = None,
) -> dict[str, Any]:
    """Judge L3-06 from DISTINCT effective VERIFIED count + duplicate suspects.

    Unresolved alias/clone suspicions never auto-exclude tools. When collapsing
    those suspicions would change MET vs NOT_MET, judgment is INCOMPLETE.
    """

    if not isinstance(counted, int) or counted < 0:
        return {
            "judgment": JUDGMENT_INCOMPLETE,
            "reason": "COUNT_INVALID",
            "counted": counted,
            "target_min_count": target_min_count,
            "gap_to_target_min": None,
            "unique_lower_bound": unique_lower_bound,
            "suspect_group_count": len(suspect_groups),
        }
    if not isinstance(target_min_count, int) or target_min_count < 1:
        return {
            "judgment": JUDGMENT_INCOMPLETE,
            "reason": "TARGET_MIN_INVALID",
            "counted": counted,
            "target_min_count": target_min_count,
            "gap_to_target_min": None,
            "unique_lower_bound": unique_lower_bound,
            "suspect_group_count": len(suspect_groups),
        }

    gap = max(target_min_count - counted, 0)
    lower = (
        unique_lower_bound
        if isinstance(unique_lower_bound, int)
        else counted
    )
    if lower < 0 or lower > counted:
        return {
            "judgment": JUDGMENT_INCOMPLETE,
            "reason": "LOWER_BOUND_INVALID",
            "counted": counted,
            "target_min_count": target_min_count,
            "gap_to_target_min": gap,
            "unique_lower_bound": lower,
            "suspect_group_count": len(suspect_groups),
        }

    raw_met = counted >= target_min_count
    lower_met = lower >= target_min_count
    if suspect_groups and raw_met != lower_met:
        return {
            "judgment": JUDGMENT_INCOMPLETE,
            "reason": "DUPLICATE_SUSPECTS_AFFECT_JUDGMENT",
            "counted": counted,
            "target_min_count": target_min_count,
            "gap_to_target_min": gap,
            "unique_lower_bound": lower,
            "suspect_group_count": len(suspect_groups),
        }

    return {
        "judgment": JUDGMENT_MET if raw_met else JUDGMENT_NOT_MET,
        "reason": None,
        "counted": counted,
        "target_min_count": target_min_count,
        "gap_to_target_min": gap,
        "unique_lower_bound": lower,
        "suspect_group_count": len(suspect_groups),
    }


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
