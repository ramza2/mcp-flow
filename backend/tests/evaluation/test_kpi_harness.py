"""Focused tests for KPI / regression harness helpers (Issue #119 review)."""

from __future__ import annotations

import os
from pathlib import Path

from tests.evaluation.harness.common import (
    eval_root,
    existing_paths,
    load_json,
    load_yaml,
    percent,
    redact_secrets,
)
from tests.evaluation.harness.kpi_measure import (
    diagnostic_selector_decide_timing,
    diagnostic_selector_decision_regression,
    measure_official_kpi_01,
    measure_official_kpi_02,
    measure_official_kpi_03,
    measure_official_kpi_04,
    measure_official_kpi_05,
    measure_official_kpi_06,
)
from tests.evaluation.harness.regression import run_scenario


def test_targets_yaml_has_six_kpis() -> None:
    doc = load_yaml(eval_root() / "targets.yaml")
    assert doc["dataset_bundle_version"] == "evaluation.v1"
    for kpi_id in ("KPI-01", "KPI-02", "KPI-03", "KPI-04", "KPI-05", "KPI-06"):
        assert kpi_id in doc["targets"]
    assert doc["targets"]["KPI-03"]["target_min"] == 10
    assert doc["targets"]["KPI-01"]["target"] is None
    assert doc["targets"]["KPI-02"]["target"] is None


def test_regression_matrix_covers_required_scenarios() -> None:
    matrix = load_yaml(eval_root() / "regression_matrix.yaml")
    ids = {item["id"] for item in matrix["scenarios"]}
    required = {
        "agent_request_nl_to_mcp",
        "workflow_sequential",
        "workflow_parallel",
        "condition",
        "foreach",
        "while",
        "tool_policy_approval",
        "authorization_deny",
        "schedule_occurrence",
        "cancellation",
        "recovery_non_loop",
        "recovery_foreach",
        "recovery_while",
        "recovery_unsafe_ambiguous",
        "tool_factory_execution_path",
        "modelops_timeout_error",
    }
    assert required <= ids


def test_tool_mapping_dataset_versioned() -> None:
    dataset = load_json(eval_root() / "tool-mapping" / "dataset.v1.json")
    assert dataset["dataset_version"] == "tool-mapping.v1"
    assert len(dataset["cases"]) >= 4


def test_offline_kpi_01_official_value_null() -> None:
    metric = measure_official_kpi_01(mode="offline")
    assert metric["kpi_id"] == "KPI-01"
    assert metric["status"] == "NOT_MEASURED"
    assert metric["measured_value"] is None
    assert metric["evidence_complete"] is False
    assert "OFFLINE_MODE" in metric["reason"]


def test_offline_kpi_02_official_value_null() -> None:
    metric = measure_official_kpi_02(mode="offline")
    assert metric["kpi_id"] == "KPI-02"
    assert metric["status"] == "NOT_MEASURED"
    assert metric["measured_value"] is None
    assert metric["evidence_complete"] is False
    assert "OFFLINE_MODE" in metric["reason"]


def test_frozen_selector_diagnostic_separate_from_official() -> None:
    diagnostic = diagnostic_selector_decision_regression()
    assert diagnostic["diagnostic_id"] == "selector_decision_regression"
    assert diagnostic["denominator"] > 0
    assert diagnostic["numerator"] >= 3
    assert diagnostic["measured_value"] == percent(
        diagnostic["numerator"], diagnostic["denominator"]
    )
    timing = diagnostic_selector_decide_timing()
    assert timing["diagnostic_id"] == "selector_decide_microbenchmark"
    assert "total_diagnostic_ms" in timing["segments"]
    # Official offline values stay null even when diagnostics produce numbers.
    assert measure_official_kpi_01(mode="offline")["measured_value"] is None
    assert measure_official_kpi_02(mode="offline")["measured_value"] is None
    assert timing["measured_value"] is not None
    assert diagnostic["measured_value"] == 100.0


def test_required_case_missing_does_not_shrink_denominator() -> None:
    from tests.evaluation.harness.kpi_measure import _run_fixed_denominator_pytest_groups

    groups = [
        {
            "id": "a",
            "title": "a",
            "pytest": ["tests/evaluation/missing_a.py"],
        },
        {
            "id": "b",
            "title": "b",
            "pytest": ["tests/evaluation/missing_b.py"],
        },
    ]
    result = _run_fixed_denominator_pytest_groups(
        groups, include_optional_integration=False
    )
    assert result["declared"] == 2
    assert result["not_run"] == 2
    assert result["evidence_complete"] is False
    # Denominator stays declared; no silent shrink to attempted-only rate.


