"""L3-02 v2 semantic adjudication gate (offline; no LLM or Tool calls).

Semantic equivalence is a human reviewer judgment, NOT a frozen wordlist or
a claim that reviewer metadata proves independent review. The source evidence
must be checked by real reviewers before this module can support a report.
v1 scorer and its approved 20 cases are never modified by this module.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

DATASET_VERSION = "level3-natural-language-analysis.v2"
CRITERIA = frozenset({
    "intent", "capability", "entities", "provenance",
    "temporal", "risk", "clarification", "schema",
})
FAMILY_COUNTS = {
    "lookup": 6,
    "create": 4,
    "update": 4,
    "verbatim": 4,
    "ambiguous": 2,
}
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _nonblank(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def gold_gate(gold: dict[str, Any]) -> dict[str, Any]:
    """Return explicit readiness, never turn an unsigned gold into approval."""
    if gold.get("dataset_version") != DATASET_VERSION:
        return {"ok": False, "reason": "DATASET_VERSION_MISMATCH"}
    if gold.get("method_review_status") != "APPROVED":
        return {"ok": False, "reason": "METHODOLOGY_NOT_APPROVED"}
    if gold.get("review_status") != "APPROVED":
        return {"ok": False, "reason": "GOLD_SET_NOT_APPROVED"}
    if not _nonblank(gold.get("reviewed_by")) or not _nonblank(gold.get("reviewed_at")):
        return {"ok": False, "reason": "GOLD_APPROVAL_RECORD_MISSING"}

    records = gold.get("independent_reviews")
    if not isinstance(records, list) or len(records) != 2:
        return {"ok": False, "reason": "INDEPENDENT_GOLD_REVIEWS_MISSING"}
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            return {"ok": False, "reason": "INDEPENDENT_GOLD_REVIEW_INVALID"}
        reviewer = record.get("reviewer_ref")
        if (
            not _nonblank(reviewer)
            or reviewer in seen
            or record.get("decision") != "APPROVED"
            or not _nonblank(record.get("reviewed_at"))
            or not _nonblank(record.get("evidence_ref"))
        ):
            return {"ok": False, "reason": "INDEPENDENT_GOLD_REVIEW_INVALID"}
        seen.add(reviewer)

    cases = gold.get("cases")
    if not isinstance(cases, list) or len(cases) != 20:
        return {"ok": False, "reason": "GOLD_CASE_COUNT_INVALID"}
    if any(not isinstance(c, dict) or not _nonblank(c.get("id")) for c in cases):
        return {"ok": False, "reason": "GOLD_CASE_INVALID"}
    case_ids = [c["id"] for c in cases]
    if len(set(case_ids)) != 20 or Counter(c.get("family") for c in cases) != FAMILY_COUNTS:
        return {"ok": False, "reason": "GOLD_CASE_DISTRIBUTION_INVALID"}
    return {"ok": True, "reason": None, "case_ids": tuple(case_ids)}


def _checks(raw: Any) -> dict[str, bool]:
    if (
        not isinstance(raw, dict)
        or set(raw) != CRITERIA
        or any(type(value) is not bool for value in raw.values())
    ):
        raise ValueError("INVALID_EIGHT_CRITERIA_REVIEW")
    return {k: raw[k] for k in sorted(CRITERIA)}


def _review(raw: Any) -> tuple[str, dict[str, bool]]:
    if not isinstance(raw, dict):
        raise ValueError("REVIEW_RECORD_INVALID")
    reviewer = raw.get("reviewer_ref")
    if (
        not _nonblank(reviewer)
        or not _nonblank(raw.get("evidence_ref"))
        or not _nonblank(raw.get("reviewed_at"))
        or not _nonblank(raw.get("rationale"))
    ):
        raise ValueError("REVIEW_AUDIT_FIELDS_MISSING")
    return reviewer, _checks(raw.get("checks"))


def adjudicate_case(case: dict[str, Any]) -> dict[str, Any]:
    """Two independent semantic ratings; a third adjudicates any disagreement.

    The third reviewer may decide only disputed checks. Agreed checks cannot
    be silently overturned. No automatic alias/keyword matching is used.
    """
    cid = case.get("case_id")
    if not _nonblank(cid):
        raise ValueError("CASE_ID_INVALID")
    digest = case.get("observed_sha256")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise ValueError("OBSERVED_EVIDENCE_HASH_INVALID")
    schema_valid = case.get("schema_valid")
    if type(schema_valid) is not bool:
        raise ValueError("SCHEMA_EVIDENCE_MISSING")
    reviews = case.get("reviews")
    if not isinstance(reviews, list) or len(reviews) != 2:
        raise ValueError("TWO_INDEPENDENT_OUTPUT_REVIEWS_REQUIRED")
    reviewer_a, checks_a = _review(reviews[0])
    reviewer_b, checks_b = _review(reviews[1])
    if reviewer_a == reviewer_b:
        raise ValueError("REVIEWERS_NOT_DISTINCT")
    if (checks_a["schema"] is not schema_valid) or (
        checks_b["schema"] is not schema_valid
    ):
        raise ValueError("SCHEMA_REVIEW_DISAGREES_WITH_VALIDATION")

    disagreed = sorted(k for k in CRITERIA if checks_a[k] != checks_b[k])
    adjudication = case.get("adjudication")
    final = dict(checks_a)
    if disagreed:
        if not isinstance(adjudication, dict):
            raise ValueError("ADJUDICATION_REQUIRED")
        adjudicator, resolution = _review(adjudication)
        if adjudicator in {reviewer_a, reviewer_b}:
            raise ValueError("ADJUDICATOR_NOT_INDEPENDENT")
        for key in sorted(CRITERIA):
            if key in disagreed:
                final[key] = resolution[key]
            elif checks_a[key] != resolution[key]:
                raise ValueError("ADJUDICATION_CHANGED_AGREED_CHECK")
        if final["schema"] is not schema_valid:
            raise ValueError("ADJUDICATION_CHANGED_SCHEMA_EVIDENCE")
    elif adjudication is not None:
        raise ValueError("UNNECESSARY_ADJUDICATION")

    return {
        "case_id": cid,
        "observed_sha256": digest,
        "checks": final,
        "failed_criteria": sorted(k for k in CRITERIA if not final[k]),
        "adjudicated_criteria": disagreed,
        "passed": all(final.values()),
        "reviewer_refs": [reviewer_a, reviewer_b],
    }


def score_reviewed_dataset(
    gold: dict[str, Any],
    *,
    gold_sha256: str,
    evidence: dict[str, Any],
    target_min_percent: float = 85.0,
) -> dict[str, Any]:
    """Fail closed on any missing approval, mismatched evidence or review."""
    gate = gold_gate(gold)
    if not gate["ok"]:
        return {"judgment": "INCOMPLETE", "reason": gate["reason"], "ok": False}
    if not isinstance(gold_sha256, str) or not _SHA256_RE.fullmatch(gold_sha256):
        return {"judgment": "INCOMPLETE", "reason": "GOLD_HASH_INVALID", "ok": False}
    if not isinstance(evidence, dict) or evidence.get("gold_sha256") != gold_sha256:
        return {"judgment": "INCOMPLETE", "reason": "GOLD_HASH_MISMATCH", "ok": False}
    run_ref = evidence.get("run_ref")
    cases = evidence.get("cases")
    if not _nonblank(run_ref) or not isinstance(cases, list) or len(cases) != 20:
        return {"judgment": "INCOMPLETE", "reason": "EVIDENCE_COUNT_MISMATCH", "ok": False}
    seen: set[str] = set()
    scores: list[dict[str, Any]] = []
    try:
        for case in cases:
            if not isinstance(case, dict):
                raise ValueError("EVIDENCE_CASE_INVALID")
            case_id = case.get("case_id")
            if case_id not in gate["case_ids"] or case_id in seen:
                raise ValueError("EVIDENCE_CASE_ID_MISMATCH")
            if case.get("run_ref") != run_ref:
                raise ValueError("EVIDENCE_RUN_MISMATCH")
            seen.add(case_id)
            scores.append(adjudicate_case(case))
        if seen != set(gate["case_ids"]):
            raise ValueError("EVIDENCE_CASES_INCOMPLETE")
    except ValueError as exc:
        # Only fixed error codes are allowed; never echo inputs/model text.
        return {"judgment": "INCOMPLETE", "reason": str(exc), "ok": False}

    success = sum(row["passed"] for row in scores)
    percent = round(100 * success / 20, 4)
    return {
        "judgment": "MET" if percent >= target_min_percent else "NOT_MET",
        "reason": None,
        "ok": True,
        "required_cases": 20,
        "observed_cases": 20,
        "success_count": success,
        "failed_cases": 20 - success,
        "success_rate_percent": percent,
        "target_min_percent": target_min_percent,
        "dataset_version": DATASET_VERSION,
        "gold_sha256": gold_sha256,
        "run_ref": run_ref,
        "case_results": scores,
        "method": "two_independent_semantic_reviews_plus_disagreement_adjudication",
    }
