"""Offline L3-02 v2 scoring tests: synthetic approvals ONLY, no live model calls."""

from copy import deepcopy
from pathlib import Path

import yaml

from tests.evaluation.level3.scoring_l302_v2 import (
    CRITERIA,
    adjudicate_case,
    gold_gate,
    score_reviewed_dataset,
)

GOLD_PATH = Path(__file__).resolve().parent / "level3" / "dataset_l302.v2.yaml"
DUMMY_DIGEST = "a" * 64
DUMMY_TIME = "2026-10-21T09:00:00+09:00"


def _gold(*, approved: bool = True) -> dict:
    data = yaml.safe_load(GOLD_PATH.read_text(encoding="utf-8"))
    if approved:
        # Synthetic test records do NOT constitute approval of the real dataset.
        data["review_status"] = "APPROVED"
        data["reviewed_by"] = "TEST_FIXTURE_ONLY"
        data["reviewed_at"] = "2026-10-21"
        data["independent_reviews"] = [
            {
                "reviewer_ref": f"test-gold-{i}",
                "decision": "APPROVED",
                "reviewed_at": DUMMY_TIME,
                "evidence_ref": f"fixture-{i}",
            }
            for i in (1, 2)
        ]
    return data


def _review(reviewer: str, checks: dict[str, bool]) -> dict:
    return {
        "reviewer_ref": reviewer,
        "reviewed_at": DUMMY_TIME,
        "evidence_ref": f"fixture-{reviewer}",
        "rationale": "Synthetic example evidence for scorer tests only.",
        "checks": checks,
    }


def _evidence(gold: dict, *, failed: set[str] | None = None) -> dict:
    rows = []
    for case in gold["cases"]:
        checks = dict.fromkeys(CRITERIA, True)
        if case["id"] in (failed or set()):
            checks["intent"] = False
        rows.append({
            "case_id": case["id"],
            "run_ref": "synthetic-offline-test",
            "observed_sha256": DUMMY_DIGEST,
            "schema_valid": True,
            "reviews": [_review("test-A", checks), _review("test-B", checks)],
        })
    return {
        "run_ref": "synthetic-offline-test",
        "gold_sha256": DUMMY_DIGEST,
        "cases": rows,
    }


def test_draft_gold_remains_unapproved_and_cannot_be_scored() -> None:
    draft = _gold(approved=False)
    assert draft["review_status"] == "PENDING_REVIEW"
    assert gold_gate(draft)["reason"] == "GOLD_SET_NOT_APPROVED"
    result = score_reviewed_dataset(
        draft, gold_sha256=DUMMY_DIGEST, evidence=_evidence(draft)
    )
    assert result == {
        "judgment": "INCOMPLETE",
        "reason": "GOLD_SET_NOT_APPROVED",
        "ok": False,
    }


def test_synthetic_two_gold_reviewers_are_required() -> None:
    gold = _gold()
    assert gold_gate(gold)["ok"] is True
    gold["independent_reviews"][1]["reviewer_ref"] = "test-gold-1"
    assert gold_gate(gold)["reason"] == "INDEPENDENT_GOLD_REVIEW_INVALID"
    gold = _gold()
    gold["independent_reviews"] = []
    assert gold_gate(gold)["reason"] == "INDEPENDENT_GOLD_REVIEWS_MISSING"


def test_synthetic_twenty_outcome_reviews_and_target() -> None:
    gold = _gold()
    score = score_reviewed_dataset(
        gold, gold_sha256=DUMMY_DIGEST, evidence=_evidence(gold)
    )
    assert score["judgment"] == "MET"
    assert score["success_count"] == 20
    assert all(set(case["checks"]) == CRITERIA for case in score["case_results"])
    # 17/20 meets the unchanged 85% target. Fixture numbers are NOT live evidence.
    fails = {row["id"] for row in gold["cases"][:3]}
    score = score_reviewed_dataset(
        gold, gold_sha256=DUMMY_DIGEST, evidence=_evidence(gold, failed=fails)
    )
    assert score["success_count"] == 17
    assert score["success_rate_percent"] == 85.0
    assert score["judgment"] == "MET"
    fails = {row["id"] for row in gold["cases"][:4]}
    score = score_reviewed_dataset(
        gold, gold_sha256=DUMMY_DIGEST, evidence=_evidence(gold, failed=fails)
    )
    assert score["success_count"] == 16
    assert score["judgment"] == "NOT_MET"