def test_incomplete_ops_evidence_not_fake_100(monkeypatch) -> None:
    """If a required ops path is missing, official KPI-06 is INCOMPLETE (null)."""
    from tests.evaluation.harness import kpi_measure as km

    def fake_groups(groups, *, path_key="pytest", include_optional_integration):
        return {
            "declared": 6,
            "passed": 5,
            "failed": 0,
            "not_run": 1,
            "evidence_complete": False,
            "details": [{"id": "x", "status": "NOT_RUN"}],
        }

    monkeypatch.setattr(km, "_run_fixed_denominator_pytest_groups", fake_groups)
    metric = measure_official_kpi_06(mode="offline")
    assert metric["status"] == "INCOMPLETE"
    assert metric["measured_value"] is None
    assert metric["denominator"] == 6
    assert metric["evidence_complete"] is False


def test_missing_canonical_regression_path_fails() -> None:
    result = run_scenario(
        {
            "id": "empty",
            "title": "empty",
            "layers": ["unit"],
            "unit": ["tests/evaluation/missing_module.py"],
        },
        include_integration=False,
    )
    assert result["status"] == "FAIL"
    assert result["layers"][0]["reason"] == "MISSING_REQUIRED_PATH"


def test_kpi_03_gap_behavior_preserved() -> None:
    metric = measure_official_kpi_03(mode="offline")
    assert metric["kpi_id"] == "KPI-03"
    if metric["status"] == "MEASURED":
        assert isinstance(metric["measured_value"], int)
        assert metric["target_min"] == 10
        assert metric["gap_to_target_min"] == max(10 - metric["measured_value"], 0)
    else:
        assert metric["measured_value"] is None


def test_live_mode_without_prereq_not_measured() -> None:
    # Ensure live pins are absent for this process.
    for key in (
        "MCPFLOW_KPI_AGENT_VERSION_ID",
        "MCPFLOW_KPI_LLM_PROFILE_ID",
        "MCPFLOW_KPI_EMBEDDING_PROFILE_ID",
        "MCPFLOW_KPI_01_LIVE_RESULT_MS",
        "MCPFLOW_KPI_02_LIVE_NUMERATOR",
        "MCPFLOW_KPI_02_LIVE_DENOMINATOR",
        "MCPFLOW_KPI_SMOKE_MCP_SERVER_ID",
        "MCPFLOW_KPI_LIVE_COMPOSITE",
    ):
        os.environ.pop(key, None)
    m1 = measure_official_kpi_01(mode="live")
    m2 = measure_official_kpi_02(mode="live")
    m4 = measure_official_kpi_04(mode="live")
    m5 = measure_official_kpi_05(mode="live")
    assert m1["status"] == "NOT_MEASURED"
    assert m1["measured_value"] is None
    assert m2["status"] == "NOT_MEASURED"
    assert m2["measured_value"] is None
    # Declared denominator retained; incomplete/not measured — no fake 100%.
    assert m4["status"] in {"NOT_MEASURED", "INCOMPLETE"}
    assert m4["measured_value"] is None
    assert m5["status"] in {"NOT_MEASURED", "INCOMPLETE"}
    assert m5["measured_value"] is None
    if m4["status"] == "INCOMPLETE":
        assert m4["denominator"] == 4
    if m5["status"] == "INCOMPLETE":
        assert m5["denominator"] == 6


def test_redact_secrets_strips_credential_keys() -> None:
    payload = redact_secrets(
        {"token": "abc", "nested": {"api_key": "x", "ok": 1}, "list": [{"password": "p"}]}
    )
    assert payload["token"] == "[REDACTED]"
    assert payload["nested"]["api_key"] == "[REDACTED]"
    assert payload["nested"]["ok"] == 1
    assert payload["list"][0]["password"] == "[REDACTED]"


def test_existing_paths_filters_missing(tmp_path: Path) -> None:
    present = "tests/evaluation/targets.yaml"
    assert existing_paths([present, "tests/evaluation/does-not-exist.yaml"]) == [present]


def test_operations_cases_include_required_categories() -> None:
    doc = load_yaml(eval_root() / "operation" / "cases.yaml")
    categories = {item["category"] for item in doc["cases"]}
    assert {
        "authorization",
        "approval",
        "audit",
        "cancellation",
        "recovery",
    } <= categories
