"""Read-only static gold audit for the L3-02 internal one-owner review.

Not an LLM test, human approval, performance evaluation, or official score.
Prints the exact gold case-set fingerprint the real reviewer must attest.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from tests.evaluation.harness.common import load_yaml
from tests.evaluation.level3.scoring_l302_internal import (
    DATASET_VERSION,
    FAMILY_COUNTS,
    case_set_sha256,
    gold_gate,
)

GOLD_FILE = Path(__file__).resolve().parent / "dataset_l302.internal.v1.yaml"
EXPECTED_RISKS = frozenset({
    "READ_ONLY", "IDEMPOTENT_WRITE", "NON_IDEMPOTENT_WRITE",
    "DESTRUCTIVE", "UNKNOWN",
})


def audit_gold(gold: Any) -> dict[str, Any]:
    """Static shape review only; no interpretation of model predictions."""
    issues: list[dict[str, str]] = []
    if not isinstance(gold, dict) or gold.get("dataset_version") != DATASET_VERSION:
        return {
            "ok": False,
            "reason": "DATASET_VERSION_INVALID",
            "issues": [{"case_id": "-", "reason": "DATASET_VERSION_INVALID"}],
        }
    cases = gold.get("cases")
    if not isinstance(cases, list):
        return {
            "ok": False,
            "reason": "CASES_NOT_LIST",
            "issues": [{"case_id": "-", "reason": "CASES_NOT_LIST"}],
        }
    ids: list[str] = []
    families: list[str] = []
    for i, row in enumerate(cases):
        cid = row.get("id") if isinstance(row, dict) else None
        cid = cid if isinstance(cid, str) and cid.strip() else f"ROW_{i + 1}"
        if not isinstance(row, dict):
            issues.append({"case_id": cid, "reason": "CASE_NOT_OBJECT"})
            continue
        if not isinstance(row.get("id"), str) or not row["id"].strip():
            issues.append({"case_id": cid, "reason": "CASE_ID_INVALID"})
        else:
            ids.append(row["id"])
        family = row.get("family")
        if not isinstance(family, str) or family not in FAMILY_COUNTS:
            issues.append({"case_id": cid, "reason": "UNKNOWN_FAMILY"})
        else:
            families.append(family)
        if not isinstance(row.get("request_text"), str) or not row["request_text"].strip():
            issues.append({"case_id": cid, "reason": "REQUEST_BLANK"})
        gold_row = row.get("gold")
        if not isinstance(gold_row, dict):
            issues.append({"case_id": cid, "reason": "GOLD_MISSING"})
            continue
        intent = gold_row.get("intent")
        if (
            not isinstance(intent, dict)
            or set(intent) != {"domain", "action", "object"}
            or any(not isinstance(v, str) or not v.strip() for v in intent.values())
        ):
            issues.append({"case_id": cid, "reason": "GOLD_INTENT_INVALID"})
        capability = gold_row.get("capability")
        if family == "ambiguous":
            if capability is not None or gold_row.get("needs_clarification") is not True:
                issues.append({"case_id": cid, "reason": "AMBIGUITY_GOLD_INVALID"})
        elif (
            not isinstance(capability, dict)
            or set(capability) != {"domain", "action"}
            or any(not isinstance(v, str) or not v.strip() for v in capability.values())
            or gold_row.get("needs_clarification") is not False
        ):
            issues.append({"case_id": cid, "reason": "CAPABILITY_GOLD_INVALID"})
        ents = gold_row.get("entities")
        if (
            not isinstance(ents, list)
            or any(
                not isinstance(e, dict)
                or set(e) != {"role", "value", "source"}
                or not isinstance(e.get("role"), str)
                or not e["role"].strip()
                or not isinstance(e.get("value"), str)
                or not e["value"].strip()
                or e.get("source") != "USER_EXPLICIT"
                for e in ents
            )
        ):
            issues.append({"case_id": cid, "reason": "ENTITIES_GOLD_INVALID"})
        risks = gold_row.get("risk")
        if (
            not isinstance(risks, list)
            or not risks
            or any(not isinstance(r, str) or r not in EXPECTED_RISKS for r in risks)
        ):
            issues.append({"case_id": cid, "reason": "RISK_GOLD_INVALID"})
        if family == "ambiguous" and risks != ["UNKNOWN"]:
            issues.append({"case_id": cid, "reason": "AMBIGUOUS_RISK_INVALID"})

    if len(cases) != 20 or len(ids) != 20 or len(set(ids)) != 20:
        issues.append({"case_id": "-", "reason": "CASE_COUNT_OR_ID_INVALID"})
    if Counter(families) != Counter(FAMILY_COUNTS):
        issues.append({"case_id": "-", "reason": "FAMILY_DISTRIBUTION_INVALID"})
    texts = [
        x.get("request_text") for x in cases
        if isinstance(x, dict) and isinstance(x.get("request_text"), str)
    ]
    if len(texts) != 20 or len(set(texts)) != 20:
        issues.append({"case_id": "-", "reason": "REQUEST_DUPLICATE_OR_INVALID"})
    return {
        "ok": not issues,
        "reason": None if not issues else "STATIC_GOLD_VALIDATION_FAILED",
        "dataset_version": DATASET_VERSION,
        "review_status": gold.get("review_status"),
        "case_count": len(cases),
        "family_counts": dict(Counter(families)),
        "case_set_sha256": case_set_sha256(cases),
        "gold_gate": gold_gate(gold).get("reason"),
        "owner_confirmation": "NOT_PERFORMED_BY_STATIC_AUDIT",
        "issues": issues,
    }


def main() -> int:
    report = audit_gold(load_yaml(GOLD_FILE))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
