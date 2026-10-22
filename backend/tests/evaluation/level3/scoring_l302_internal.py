"""L3-02 internal self-assessment scorer (single actual reviewer; offline).

This is NOT the approved independent-review v2 scorer. No human review is
performed by code, and metadata alone cannot prove a real review took place.
Never claim external certification or silently substitute this for v1/v2.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any

DATASET_VERSION = "level3-natural-language-analysis.internal.v1"
CRITERIA = frozenset({
    "intent", "capability", "entities", "provenance",
    "temporal", "risk", "clarification", "schema",
})
FAMILY_COUNTS = {
    "lookup": 6, "create": 4, "update": 4,
    "verbatim": 4, "ambiguous": 2,
}
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _nonblank(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def case_set_sha256(cases: list[dict[str, Any]]) -> str:
    """Pin owner approval to exact gold cases, independent of YAML formatting."""
    canonical = json.dumps(
        cases, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def gold_gate(gold: dict[str, Any]) -> dict[str, Any]:
    """Fail before any measurement while actual owner gold review is absent."""
    if not isinstance(gold, dict) or gold.get("dataset_version") != DATASET_VERSION:
        return {"ok": False, "reason": "INTERNAL_DATASET_VERSION_MISMATCH"}
    if gold.get("method_review_status") != "APPROVED_INTERNAL_METHOD_ONLY":
        return {"ok": False, "reason": "INTERNAL_METHOD_NOT_APPROVED"}
    if gold.get("review_status") != "APPROVED":
        return {"ok": False, "reason": "INTERNAL_GOLD_NOT_APPROVED"}
    owner = gold.get("reviewed_by")
    if not _nonblank(owner) or not _nonblank(gold.get("reviewed_at")):
        return {"ok": False, "reason": "INTERNAL_GOLD_APPROVAL_RECORD_MISSING"}
    cases = gold.get("cases")
    if not isinstance(cases, list) or len(cases) != 20:
        return {"ok": False, "reason": "INTERNAL_GOLD_CASE_COUNT_INVALID"}
    if any(
        not isinstance(c, dict)
        or not _nonblank(c.get("id"))
        or not _nonblank(c.get("request_text"))
        or not isinstance(c.get("family"), str)
        for c in cases
    ):
        return {"ok": False, "reason": "INTERNAL_GOLD_CASE_INVALID"}
    ids = [c["id"] for c in cases]
    if len(set(ids)) != 20 or Counter(c["family"] for c in cases) != FAMILY_COUNTS:
        return {"ok": False, "reason": "INTERNAL_GOLD_CASE_DISTRIBUTION_INVALID"}

    review = gold.get("owner_review")
    if not isinstance(review, dict) or review.get("reviewer_ref") != owner:
        return {"ok": False, "reason": "INTERNAL_OWNER_REVIEW_MISSING"}
    # The review must attest to these exact 20 request texts and gold values.
    if review.get("case_set_sha256") != case_set_sha256(cases):
        return {"ok": False, "reason": "INTERNAL_OWNER_REVIEW_CASE_HASH_MISMATCH"}
    if not _nonblank(review.get("reviewed_at")) or not _nonblank(
        review.get("evidence_ref")
    ):
        return {"ok": False, "reason": "INTERNAL_OWNER_REVIEW_AUDIT_MISSING"}
    decisions = review.get("case_decisions")
    if not isinstance(decisions, list) or len(decisions) != 20:
        return {"ok": False, "reason": "INTERNAL_OWNER_CASE_REVIEWS_MISSING"}
    if any(
        not isinstance(row, dict)
        or not _nonblank(row.get("case_id"))
        or row.get("decision") != "APPROVED"
        or not _nonblank(row.get("rationale"))
        for row in decisions
    ):
        return {"ok": False, "reason": "INTERNAL_OWNER_CASE_REVIEW_INVALID"}
    if {r["case_id"] for r in decisions} != set(ids) or len({
        r["case_id"] for r in decisions
    }) != 20:
        return {"ok": False, "reason": "INTERNAL_OWNER_CASE_REVIEW_MISMATCH"}
    return {"ok": True, "reason": None, "case_ids": tuple(ids)}


def score_reviewed_dataset(
    gold: dict[str, Any],
    *,
    gold_sha256: str,
    evidence: dict[str, Any],
    target_min_percent: float = 85.0,
) -> dict[str, Any]:
    """Count 20 human-reviewed semantic outcomes; never auto-judge meaning."""
    gate = gold_gate(gold)
    if not gate["ok"]:
        return {"ok": False, "judgment": "INCOMPLETE", "reason": gate["reason"]}
    if type(target_min_percent) not in {float, int} or target_min_percent != 85:
        return {"ok": False, "judgment": "INCOMPLETE", "reason": "TARGET_MISMATCH"}
    if not isinstance(gold_sha256, str) or not _SHA256_RE.fullmatch(gold_sha256):
        return {"ok": False, "judgment": "INCOMPLETE", "reason": "GOLD_HASH_INVALID"}
    if not isinstance(evidence, dict) or evidence.get("gold_sha256") != gold_sha256:
        return {"ok": False, "judgment": "INCOMPLETE", "reason": "GOLD_HASH_MISMATCH"}
    run_ref = evidence.get("run_ref")
    rows = evidence.get("cases")
    if not _nonblank(run_ref) or not isinstance(rows, list) or len(rows) != 20:
        return {"ok": False, "judgment": "INCOMPLETE", "reason": "EVIDENCE_COUNT_MISMATCH"}

    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            return _incomplete("EVIDENCE_CASE_INVALID")
        case_id = row.get("case_id")
        if (
            not isinstance(case_id, str)
            or case_id not in gate["case_ids"]
            or case_id in seen
            or row.get("run_ref") != run_ref
        ):
            return _incomplete("EVIDENCE_CASE_ID_OR_RUN_MISMATCH")
        seen.add(case_id)
        digest = row.get("observed_sha256")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            return _incomplete("OBSERVATION_HASH_INVALID")
        schema_valid = row.get("schema_valid")
        if type(schema_valid) is not bool:
            return _incomplete("SCHEMA_EVIDENCE_MISSING")
        reviewer = row.get("review")
        if not isinstance(reviewer, dict) or any(
            not _nonblank(reviewer.get(key))
            for key in ("reviewer_ref", "reviewed_at", "evidence_ref", "rationale")
        ):
            return _incomplete("HUMAN_REVIEW_RECORD_MISSING")
        checks = reviewer.get("checks")
        if (
            not isinstance(checks, dict)
            or set(checks) != CRITERIA
            or any(type(v) is not bool for v in checks.values())
        ):
            return _incomplete("EIGHT_CRITERIA_REVIEW_INVALID")
        if checks["schema"] is not schema_valid:
            return _incomplete("SCHEMA_REVIEW_MISMATCH")
        final = {key: checks[key] for key in sorted(CRITERIA)}
        results.append({
            "case_id": case_id,
            "observed_sha256": digest,
            "checks": final,
            "failed_criteria": sorted(k for k in CRITERIA if not final[k]),
            "passed": all(final.values()),
            "reviewer_ref": reviewer["reviewer_ref"],
        })
    if seen != set(gate["case_ids"]):
        return _incomplete("EVIDENCE_CASES_INCOMPLETE")
    count = sum(x["passed"] for x in results)
    rate = round(100 * count / 20, 4)
    return {
        "ok": True,
        "judgment": "INTERNAL_MET" if rate >= 85 else "INTERNAL_NOT_MET",
        "reason": None,
        "dataset_version": DATASET_VERSION,
        "gold_sha256": gold_sha256,
        "run_ref": run_ref,
        "required_cases": 20,
        "observed_cases": 20,
        "success_count": count,
        "failed_cases": 20 - count,
        "success_rate_percent": rate,
        "target_min_percent": 85.0,
        "case_results": results,
        "method": "human_single_reviewer_internal_self_assessment",
    }


def _incomplete(reason: str) -> dict[str, Any]:
    return {"ok": False, "judgment": "INCOMPLETE", "reason": reason}
