"""Focused tests for KPI / regression harness helpers (Issue #119)."""

from __future__ import annotations

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
    measure_kpi_01_response_time,
    measure_kpi_02_tool_mapping,
)
from tests.evaluation.harness.regression import run_scenario


def test_targets_yaml_has_six_kpis() -> None:
    doc = load_yaml(eval_root() / "targets.yaml")
    assert doc["dataset_bundle_version"] == "evaluation.v1"
    for kpi_id in ("KPI-01", "KPI-02", "KPI-03", "KPI-04", "KPI-05", "KPI-06"):
        assert kpi_id in doc["targets"]
    assert doc["targets"]["KPI-03"]["target_min"] == 10
    # Do not invent numeric gates for KPIs without approved targets.
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
    assert "max_candidates" in dataset["selection_settings"]


def test_kpi_02_top1_accuracy_on_frozen_dataset() -> None:
    metric = measure_kpi_02_tool_mapping()
    assert metric["kpi_id"] == "KPI-02"
    assert metric["denominator"] is not None and metric["denominator"] > 0
    assert metric["numerator"] is not None
    assert metric["numerator"] <= metric["denominator"]
    assert metric["measured_value"] == percent(metric["numerator"], metric["denominator"])
    # Frozen happy-path cases should pass with pinned rerank.
    assert metric["numerator"] >= 3


def test_kpi_01_reports_segments_without_invented_sla() -> None:
    metric = measure_kpi_01_response_time()
    assert metric["kpi_id"] == "KPI-01"
    assert metric["target"] is None
    assert "total_response_ms" in metric["segments"]
    assert metric["measured_value"] == metric["segments"]["total_response_ms"]


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


def test_run_scenario_skips_when_no_paths() -> None:
    result = run_scenario(
        {
            "id": "empty",
            "title": "empty",
            "layers": ["unit"],
            "unit": ["tests/evaluation/missing_module.py"],
        },
        include_integration=False,
    )
    assert result["status"] == "SKIP"
