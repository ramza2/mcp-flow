"""Pure offline checks for unapproved L3-02 internal gold review preparation."""

from copy import deepcopy

import yaml

from tests.evaluation.level3 import audit_l302_internal_gold as audit


def _gold() -> dict:
    return yaml.safe_load(audit.GOLD_FILE.read_text(encoding="utf-8"))


def test_draft_gold_is_statically_ready_but_not_human_approved() -> None:
    result = audit.audit_gold(_gold())
    assert result["ok"] is True
    assert result["case_count"] == 20
    assert result["gold_gate"] == "INTERNAL_GOLD_NOT_APPROVED"
    assert result["review_status"] == "PENDING_REVIEW"
    assert result["owner_confirmation"] == "NOT_PERFORMED_BY_STATIC_AUDIT"
    assert len(result["case_set_sha256"]) == 64
    assert result["issues"] == []


def test_audit_rejects_missing_gold_or_unjustified_provenance() -> None:
    gold = _gold()
    gold["cases"][0]["gold"]["entities"][0]["source"] = "SYSTEM_INFERRED"
    result = audit.audit_gold(gold)
    assert result["ok"] is False
    assert any(x["reason"] == "ENTITIES_GOLD_INVALID" for x in result["issues"])

    gold = _gold()
    gold["cases"][0]["gold"] = {}
    result = audit.audit_gold(gold)
    assert any(x["reason"] == "GOLD_INTENT_INVALID" for x in result["issues"])


def test_audit_rejects_duplicate_or_removed_cases() -> None:
    gold = _gold()
    gold["cases"][1] = deepcopy(gold["cases"][0])
    result = audit.audit_gold(gold)
    assert result["ok"] is False
    assert any(x["reason"] == "CASE_COUNT_OR_ID_INVALID" for x in result["issues"])
    gold["cases"].pop()
    assert audit.audit_gold(gold)["ok"] is False


def test_audit_rejects_silent_write_to_read_risk_or_ambiguous_capability() -> None:
    gold = _gold()
    gold["cases"][-1]["gold"]["capability"] = {"domain": "task", "action": "run"}
    result = audit.audit_gold(gold)
    assert any(x["reason"] == "AMBIGUITY_GOLD_INVALID" for x in result["issues"])

    gold = _gold()
    gold["cases"][0]["gold"]["risk"] = ["INVALID"]
    result = audit.audit_gold(gold)
    assert any(x["reason"] == "RISK_GOLD_INVALID" for x in result["issues"])

    gold = _gold()
    gold["cases"][6]["gold"]["risk"] = ["READ_ONLY"]
    result = audit.audit_gold(gold)
    assert any(x["reason"] == "WRITE_FAMILY_RISK_INVALID" for x in result["issues"])

    gold = _gold()
    gold["cases"][1]["gold"]["risk"] = ["NON_IDEMPOTENT_WRITE"]
    result = audit.audit_gold(gold)
    assert any(x["reason"] == "READ_ONLY_FAMILY_RISK_INVALID" for x in result["issues"])


def test_case_fingerprint_changes_when_request_or_gold_changes() -> None:
    gold = _gold()
    baseline = audit.audit_gold(gold)["case_set_sha256"]
    gold["cases"][1]["gold"]["entities"][0]["value"] = "RB-205"
    result = audit.audit_gold(gold)
    assert result["case_set_sha256"] != baseline
    gold = _gold()
    gold["cases"][1]["request_text"] = "다른 요청"
    assert audit.audit_gold(gold)["case_set_sha256"] != baseline
