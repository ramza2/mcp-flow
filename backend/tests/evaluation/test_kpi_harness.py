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
        "MCPFLOW_KPI_AGENT_REQUEST_ID",
        "MCPFLOW_KPI_02_EVIDENCE_MAP",
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


def test_naked_env_result_values_cannot_produce_measured(monkeypatch) -> None:
    """Review blocker: self-asserted env values must never yield MEASURED."""
    monkeypatch.setenv("MCPFLOW_KPI_AGENT_VERSION_ID", "00000000-0000-4000-8000-000000000001")
    monkeypatch.setenv("MCPFLOW_KPI_LLM_PROFILE_ID", "00000000-0000-4000-8000-000000000002")
    monkeypatch.setenv(
        "MCPFLOW_KPI_EMBEDDING_PROFILE_ID", "00000000-0000-4000-8000-000000000003"
    )
    monkeypatch.setenv("MCPFLOW_KPI_01_LIVE_RESULT_MS", "123.4")
    monkeypatch.setenv("MCPFLOW_KPI_02_LIVE_NUMERATOR", "4")
    monkeypatch.setenv("MCPFLOW_KPI_02_LIVE_DENOMINATOR", "4")
    monkeypatch.setenv("MCPFLOW_KPI_04_RESULT_SMOKE_CONNECT", "pass")
    monkeypatch.setenv("MCPFLOW_KPI_04_RESULT_SMOKE_DISCOVERY", "pass")
    monkeypatch.setenv("MCPFLOW_KPI_04_RESULT_SMOKE_APPLY_REGISTER", "pass")
    monkeypatch.setenv("MCPFLOW_KPI_04_RESULT_SMOKE_VERIFICATION", "pass")
    monkeypatch.setenv("MCPFLOW_KPI_05_RESULT_SEQUENTIAL_TOOL_CHAIN", "pass")
    monkeypatch.setenv("MCPFLOW_KPI_05_RESULT_PARALLEL_JOIN_WAVE", "pass")
    monkeypatch.setenv("MCPFLOW_KPI_05_RESULT_CONDITION_BRANCH", "pass")
    monkeypatch.setenv("MCPFLOW_KPI_05_RESULT_FOREACH_LOOP", "pass")
    monkeypatch.setenv("MCPFLOW_KPI_05_RESULT_WHILE_LOOP", "pass")
    monkeypatch.setenv("MCPFLOW_KPI_05_RESULT_APPROVAL_WAIT_RESUME", "pass")
    # Intentionally omit durable selectors / evidence map / smoke server id.
    monkeypatch.delenv("MCPFLOW_KPI_AGENT_REQUEST_ID", raising=False)
    monkeypatch.delenv("MCPFLOW_KPI_02_EVIDENCE_MAP", raising=False)
    monkeypatch.delenv("MCPFLOW_KPI_SMOKE_MCP_SERVER_ID", raising=False)

    m1 = measure_official_kpi_01(mode="live")
    m2 = measure_official_kpi_02(mode="live")
    m4 = measure_official_kpi_04(mode="live")
    m5 = measure_official_kpi_05(mode="live")
    assert m1["status"] != "MEASURED"
    assert m1["measured_value"] is None
    assert m2["status"] != "MEASURED"
    assert m2["measured_value"] is None
    assert m4["status"] != "MEASURED"
    assert m4["measured_value"] is None
    assert m5["status"] != "MEASURED"
    assert m5["measured_value"] is None


def test_durable_helpers_can_score_measured_paths() -> None:
    """Valid durable evidence helpers can produce MEASURED inputs (no env results)."""
    from datetime import UTC, datetime, timedelta

    from tests.evaluation.harness.live_evidence import (
        compute_kpi_01_duration_ms,
        score_execution_terminal,
        score_kpi_02_case,
    )

    start = datetime(2026, 10, 16, 8, 0, 0, tzinfo=UTC)
    end = start + timedelta(milliseconds=1500)
    assert compute_kpi_01_duration_ms(t_start=start, t_end=end) == 1500.0

    assert (
        score_kpi_02_case(
            expected_tool_name="lookup_weather",
            expected_decision="AUTO_SELECT",
            selected_tool_name="lookup_weather",
            decision="AUTO_SELECT",
        )
        is True
    )
    assert (
        score_kpi_02_case(
            expected_tool_name="lookup_weather",
            expected_decision="AUTO_SELECT",
            selected_tool_name="echo",
            decision="AUTO_SELECT",
        )
        is False
    )
    assert score_execution_terminal("SUCCEEDED") == "PASS"
    assert score_execution_terminal("FAILED") == "FAIL"
    assert score_execution_terminal("RUNNING") == "NOT_RUN"