def test_approval_threshold_cannot_be_weakened() -> None:
    gold = _gold()
    score = score_reviewed_dataset(
        gold, gold_sha256=DUMMY_DIGEST, evidence=_evidence(gold),
        target_min_percent=0.0,
    )
    assert score["judgment"] == "INCOMPLETE"
    assert score["reason"] == "APPROVED_TARGET_MISMATCH"


def test_incomplete_review_set_and_hash_mismatch_fail_closed() -> None:
    gold = _gold()
    base = _evidence(gold)
    wrong_hash = deepcopy(base)
    wrong_hash["gold_sha256"] = "b" * 64
    score = score_reviewed_dataset(gold, gold_sha256=DUMMY_DIGEST, evidence=wrong_hash)
    assert score["judgment"] == "INCOMPLETE"
    assert score["reason"] == "GOLD_HASH_MISMATCH"

    partial = deepcopy(base)
    partial["cases"].pop()
    score = score_reviewed_dataset(gold, gold_sha256=DUMMY_DIGEST, evidence=partial)
    assert score["reason"] == "EVIDENCE_COUNT_MISMATCH"

    duplicate = deepcopy(base)
    duplicate["cases"][1]["case_id"] = duplicate["cases"][0]["case_id"]
    score = score_reviewed_dataset(gold, gold_sha256=DUMMY_DIGEST, evidence=duplicate)
    assert score["reason"] == "EVIDENCE_CASE_ID_MISMATCH"

    mixed_run = deepcopy(base)
    mixed_run["cases"][1]["run_ref"] = "other-run"
    score = score_reviewed_dataset(gold, gold_sha256=DUMMY_DIGEST, evidence=mixed_run)
    assert score["reason"] == "EVIDENCE_RUN_MISMATCH"


def test_disagreement_requires_an_independent_third_reviewer() -> None:
    gold = _gold()
    evidence = _evidence(gold)
    item = evidence["cases"][0]
    item["reviews"][1]["checks"]["intent"] = False

    score = score_reviewed_dataset(gold, gold_sha256=DUMMY_DIGEST, evidence=evidence)
    assert score["reason"] == "ADJUDICATION_REQUIRED"
    item["adjudication"] = _review("test-A", dict.fromkeys(CRITERIA, True))
    score = score_reviewed_dataset(gold, gold_sha256=DUMMY_DIGEST, evidence=evidence)
    assert score["reason"] == "ADJUDICATOR_NOT_INDEPENDENT"

    item["adjudication"] = _review("test-C", dict.fromkeys(CRITERIA, True))
    outcome = adjudicate_case(item)
    assert outcome["passed"] is True
    assert outcome["adjudicated_criteria"] == ["intent"]

    item["adjudication"]["checks"]["risk"] = False
    score = score_reviewed_dataset(gold, gold_sha256=DUMMY_DIGEST, evidence=evidence)
    assert score["reason"] == "ADJUDICATION_CHANGED_AGREED_CHECK"


def test_invalid_boolean_and_schema_check_are_rejected() -> None:
    gold = _gold()
    evidence = _evidence(gold)
    item = evidence["cases"][0]
    item["reviews"][0]["checks"]["intent"] = 1  # 1 is not an actual boolean.
    score = score_reviewed_dataset(gold, gold_sha256=DUMMY_DIGEST, evidence=evidence)
    assert score["reason"] == "INVALID_EIGHT_CRITERIA_REVIEW"

    evidence = _evidence(gold)
    item = evidence["cases"][0]
    item["schema_valid"] = False
    score = score_reviewed_dataset(gold, gold_sha256=DUMMY_DIGEST, evidence=evidence)
    assert score["reason"] == "SCHEMA_REVIEW_DISAGREES_WITH_VALIDATION"

    # A real ValidationError must be marked failed, not filtered out.
    item["reviews"][0]["checks"]["schema"] = False
    item["reviews"][1]["checks"]["schema"] = False
    score = score_reviewed_dataset(gold, gold_sha256=DUMMY_DIGEST, evidence=evidence)
    assert score["success_count"] == 19
    assert score["case_results"][0]["failed_criteria"] == ["schema"]
