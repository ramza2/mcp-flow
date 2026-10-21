"""Static review-time checks for L3-02 v2 candidate gold. No model calls.

This test does NOT approve the gold or perform semantic model scoring.
The v2 evaluator must remain separate from approved v1 scoring/runner.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parent / "level3"


def load_yaml(path: Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_v2_candidate_gold_is_complete_and_not_yet_approved() -> None:
    gold = load_yaml(ROOT / "dataset_l302.v2.yaml")

    assert gold["schema_version"] == "mcpflow.level3_analyzer_dataset.v2"
    assert gold["dataset_version"] == "level3-natural-language-analysis.v2"
    assert gold["method_review_status"] == "APPROVED"
    # Deliberately fail closed until separate gold-review commit changes the gate.
    assert gold["review_status"] == "PENDING_REVIEW"
    assert gold["reviewed_by"] is None
    assert gold["reviewed_at"] is None
    assert gold["measurement_performed"] is False

    cases = gold["cases"]
    assert len(cases) == 20
    assert len({row["id"] for row in cases}) == 20
    assert len({row["request_text"] for row in cases}) == 20
    assert Counter(row["family"] for row in cases) == {
        "lookup": 6, "create": 4, "update": 4, "verbatim": 4, "ambiguous": 2,
    }

    original = load_yaml(ROOT / "dataset_l302.v1.yaml")
    prior_requests = {row["request_text"] for row in original["cases"]}
    assert not prior_requests.intersection(row["request_text"] for row in cases)


def test_v2_gold_semantic_fields_and_temporal_bounds() -> None:
    cases = load_yaml(ROOT / "dataset_l302.v2.yaml")["cases"]

    for row in cases:
        gold = row["gold"]
        assert isinstance(row["review_note"], str) and row["review_note"].strip()
        assert isinstance(row["request_text"], str) and row["request_text"].strip()
        assert set(gold["intent"]) == {"domain", "action", "object"}
        assert all(str(value).strip() for value in gold["intent"].values())
        assert isinstance(gold["entities"], list)
        assert gold["risk"]
        assert set(gold["risk"]) <= {
            "READ_ONLY", "IDEMPOTENT_WRITE", "NON_IDEMPOTENT_WRITE",
            "DESTRUCTIVE", "UNKNOWN",
        }

        roles = set()
        for entity in gold["entities"]:
            assert set(entity) == {"role", "value", "source"}
            assert entity["role"] not in roles
            roles.add(entity["role"])
            assert entity["role"] and entity["value"]
            assert entity["source"] == "USER_EXPLICIT"

        if row["family"] == "ambiguous":
            assert gold["needs_clarification"] is True
            assert gold["capability"] is None
            assert gold["missing_meaning"]
            assert gold["risk"] == ["UNKNOWN"]
        else:
            assert gold["needs_clarification"] is False
            capability = gold["capability"]
            assert isinstance(capability, dict)
            assert set(capability) == {"domain", "action"}
            assert all(value for value in capability.values())

        temporal = gold.get("temporal")
        if temporal:
            assert temporal["kind"] in {"date", "range", "month", "datetime"}
            for value in temporal.get("dates", []):
                assert date.fromisoformat(value).isoformat() == value
            for value in temporal.get("months", []):
                assert len(value) == 7
                assert date.fromisoformat(value + "-01").strftime("%Y-%m") == value
            if temporal["kind"] == "range":
                assert len(temporal["dates"]) == 2
                assert temporal["dates"][0] <= temporal["dates"][1]
            if temporal["kind"] == "datetime":
                assert len(temporal["dates"]) == 1
                clock = temporal["time_24h"]
                assert len(clock) == 5 and clock[2] == ":"
                hh, mm = clock.split(":")
                assert 0 <= int(hh) < 24 and 0 <= int(mm) < 60