def test_kpi01_wrong_agent_version_pin_blocks_measured(monkeypatch) -> None:
    """KPI-01: AgentRequest.agent_version_id must match pin for MEASURED."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from tests.evaluation.harness import kpi_measure as km
    from tests.evaluation.harness.live_evidence import Kpi01Evidence

    pinned = uuid4()
    other = uuid4()
    monkeypatch.setenv("MCPFLOW_KPI_AGENT_VERSION_ID", str(pinned))
    monkeypatch.setenv("MCPFLOW_KPI_LLM_PROFILE_ID", str(uuid4()))
    monkeypatch.setenv("MCPFLOW_KPI_EMBEDDING_PROFILE_ID", str(uuid4()))
    monkeypatch.setenv("MCPFLOW_KPI_AGENT_REQUEST_ID", str(uuid4()))
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+asyncpg://unused/db")

    evidence = Kpi01Evidence(
        agent_request_id=uuid4(),
        tool_selection_run_id=uuid4(),
        agent_version_id=other,
        t_start=datetime(2026, 10, 16, 8, 0, 0, tzinfo=UTC),
        t_end=datetime(2026, 10, 16, 8, 0, 1, tzinfo=UTC),
        duration_ms=1000.0,
        decision="AUTO_SELECT",
        lineage_ok=False,
        lineage_reasons=("AGENT_VERSION_PIN_MISMATCH",),
        evidence_ids={"agent_version_id": str(other)},
    )

    async def fake_with_session(factory):
        return evidence

    monkeypatch.setattr(km, "_live_prereq_missing", lambda: [])
    monkeypatch.setattr(
        "tests.evaluation.harness.live_evidence.with_session", fake_with_session
    )
    monkeypatch.setattr(
        "tests.evaluation.harness.live_evidence.load_kpi_01_evidence",
        lambda *a, **k: evidence,
    )

    metric = measure_official_kpi_01(mode="live")
    assert metric["status"] != "MEASURED"
    assert metric["measured_value"] is None
    assert "LINEAGE_MISMATCH" in metric["reason"]
    assert "AGENT_VERSION_PIN_MISMATCH" in metric["reason"]


def test_kpi01_valid_lineage_can_measure(monkeypatch) -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    from tests.evaluation.harness import kpi_measure as km
    from tests.evaluation.harness.live_evidence import Kpi01Evidence

    pinned = uuid4()
    monkeypatch.setenv("MCPFLOW_KPI_AGENT_VERSION_ID", str(pinned))
    monkeypatch.setenv("MCPFLOW_KPI_LLM_PROFILE_ID", str(uuid4()))
    monkeypatch.setenv("MCPFLOW_KPI_EMBEDDING_PROFILE_ID", str(uuid4()))
    monkeypatch.setenv("MCPFLOW_KPI_AGENT_REQUEST_ID", str(uuid4()))
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+asyncpg://unused/db")

    evidence = Kpi01Evidence(
        agent_request_id=uuid4(),
        tool_selection_run_id=uuid4(),
        agent_version_id=pinned,
        t_start=datetime(2026, 10, 16, 8, 0, 0, tzinfo=UTC),
        t_end=datetime(2026, 10, 16, 8, 0, 1, tzinfo=UTC),
        duration_ms=1500.0,
        decision="AUTO_SELECT",
        lineage_ok=True,
        lineage_reasons=(),
        evidence_ids={"agent_version_id": str(pinned)},
    )

    async def fake_with_session(factory):
        return evidence

    monkeypatch.setattr(km, "_live_prereq_missing", lambda: [])
    monkeypatch.setattr(
        "tests.evaluation.harness.live_evidence.with_session", fake_with_session
    )

    metric = measure_official_kpi_01(mode="live")
    assert metric["status"] == "MEASURED"
    assert metric["measured_value"] == 1500.0


def test_kpi02_selector_map_rejects_expected_label_override() -> None:
    from uuid import uuid4

    from tests.evaluation.harness.live_evidence import parse_kpi_02_selector_map

    payload = {
        "cases": [
            {
                "case_id": "weather-seoul-ko",
                "agent_request_id": str(uuid4()),
                "expected_tool_version_id": str(uuid4()),
                "expected_decision": "AUTO_SELECT",
            }
        ]
    }
    selectors, errors = parse_kpi_02_selector_map(payload)
    assert selectors == {}
    assert any("FORBIDDEN_EXPECTED_LABEL_KEYS" in e for e in errors)


def test_kpi02_unknown_duplicate_missing_case_incomplete(monkeypatch, tmp_path: Path) -> None:
    from uuid import uuid4

    from tests.evaluation.harness import kpi_measure as km

    pinned = uuid4()
    monkeypatch.setenv("MCPFLOW_KPI_AGENT_VERSION_ID", str(pinned))
    monkeypatch.setenv("MCPFLOW_KPI_LLM_PROFILE_ID", str(uuid4()))
    monkeypatch.setenv("MCPFLOW_KPI_EMBEDDING_PROFILE_ID", str(uuid4()))
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+asyncpg://unused/db")
    monkeypatch.setattr(km, "_live_prereq_missing", lambda: [])

    # Duplicate case id
    dup_map = tmp_path / "dup.json"
    rid = str(uuid4())
    dup_map.write_text(
        '{"cases":['
        f'{{"case_id":"weather-seoul-ko","agent_request_id":"{rid}"}},'
        f'{{"case_id":"weather-seoul-ko","agent_request_id":"{uuid4()}"}}'
        "]}",
        encoding="utf-8",
    )
    monkeypatch.setenv("MCPFLOW_KPI_02_EVIDENCE_MAP", str(dup_map))
    dup_metric = measure_official_kpi_02(mode="live")
    assert dup_metric["status"] == "INCOMPLETE"
    assert dup_metric["measured_value"] is None
    assert "DUPLICATE_CASE_ID" in dup_metric["reason"]

    # Unknown case id
    unk_map = tmp_path / "unk.json"
    unk_map.write_text(
        (
            '{"cases":[{"case_id":"not-a-real-case",'
            f'"agent_request_id":"{uuid4()}"}}]}}'
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MCPFLOW_KPI_02_EVIDENCE_MAP", str(unk_map))
    unk_metric = measure_official_kpi_02(mode="live")
    assert unk_metric["status"] == "INCOMPLETE"
    assert "UNKNOWN_CASE_ID" in unk_metric["reason"]

    # Missing required case → NOT_RUN → INCOMPLETE (fixed denominator)
    from tests.evaluation.harness.live_evidence import (
        Kpi02CaseEvidence,
        load_canonical_kpi_02_cases,
    )

    dataset = load_json(eval_root() / "tool-mapping" / "dataset.v1.json")
    eligible = load_canonical_kpi_02_cases(dataset)
    assert len(eligible) >= 1
    first = str(eligible[0]["id"])
    miss_map = tmp_path / "miss.json"
    miss_map.write_text(
        f'{{"cases":[{{"case_id":"{first}","agent_request_id":"{uuid4()}"}}]}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("MCPFLOW_KPI_02_EVIDENCE_MAP", str(miss_map))

    async def fake_eval(session, *, dataset_cases, selectors):
        return [
            Kpi02CaseEvidence(
                case_id=str(c["id"]),
                agent_request_id=selectors.get(str(c["id"])),
                expected_tool_name=c.get("expected_tool_name"),
                expected_decision=c.get("expected_decision"),
                tool_selection_run_id=None,
                selected_tool_name=None,
                decision=None,
                correct=None,
                status="PASS" if str(c["id"]) in selectors else "NOT_RUN",
                reason=None if str(c["id"]) in selectors else "MISSING_SELECTOR_FOR_DECLARED_CASE",
            )
            for c in dataset_cases
        ]

    async def fake_with_session(factory):
        return await factory(None)

    monkeypatch.setattr(
        "tests.evaluation.harness.live_evidence.evaluate_kpi_02_cases", fake_eval
    )
    monkeypatch.setattr(
        "tests.evaluation.harness.live_evidence.with_session", fake_with_session
    )
    miss_metric = measure_official_kpi_02(mode="live")
    assert miss_metric["status"] == "INCOMPLETE"
    assert miss_metric["measured_value"] is None
    assert miss_metric["denominator"] == len(eligible)


def test_kpi02_wrong_agent_version_blocks_official_score() -> None:
    from uuid import uuid4

    from tests.evaluation.harness.live_evidence import validate_agent_request_lineage

    pinned = uuid4()
    reasons = validate_agent_request_lineage(
        request_agent_version_id=uuid4(),
        selection_agent_version_id=None,
        selection_llm_profile_id=None,
        selection_embedding_profile_id=None,
        agent_version_llm_profile_id=None,
        pinned_agent_version_id=pinned,
        pinned_llm_profile_id=None,
        pinned_embedding_profile_id=None,
    )
    assert "AGENT_VERSION_PIN_MISMATCH" in reasons


def test_kpi04_other_server_verification_and_missing_apply_block_pass() -> None:
    from tests.evaluation.harness.live_evidence import discovery_has_durable_apply

    assert discovery_has_durable_apply(None) is False
    assert discovery_has_durable_apply({"tools_registered": 3}) is False
    assert discovery_has_durable_apply({"factory_apply_executed": True}) is True
    assert (
        discovery_has_durable_apply(
            {
                "factory_reconciliation": {"status": "CLEAN", "apply_executed": True},
            }
        )
        is True
    )


def test_kpi04_evaluate_rejects_wrong_server_verification() -> None:
    """Unit-level: VERIFICATION_SERVER_MISMATCH status is FAIL not PASS."""
    from tests.evaluation.harness.live_evidence import Kpi04AttemptEvidence

    bad = Kpi04AttemptEvidence(
        attempt_id="smoke-verification",
        operation="verification",
        success=False,
        status="FAIL",
        evidence_ids={"failure": "server_mismatch"},
        failure_category="VERIFICATION_SERVER_MISMATCH",
    )
    apply_missing = Kpi04AttemptEvidence(
        attempt_id="smoke-apply-register",
        operation="apply/register",
        success=False,
        status="NOT_RUN",
        evidence_ids={},
        failure_category="DURABLE_APPLY_EVIDENCE_MISSING",
    )
    assert bad.status != "PASS"
    assert apply_missing.status != "PASS"


def test_kpi04_incomplete_when_apply_not_run(monkeypatch) -> None:
    from uuid import uuid4

    from tests.evaluation.harness.live_evidence import Kpi04AttemptEvidence

    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+asyncpg://unused/db")
    monkeypatch.setenv("MCPFLOW_KPI_SMOKE_MCP_SERVER_ID", str(uuid4()))

    attempts = [
        Kpi04AttemptEvidence(
            attempt_id="smoke-connect",
            operation="connect",
            success=True,
            status="PASS",
            evidence_ids={},
        ),
        Kpi04AttemptEvidence(
            attempt_id="smoke-discovery",
            operation="discovery",
            success=True,
            status="PASS",
            evidence_ids={},
        ),
        Kpi04AttemptEvidence(
            attempt_id="smoke-apply-register",
            operation="apply/register",
            success=False,
            status="NOT_RUN",
            evidence_ids={},
            failure_category="DURABLE_APPLY_EVIDENCE_MISSING",
        ),
        Kpi04AttemptEvidence(
            attempt_id="smoke-verification",
            operation="verification",
            success=False,
            status="FAIL",
            evidence_ids={},
            failure_category="VERIFICATION_SERVER_MISMATCH",
        ),
    ]

    async def fake_with_session(factory):
        return attempts

    monkeypatch.setattr(
        "tests.evaluation.harness.live_evidence.with_session", fake_with_session
    )
    metric = measure_official_kpi_04(mode="live")
    assert metric["status"] != "MEASURED" or metric["measured_value"] != 100.0
    assert metric["status"] == "INCOMPLETE"
    assert metric["measured_value"] is None
    assert any(
        a.get("failure_category") == "DURABLE_APPLY_EVIDENCE_MISSING"
        for a in metric["attempts"]
    )


def test_kpi05_plan_shape_and_execution_reuse() -> None:
    from tests.evaluation.harness.live_evidence import plan_matches_scenario_kind

    seq_ok = {
        "steps": [
            {"id": "t1", "type": "TOOL", "depends_on": []},
            {"id": "t2", "type": "TOOL", "depends_on": ["t1"]},
        ]
    }
    assert plan_matches_scenario_kind(seq_ok, "sequential")[0] is True
    assert plan_matches_scenario_kind(
        {"steps": [{"id": "t1", "type": "TOOL", "depends_on": []}]},
        "sequential",
    )[0] is False

    par_ok = {
        "steps": [
            {"id": "a", "type": "TOOL", "depends_on": []},
            {"id": "b", "type": "TOOL", "depends_on": []},
            {"id": "j", "type": "JOIN", "depends_on": ["a", "b"]},
        ]
    }
    assert plan_matches_scenario_kind(par_ok, "parallel")[0] is True
    assert plan_matches_scenario_kind(seq_ok, "parallel")[0] is False

    assert plan_matches_scenario_kind(
        {"steps": [{"id": "c", "type": "CONDITION", "depends_on": []}]},
        "condition",
    )[0] is True
    assert plan_matches_scenario_kind(
        {
            "steps": [
                {
                    "id": "l",
                    "type": "LOOP",
                    "depends_on": [],
                    "config": {"mode": "FOR_EACH"},
                }
            ]
        },
        "foreach",
    )[0] is True
    assert plan_matches_scenario_kind(
        {
            "steps": [
                {
                    "id": "l",
                    "type": "LOOP",
                    "depends_on": [],
                    "config": {"mode": "WHILE"},
                }
            ]
        },
        "while",
    )[0] is True
    # Wrong shape for while (foreach config) must not pass.
    assert plan_matches_scenario_kind(
        {
            "steps": [
                {
                    "id": "l",
                    "type": "LOOP",
                    "depends_on": [],
                    "config": {"mode": "FOR_EACH"},
                }
            ]
        },
        "while",
    )[0] is False


def test_kpi05_reused_execution_blocks_perfect_score(monkeypatch) -> None:
    from uuid import uuid4

    from tests.evaluation.harness.live_evidence import Kpi05ScenarioEvidence

    shared = uuid4()
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+asyncpg://unused/db")
    results = [
        Kpi05ScenarioEvidence(
            scenario_id="sequential_tool_chain",
            execution_id=shared,
            status="PASS",
            success=True,
            execution_status="SUCCEEDED",
            evidence_ids={"execution_id": str(shared)},
        ),
        Kpi05ScenarioEvidence(
            scenario_id="parallel_join_wave",
            execution_id=shared,
            status="FAIL",
            success=False,
            execution_status=None,
            evidence_ids={"execution_id": str(shared)},
            reason="EXECUTION_ID_REUSED_ACROSS_SCENARIOS",
        ),
        Kpi05ScenarioEvidence(
            scenario_id="condition_branch",
            execution_id=shared,
            status="FAIL",
            success=False,
            execution_status=None,
            evidence_ids={"execution_id": str(shared)},
            reason="EXECUTION_ID_REUSED_ACROSS_SCENARIOS",
        ),
        Kpi05ScenarioEvidence(
            scenario_id="foreach_loop",
            execution_id=shared,
            status="FAIL",
            success=False,
            execution_status=None,
            evidence_ids={"execution_id": str(shared)},
            reason="EXECUTION_ID_REUSED_ACROSS_SCENARIOS",
        ),
        Kpi05ScenarioEvidence(
            scenario_id="while_loop",
            execution_id=shared,
            status="FAIL",
            success=False,
            execution_status=None,
            evidence_ids={"execution_id": str(shared)},
            reason="EXECUTION_ID_REUSED_ACROSS_SCENARIOS",
        ),
        Kpi05ScenarioEvidence(
            scenario_id="approval_wait_resume",
            execution_id=shared,
            status="FAIL",
            success=False,
            execution_status=None,
            evidence_ids={"execution_id": str(shared)},
            reason="EXECUTION_ID_REUSED_ACROSS_SCENARIOS",
        ),
    ]

    async def fake_with_session(factory):
        return results

    monkeypatch.setattr(
        "tests.evaluation.harness.live_evidence.with_session", fake_with_session
    )
    metric = measure_official_kpi_05(mode="live")
    assert metric["status"] == "MEASURED"
    assert metric["measured_value"] != 100.0
    assert metric["numerator"] == 1
    assert metric["denominator"] == 6
    assert any(
        s.get("reason") == "EXECUTION_ID_REUSED_ACROSS_SCENARIOS"
        for s in metric["scenarios"]
    )


def test_kpi05_succeeded_wrong_plan_shape_not_pass() -> None:
    from tests.evaluation.harness.live_evidence import plan_matches_scenario_kind

    succeeded_but_wrong = {
        "steps": [
            {"id": "t1", "type": "TOOL", "depends_on": []},
            {"id": "t2", "type": "TOOL", "depends_on": []},  # no chain
        ]
    }
    ok, reason = plan_matches_scenario_kind(succeeded_but_wrong, "sequential")
    assert ok is False
    assert reason == "SEQUENTIAL_DEPENDENCY_CHAIN_MISSING"
    # Same SUCCEEDED plan cannot satisfy all six kinds.
    kinds = ("sequential", "parallel", "condition", "foreach", "while", "approval")
    passes = sum(
        1 for kind in kinds if plan_matches_scenario_kind(succeeded_but_wrong, kind)[0]
    )
    assert passes < 6


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
