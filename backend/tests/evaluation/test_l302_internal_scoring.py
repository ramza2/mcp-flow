"""Synthetic offline tests for INTERNAL L3-02 scorer. No model or human review."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import yaml

from tests.evaluation.level3.scoring_l302_internal import (
    CRITERIA,
    gold_gate,
    score_reviewed_dataset,
)

DATASET = Path(__file__).resolve().parent / "level3" / "dataset_l302.internal.v1.yaml"
DIGEST = "a" * 64


def _gold(*, synthetic_approval: bool = False) -> dict:
    gold = yaml.safe_load(DATASET.read_text(encoding="utf-8"))
    if synthetic_approval:
        # Fixture only; do not use synthetic metadata in real gold or reporting.
        gold.update(
            review_status="APPROVED",
            reviewed_by="TEST_OWNER_NOT_A_REAL_APPROVAL",
            reviewed_at="2026-10-22T10:00:00+09:00",
            owner_review={
                "reviewer_ref": "TEST_OWNER_NOT_A_REAL_APPROVAL",
                "reviewed_at": "2026-10-22T10:00:00+09:00",
                "evidence_ref": "test-fixture-only",
                "case_decisions": [
                    {
                        "case_id": case["id"],
                        "decision": "APPROVED",
                        "rationale": "Reviewed synthetic test fixture.",
                    }
                    for case in gold["cases"]
                ],
            },
        )
    return gold


def _evidence(gold: dict, *, fail_ids: set[str] | None = None) -> dict:
    rows = []
    for case in gold["cases"]:
        checks = dict.fromkeys(CRITERIA, True)
        if case["id"] in (fail_ids or set()):
            checks["intent"] = False
        rows.append({
            "case_id": case["id"],
            "run_ref": "synthetic-test-only",
            "observed_sha256": DIGEST,
            "schema_valid": True,
            "review": {
                "reviewer_ref": "TEST_EVALUATOR_NOT_A_REAL_PERSON_RECORD",
                "reviewed_at": "2026-10-22T11:00:00+09:00",
                "evidence_ref": "synthetic-test-only",
                "rationale": "Synthetic offline test judgment, no model output.",
                "checks": checks,
            },
        })
    return {
        "gold_sha256": DIGEST,
        "run_ref": "synthetic-test-only",
        "cases": rows,
    }


def test_internal_unapproved_gold_blocks_scoring() -> None:
    gold = _gold()
    assert gold["review_status"] == "PENDING_REVIEW"
    assert gold_gate(gold)["reason"] == "INTERNAL_GOLD_NOT_APPROVED"
    assert score_reviewed_dataset(
        gold, gold_sha256=DIGEST, evidence=_evidence(gold)
    ) == {
        "ok": False,
        "judgment": "INCOMPLETE",
        "reason": "INTERNAL_GOLD_NOT_APPROVED",
    }


def test_synthetic_owner_must_review_each_of_twenty_cases() -> None:
    gold = _gold(synthetic_approval=True)
    assert gold_gate(gold)["ok"] is True
    gold["owner_review"]["case_decisions"].pop()
    assert gold_gate(gold)["reason"] == "INTERNAL_OWNER_CASE_REVIEWS_MISSING"
    gold = _gold(synthetic_approval=True)
    gold["owner_review"]["reviewer_ref"] = "SOMEONE_ELSE"
    assert gold_gate(gold)["reason"] == "INTERNAL_OWNER_REVIEW_MISSING"


def test_synthetic_85_percent_threshold() -> None:
    gold = _gold(synthetic_approval=True)
    for failed_count, expected in (
        (0, "INTERNAL_MET"),
        (3, "INTERNAL_MET"),
        (4, "INTERNAL_NOT_MET"),
    ):
        fail_ids = {c["id"] for c in gold["cases"][:failed_count]}
        score = score_reviewed_dataset(
            gold, gold_sha256=DIGEST,
            evidence=_evidence(gold, fail_ids=fail_ids),
        )
        assert score["judgment"] == expected
        assert score["success_count"] == 20 - failed_count
        assert score["success_rate_percent"] == 100 - failed_count * 5
        assert score["observed_cases"] == 20


def test_missing_run_duplicate_and_hash_are_incomplete() -> None:
    gold = _gold(synthetic_approval=True)
    evidence = _evidence(gold)
    evidence["cases"].pop()
    assert score_reviewed_dataset(
        gold, gold_sha256=DIGEST, evidence=evidence
    )["reason"] == "EVIDENCE_COUNT_MISMATCH"

    duplicate = _evidence(gold)
    duplicate["cases"][1]["case_id"] = duplicate["cases"][0]["case_id"]
    assert score_reviewed_dataset(
        gold, gold_sha256=DIGEST, evidence=duplicate
    )["reason"] == "EVIDENCE_CASE_ID_OR_RUN_MISMATCH"

    wrong_hash = _evidence(gold)
    wrong_hash["gold_sha256"] = "b" * 64
    assert score_reviewed_dataset(
        gold, gold_sha256=DIGEST, evidence=wrong_hash
    )["reason"] == "GOLD_HASH_MISMATCH"

    wrong_target = score_reviewed_dataset(
        gold, gold_sha256=DIGEST, evidence=_evidence(gold),
        target_min_percent=0,
    )
    assert wrong_target["reason"] == "TARGET_MISMATCH"


def test_schema_failures_count_and_eight_checks_are_required() -> None:
    gold = _gold(synthetic_approval=True)
    evidence = _evidence(gold)
    bad = evidence["cases"][0]
    bad["schema_valid"] = False
    assert score_reviewed_dataset(
        gold, gold_sha256=DIGEST, evidence=evidence
    )["reason"] == "SCHEMA_REVIEW_MISMATCH"
    bad["review"]["checks"]["schema"] = False
    score = score_reviewed_dataset(
        gold, gold_sha256=DIGEST, evidence=evidence
    )
    assert score["success_count"] == 19
    assert score["case_results"][0]["failed_criteria"] == ["schema"]
    tampered = deepcopy(evidence)
    del tampered["cases"][1]["review"]["checks"]["capability"]
    assert score_reviewed_dataset(
        gold, gold_sha256=DIGEST, evidence=tampered
    )["reason"] == "EIGHT_CRITERIA_REVIEW_INVALID"
