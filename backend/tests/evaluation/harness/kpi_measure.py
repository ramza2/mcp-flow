"""KPI measurement harness — offline diagnostics vs live official evidence.

docs/09 §25–27 / Issue #119 review: never present selector microbenchmarks or
frozen-rerank decision tests as official project KPI values.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Literal
from unittest.mock import MagicMock

from app.agent.tool_selector import ToolSelectorService
from app.domain.enums import RiskClass
from app.schemas.agent import SelectionSettings
from app.schemas.structured_request import StructuredRequestV1
from app.schemas.tool_selection import LLMRerankCandidate, LLMRerankResponse
from app.search.tool_retrieval import RetrievedToolCandidate, ToolCandidateDescriptor

from tests.evaluation.harness.common import (
    artifact_dir,
    environment_label,
    eval_root,
    existing_paths,
    git_sha,
    load_json,
    load_yaml,
    measured_at,
    percent,
    redact_secrets,
    run_pytest,
    write_json,
)

MetricStatus = Literal["MEASURED", "INCOMPLETE", "NOT_MEASURED"]
EvidenceMode = Literal["offline_regression", "live_official"]


def _targets() -> dict[str, Any]:
    return load_yaml(eval_root() / "targets.yaml")


def _db_url() -> str:
    return (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or ""
    ).strip()


def _env_present(name: str) -> bool:
    if name in {"TEST_DATABASE_URL", "DATABASE_URL"}:
        return bool(_db_url())
    return bool(os.environ.get(name, "").strip())


def _missing_env(names: list[str]) -> list[str]:
    return [name for name in names if not _env_present(name)]


def _official_metric(
    *,
    kpi_id: str,
    name: str,
    status: MetricStatus,
    evidence_mode: EvidenceMode,
    numerator: int | float | None,
    denominator: int | float | None,
    measured_value: float | int | None,
    unit: str,
    target: Any,
    evidence_complete: bool,
    reason: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kpi_id": kpi_id,
        "name": name,
        "status": status,
        "evidence_mode": evidence_mode,
        "numerator": numerator,
        "denominator": denominator,
        "measured_value": measured_value,
        "unit": unit,
        "target": target,
        "evidence_complete": evidence_complete,
        "reason": reason,
        "measured_at": measured_at(),
        "git_sha": git_sha(),
        "environment": environment_label(),
    }
    if extra:
        payload.update(extra)
    return payload


def _not_measured(
    *,
    kpi_id: str,
    name: str,
    evidence_mode: EvidenceMode,
    unit: str,
    target: Any,
    reason: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _official_metric(
        kpi_id=kpi_id,
        name=name,
        status="NOT_MEASURED",
        evidence_mode=evidence_mode,
        numerator=None,
        denominator=None,
        measured_value=None,
        unit=unit,
        target=target,
        evidence_complete=False,
        reason=reason,
        extra=extra,
    )


# ---------------------------------------------------------------------------
# Offline diagnostics (never official KPI values)
# ---------------------------------------------------------------------------


def _build_selection_inputs(
    case: dict[str, Any],
) -> tuple[StructuredRequestV1, list[RetrievedToolCandidate], LLMRerankResponse]:
    entities = list(case.get("entities") or [])
    structured = StructuredRequestV1.model_validate(
        {
            "schema_version": "1.0",
            "request_text": case["request_text"],
            "intent": case.get("intent") or "",
            "entities": entities,
            "constraints": [],
            "expected_outputs": [],
            "required_capabilities": [],
            "risk_hints": [],
            "missing_inputs": [],
            "ambiguities": [],
            "needs_clarification": False,
        }
    )
    name_to_id: dict[str, uuid.UUID] = {}
    candidates: list[RetrievedToolCandidate] = []
    for idx, raw in enumerate(case["candidates"]):
        tool_version_id = uuid.uuid4()
        name_to_id[raw["name"]] = tool_version_id
        descriptor = ToolCandidateDescriptor(
            tool_version_id=tool_version_id,
            name=raw["name"],
            description=raw.get("description"),
            tags=tuple(raw.get("tags") or ()),
            required_inputs=tuple(raw.get("required_inputs") or ()),
            optional_inputs=tuple(raw.get("optional_inputs") or ()),
            output_summary=raw.get("output_summary") or "",
            risk_class=RiskClass.READ_ONLY.value,
            retrieval_score=float(raw["retrieval_score"]),
        )
        candidates.append(
            RetrievedToolCandidate(
                mcp_tool_id=uuid.uuid4(),
                descriptor=descriptor,
                lexical_rank=idx + 1,
                vector_rank=idx + 1,
                rrf_raw=float(raw["retrieval_score"]),
                agent_requires_confirmation=False,
                parameter_constraints=None,
                policy_present=True,
                tool_policy_id=uuid.uuid4(),
                tool_policy_lock_version=1,
                policy_requires_confirmation=False,
                policy_requires_approval=False,
                approval_policy_id=None,
                allow_auto_select=bool(raw.get("allow_auto_select", True)),
            )
        )
    rerank = LLMRerankResponse(
        candidates=[
            LLMRerankCandidate(
                tool_version_id=name_to_id[item["name"]],
                llm_fit_score=float(item["llm_fit_score"]),
                reason_summary=item.get("reason_summary") or "",
            )
            for item in case["frozen_rerank"]
        ]
    )
    return structured, candidates, rerank


def diagnostic_selector_decide_timing() -> dict[str, Any]:
    """Offline diagnostic only — not official KPI-01 response_time."""
    dataset = load_json(eval_root() / "tool-mapping" / "dataset.v1.json")
    case = dataset["cases"][0]
    settings = SelectionSettings.model_validate(dataset["selection_settings"])
    t0 = time.perf_counter()
    structured, candidates, rerank = _build_selection_inputs(case)
    t_build = time.perf_counter()
    service = ToolSelectorService(MagicMock())
    outcome = service._decide(
        structured=structured,
        prompt_candidates=candidates,
        rerank=rerank,
        selection_settings=settings,
        existing_missing=[],
    )
    t_decide = time.perf_counter()
    segments = {
        "fixture_build_ms": round((t_build - t0) * 1000.0, 3),
        "selector_decide_ms": round((t_decide - t_build) * 1000.0, 3),
        "total_diagnostic_ms": round((t_decide - t0) * 1000.0, 3),
    }
    return {
        "diagnostic_id": "selector_decide_microbenchmark",
        "name": "tool_selector_decide_timing_ms",
        "evidence_mode": "offline_regression",
        "measured_value": segments["total_diagnostic_ms"],
        "unit": "milliseconds",
        "segments": segments,
        "fixture_case_id": case["id"],
        "dataset_version": dataset["dataset_version"],
        "decision": outcome.decision,
        "notes": (
            "Diagnostic only. Times ToolSelector._decide with MagicMock; "
            "excludes HTTP/analyzer/retrieval/rerank/plan/execution. "
            "Must not be used as official KPI-01 response_time."
        ),
        "measured_at": measured_at(),
        "git_sha": git_sha(),
        "environment": environment_label(),
    }


def diagnostic_selector_decision_regression() -> dict[str, Any]:
    """Frozen StructuredRequest/candidates/rerank → _decide regression."""
    dataset = load_json(eval_root() / "tool-mapping" / "dataset.v1.json")
    settings = SelectionSettings.model_validate(dataset["selection_settings"])
    service = ToolSelectorService(MagicMock())
    valid = 0
    correct = 0
    case_results: list[dict[str, Any]] = []
    for case in dataset["cases"]:
        valid_for_top1 = case.get("valid_for_top1", True)
        structured, candidates, rerank = _build_selection_inputs(case)
        outcome = service._decide(
            structured=structured,
            prompt_candidates=candidates,
            rerank=rerank,
            selection_settings=settings,
            existing_missing=[],
        )
        selected_name = (
            outcome.selected_candidate.descriptor.name
            if outcome.selected_candidate is not None
            else None
        )
        match_mode = case.get("top1_match_mode", "tool_name")
        if match_mode == "decision":
            is_correct = outcome.decision == case.get("expected_decision")
        else:
            is_correct = selected_name == case.get("expected_tool_name") and (
                case.get("expected_decision") is None
                or outcome.decision == case.get("expected_decision")
            )
        if valid_for_top1:
            valid += 1
            if is_correct:
                correct += 1
        case_results.append(
            {
                "id": case["id"],
                "valid_for_top1": valid_for_top1,
                "expected_tool_name": case.get("expected_tool_name"),
                "expected_decision": case.get("expected_decision"),
                "selected_tool_name": selected_name,
                "decision": outcome.decision,
                "correct": is_correct if valid_for_top1 else None,
            }
        )
    return {
        "diagnostic_id": "selector_decision_regression",
        "name": "frozen_selector_decision_top1_accuracy",
        "evidence_mode": "offline_regression",
        "numerator": correct,
        "denominator": valid,
        "measured_value": percent(correct, valid),
        "unit": "percent",
        "dataset_version": dataset["dataset_version"],
        "cases": case_results,
        "notes": (
            "Selector-decision regression with frozen StructuredRequest, "
            "candidates, and frozen_rerank. Not official KPI-02 NL→Tool accuracy."
        ),
        "measured_at": measured_at(),
        "git_sha": git_sha(),
        "environment": environment_label(),
    }


def _run_fixed_denominator_pytest_groups(
    groups: list[dict[str, Any]],
    *,
    path_key: str = "pytest",
    include_optional_integration: bool,
) -> dict[str, Any]:
    """Fixed denominator = declared groups. Missing required paths → NOT_RUN."""
    declared = len(groups)
    passed = 0
    failed = 0
    not_run = 0
    details: list[dict[str, Any]] = []

    for group in groups:
        required_paths = list(group.get(path_key) or [])
        optional_paths = list(group.get("optional_integration") or [])
        extra = list(group.get("pytest_args") or [])
        missing_required = [p for p in required_paths if p not in existing_paths(required_paths)]
        present_required = existing_paths(required_paths)

        if missing_required or not present_required:
            not_run += 1
            details.append(
                {
                    "id": group["id"],
                    "title": group.get("title"),
                    "status": "NOT_RUN",
                    "reason": "MISSING_REQUIRED_PATH",
                    "missing_required_paths": missing_required or required_paths,
                    "paths": present_required,
                }
            )
            continue

        unitish = [p for p in present_required if "/integration/" not in p]
        integ_required = [p for p in present_required if "/integration/" in p]
        optional_present = (
            existing_paths(optional_paths) if include_optional_integration else []
        )

        ok = True
        ran_any = False
        if unitish:
            proc = run_pytest(unitish, extra_args=extra, integration=False)
            ok = ok and proc.returncode == 0
            ran_any = True
        if integ_required:
            if not include_optional_integration and not _db_url():
                not_run += 1
                details.append(
                    {
                        "id": group["id"],
                        "title": group.get("title"),
                        "status": "NOT_RUN",
                        "reason": "INTEGRATION_REQUIRED_BUT_DISABLED",
                        "paths": present_required,
                    }
                )
                continue
            proc = run_pytest(integ_required, extra_args=extra, integration=True)
            ok = ok and proc.returncode == 0
            ran_any = True
        if optional_present:
            proc = run_pytest(optional_present, extra_args=extra, integration=True)
            # Optional integration failure does not mark NOT_RUN; records separately.
            details_optional_ok = proc.returncode == 0
        else:
            details_optional_ok = None

        if not ran_any:
            not_run += 1
            details.append(
                {
                    "id": group["id"],
                    "title": group.get("title"),
                    "status": "NOT_RUN",
                    "reason": "NO_EXECUTABLE_LAYER",
                    "paths": present_required,
                }
            )
            continue

        if ok:
            passed += 1
            status = "PASS"
        else:
            failed += 1
            status = "FAIL"
        details.append(
            {
                "id": group["id"],
                "title": group.get("title"),
                "status": status,
                "paths": present_required,
                "optional_integration_ok": details_optional_ok,
            }
        )

    evidence_complete = not_run == 0
    return {
        "declared": declared,
        "passed": passed,
        "failed": failed,
        "not_run": not_run,
        "evidence_complete": evidence_complete,
        "details": details,
    }


def diagnostic_registration_pytest_health() -> dict[str, Any]:
    cases_doc = load_yaml(eval_root() / "registration" / "cases.yaml")
    result = _run_fixed_denominator_pytest_groups(
        list(cases_doc.get("cases") or []),
        include_optional_integration=bool(_db_url()),
    )
    return {
        "diagnostic_id": "registration_pytest_group_health",
        "name": "registration_pytest_pass_rate",
        "evidence_mode": "offline_regression",
        "numerator": result["passed"],
        "denominator": result["declared"],
        "measured_value": (
            percent(result["passed"], result["declared"])
            if result["evidence_complete"]
            else None
        ),
        "evidence_complete": result["evidence_complete"],
        "not_run": result["not_run"],
        "failed": result["failed"],
        "cases_version": cases_doc.get("cases_version"),
        "cases": result["details"],
        "notes": (
            "Pytest group health only. Not official KPI-04 connection/registration "
            "attempt success rate. Fixed denominator = declared cases."
        ),
        "measured_at": measured_at(),
        "git_sha": git_sha(),
        "environment": environment_label(),
    }


def diagnostic_composite_pytest_health() -> dict[str, Any]:
    cases_doc = load_yaml(eval_root() / "workflow-scenarios" / "scenarios.yaml")
    result = _run_fixed_denominator_pytest_groups(
        list(cases_doc.get("scenarios") or []),
        include_optional_integration=bool(_db_url()),
    )
    return {
        "diagnostic_id": "composite_pytest_module_health",
        "name": "workflow_pytest_module_pass_rate",
        "evidence_mode": "offline_regression",
        "numerator": result["passed"],
        "denominator": result["declared"],
        "measured_value": (
            percent(result["passed"], result["declared"])
            if result["evidence_complete"]
            else None
        ),
        "evidence_complete": result["evidence_complete"],
        "not_run": result["not_run"],
        "failed": result["failed"],
        "scenarios_version": cases_doc.get("scenarios_version"),
        "scenarios": result["details"],
        "notes": (
            "Whole pytest-module health. Not official KPI-05 (one attempt per "
            "executable composite scenario). Fixed denominator = declared scenarios."
        ),
        "measured_at": measured_at(),
        "git_sha": git_sha(),
        "environment": environment_label(),
    }


# ---------------------------------------------------------------------------
# Official KPIs
# ---------------------------------------------------------------------------


def _live_agent_pins() -> dict[str, Any]:
    return {
        "dataset_version": os.environ.get("MCPFLOW_KPI_EVAL_DATASET_VERSION")
        or "tool-mapping.v1",
        "agent_version_id": os.environ.get("MCPFLOW_KPI_AGENT_VERSION_ID") or None,
        "llm_profile_id": os.environ.get("MCPFLOW_KPI_LLM_PROFILE_ID") or None,
        "embedding_profile_id": os.environ.get("MCPFLOW_KPI_EMBEDDING_PROFILE_ID")
        or None,
        "tool_inventory_snapshot_id": os.environ.get(
            "MCPFLOW_KPI_TOOL_INVENTORY_SNAPSHOT_ID"
        )
        or None,
        "git_sha": git_sha(),
        "environment": environment_label(),
    }


def _live_prereq_missing() -> list[str]:
    missing: list[str] = []
    if not _db_url():
        missing.append("TEST_DATABASE_URL|DATABASE_URL")
    for key in (
        "MCPFLOW_KPI_AGENT_VERSION_ID",
        "MCPFLOW_KPI_LLM_PROFILE_ID",
        "MCPFLOW_KPI_EMBEDDING_PROFILE_ID",
    ):
        if not _env_present(key):
            missing.append(key)
    return missing


def measure_official_kpi_01(*, mode: str) -> dict[str, Any]:
    import asyncio

    from tests.evaluation.harness.live_evidence import (
        load_kpi_01_evidence,
        parse_uuid,
        with_session,
    )

    targets = _targets()["targets"]["KPI-01"]
    unit = targets.get("measured_value_unit", "milliseconds")
    target = targets.get("target")
    timing_window = (
        "t_start=agent_requests.created_at; "
        "t_end=tool_selection_runs.created_at (latest for request)"
    )
    if mode != "live":
        return _not_measured(
            kpi_id="KPI-01",
            name="response_time",
            evidence_mode="offline_regression",
            unit=unit,
            target=target,
            reason=(
                "OFFLINE_MODE: official response_time requires durable "
                "AgentRequest + ToolSelectionRun evidence. "
                "Selector microbenchmark is offline diagnostic only."
            ),
            extra={"timing_window": timing_window},
        )
    missing = _live_prereq_missing()
    request_id = parse_uuid(os.environ.get("MCPFLOW_KPI_AGENT_REQUEST_ID"))
    if missing:
        return _not_measured(
            kpi_id="KPI-01",
            name="response_time",
            evidence_mode="live_official",
            unit=unit,
            target=target,
            reason=f"LIVE_PREREQUISITES_MISSING: {', '.join(missing)}",
            extra={"timing_window": timing_window, "pins": _live_agent_pins()},
        )
    if request_id is None:
        return _not_measured(
            kpi_id="KPI-01",
            name="response_time",
            evidence_mode="live_official",
            unit=unit,
            target=target,
            reason=(
                "DURABLE_EVIDENCE_SELECTOR_MISSING: set MCPFLOW_KPI_AGENT_REQUEST_ID "
                "to a persisted AgentRequest. Naked MCPFLOW_KPI_01_LIVE_RESULT_MS "
                "is ignored and cannot produce MEASURED."
            ),
            extra={"timing_window": timing_window, "pins": _live_agent_pins()},
        )
    try:
        evidence = asyncio.run(
            with_session(lambda session: load_kpi_01_evidence(session, agent_request_id=request_id))
        )
    except Exception as exc:  # noqa: BLE001
        return _not_measured(
            kpi_id="KPI-01",
            name="response_time",
            evidence_mode="live_official",
            unit=unit,
            target=target,
            reason=f"DURABLE_EVIDENCE_READ_ERROR: {type(exc).__name__}",
            extra={"timing_window": timing_window, "pins": _live_agent_pins()},
        )
    if evidence is None:
        return _not_measured(
            kpi_id="KPI-01",
            name="response_time",
            evidence_mode="live_official",
            unit=unit,
            target=target,
            reason=(
                "DURABLE_EVIDENCE_NOT_FOUND: AgentRequest or ToolSelectionRun "
                f"missing for {request_id}"
            ),
            extra={
                "timing_window": timing_window,
                "pins": _live_agent_pins(),
                "evidence_ids": {"agent_request_id": str(request_id)},
            },
        )
    if not evidence.lineage_ok:
        return _official_metric(
            kpi_id="KPI-01",
            name="response_time",
            status="INCOMPLETE",
            evidence_mode="live_official",
            numerator=None,
            denominator=None,
            measured_value=None,
            unit=unit,
            target=target,
            evidence_complete=False,
            reason="LINEAGE_MISMATCH:" + ",".join(evidence.lineage_reasons),
            extra={
                "timing_window": timing_window,
                "pins": _live_agent_pins(),
                "evidence_ids": evidence.evidence_ids,
                "lineage_reasons": list(evidence.lineage_reasons),
            },
        )
    return _official_metric(
        kpi_id="KPI-01",
        name="response_time",
        status="MEASURED",
        evidence_mode="live_official",
        numerator=None,
        denominator=None,
        measured_value=evidence.duration_ms,
        unit=unit,
        target=target,
        evidence_complete=True,
        reason="DURABLE_AGENTREQUEST_SELECTION_TIMESTAMPS",
        extra={
            "timing_window": timing_window,
            "pins": _live_agent_pins(),
            "evidence_ids": evidence.evidence_ids,
        },
    )


def measure_official_kpi_02(*, mode: str) -> dict[str, Any]:
    import asyncio
    from pathlib import Path

    from tests.evaluation.harness.live_evidence import (
        evaluate_kpi_02_cases,
        load_canonical_kpi_02_cases,
        load_json_path,
        parse_kpi_02_selector_map,
        with_session,
    )

    targets = _targets()["targets"]["KPI-02"]
    unit = targets.get("measured_value_unit", "percent")
    target = targets.get("target")
    dataset = load_json(eval_root() / "tool-mapping" / "dataset.v1.json")
    dataset_cases = load_canonical_kpi_02_cases(dataset)
    denominator = len(dataset_cases)

    if mode != "live":
        return _not_measured(
            kpi_id="KPI-02",
            name="nl_to_tool_mapping_top1_accuracy",
            evidence_mode="offline_regression",
            unit=unit,
            target=target,
            reason=(
                "OFFLINE_MODE: official Top-1 accuracy requires durable "
                "AgentRequest/ToolSelectionRun evidence per case. "
                "Frozen rerank/_decide is offline diagnostic only."
            ),
            extra={
                "counting_rule": targets.get("counting_rule"),
                "denominator_declared": denominator,
                "dataset_version": dataset.get("dataset_version"),
            },
        )
    missing = _live_prereq_missing()
    if missing:
        return _not_measured(
            kpi_id="KPI-02",
            name="nl_to_tool_mapping_top1_accuracy",
            evidence_mode="live_official",
            unit=unit,
            target=target,
            reason=f"LIVE_PREREQUISITES_MISSING: {', '.join(missing)}",
            extra={
                "counting_rule": targets.get("counting_rule"),
                "pins": _live_agent_pins(),
                "denominator_declared": denominator,
            },
        )
    map_path = os.environ.get("MCPFLOW_KPI_02_EVIDENCE_MAP", "").strip()
    if not map_path:
        return _not_measured(
            kpi_id="KPI-02",
            name="nl_to_tool_mapping_top1_accuracy",
            evidence_mode="live_official",
            unit=unit,
            target=target,
            reason=(
                "DURABLE_EVIDENCE_SELECTOR_MISSING: set MCPFLOW_KPI_02_EVIDENCE_MAP "
                "to a selector-only JSON map {cases:[{case_id, agent_request_id}]}. "
                "Expected labels come only from tool-mapping/dataset.v1.json. "
                "Naked MCPFLOW_KPI_02_LIVE_NUMERATOR/DENOMINATOR are ignored."
            ),
            extra={
                "counting_rule": targets.get("counting_rule"),
                "pins": _live_agent_pins(),
                "denominator_declared": denominator,
            },
        )
    path = Path(map_path)
    if not path.is_file():
        return _not_measured(
            kpi_id="KPI-02",
            name="nl_to_tool_mapping_top1_accuracy",
            evidence_mode="live_official",
            unit=unit,
            target=target,
            reason=f"EVIDENCE_MAP_NOT_FOUND: {map_path}",
            extra={
                "pins": _live_agent_pins(),
                "denominator_declared": denominator,
            },
        )
    try:
        payload = load_json_path(path)
    except Exception as exc:  # noqa: BLE001
        return _not_measured(
            kpi_id="KPI-02",
            name="nl_to_tool_mapping_top1_accuracy",
            evidence_mode="live_official",
            unit=unit,
            target=target,
            reason=f"EVIDENCE_MAP_PARSE_ERROR: {type(exc).__name__}",
            extra={"pins": _live_agent_pins(), "denominator_declared": denominator},
        )
    if not isinstance(payload, dict):
        return _official_metric(
            kpi_id="KPI-02",
            name="nl_to_tool_mapping_top1_accuracy",
            status="INCOMPLETE",
            evidence_mode="live_official",
            numerator=0,
            denominator=denominator,
            measured_value=None,
            unit=unit,
            target=target,
            evidence_complete=False,
            reason="EVIDENCE_MAP_MUST_BE_OBJECT",
            extra={"pins": _live_agent_pins(), "denominator_declared": denominator},
        )
    selectors, map_errors = parse_kpi_02_selector_map(payload)
    eligible_ids = {str(case["id"]) for case in dataset_cases}
    unknown = sorted(set(selectors) - eligible_ids)
    if unknown:
        map_errors.append("UNKNOWN_CASE_ID:" + ",".join(unknown))
    if map_errors:
        return _official_metric(
            kpi_id="KPI-02",
            name="nl_to_tool_mapping_top1_accuracy",
            status="INCOMPLETE",
            evidence_mode="live_official",
            numerator=0,
            denominator=denominator,
            measured_value=None,
            unit=unit,
            target=target,
            evidence_complete=False,
            reason="EVIDENCE_MAP_REJECTED:" + ";".join(map_errors),
            extra={
                "counting_rule": targets.get("counting_rule"),
                "pins": _live_agent_pins(),
                "denominator_declared": denominator,
                "dataset_version": dataset.get("dataset_version"),
                "evidence_map": map_path,
                "map_errors": map_errors,
            },
        )
    if denominator == 0:
        return _official_metric(
            kpi_id="KPI-02",
            name="nl_to_tool_mapping_top1_accuracy",
            status="INCOMPLETE",
            evidence_mode="live_official",
            numerator=0,
            denominator=0,
            measured_value=None,
            unit=unit,
            target=target,
            evidence_complete=False,
            reason="DENOMINATOR_ZERO",
            extra={"pins": _live_agent_pins()},
        )
    try:
        evaluated = asyncio.run(
            with_session(
                lambda session: evaluate_kpi_02_cases(
                    session,
                    dataset_cases=dataset_cases,
                    selectors=selectors,
                )
            )
        )
    except Exception as exc:  # noqa: BLE001
        return _not_measured(
            kpi_id="KPI-02",
            name="nl_to_tool_mapping_top1_accuracy",
            evidence_mode="live_official",
            unit=unit,
            target=target,
            reason=f"DURABLE_EVIDENCE_READ_ERROR: {type(exc).__name__}",
            extra={"pins": _live_agent_pins(), "denominator_declared": denominator},
        )
    not_run = sum(1 for item in evaluated if item.status == "NOT_RUN")
    numerator = sum(1 for item in evaluated if item.status == "PASS")
    case_details = [
        {
            "case_id": item.case_id,
            "status": item.status,
            "correct": item.correct,
            "decision": item.decision,
            "expected_tool_name": item.expected_tool_name,
            "expected_decision": item.expected_decision,
            "selected_tool_name": item.selected_tool_name,
            "evidence_ids": item.evidence_ids,
            "reason": item.reason,
        }
        for item in evaluated
    ]
    if not_run > 0:
        return _official_metric(
            kpi_id="KPI-02",
            name="nl_to_tool_mapping_top1_accuracy",
            status="INCOMPLETE",
            evidence_mode="live_official",
            numerator=numerator,
            denominator=denominator,
            measured_value=None,
            unit=unit,
            target=target,
            evidence_complete=False,
            reason="REQUIRED_CASES_NOT_RUN",
            extra={
                "counting_rule": targets.get("counting_rule"),
                "pins": _live_agent_pins(),
                "cases": case_details,
                "evidence_map": map_path,
                "dataset_version": dataset.get("dataset_version"),
            },
        )
    return _official_metric(
        kpi_id="KPI-02",
        name="nl_to_tool_mapping_top1_accuracy",
        status="MEASURED",
        evidence_mode="live_official",
        numerator=numerator,
        denominator=denominator,
        measured_value=percent(numerator, denominator),
        unit=unit,
        target=target,
        evidence_complete=True,
        reason="DURABLE_TOOL_SELECTION_RUN_CASE_SCORES",
        extra={
            "counting_rule": targets.get("counting_rule"),
            "pins": _live_agent_pins(),
            "cases": case_details,
            "evidence_map": map_path,
            "dataset_version": dataset.get("dataset_version"),
        },
    )


async def _count_verified_tools_async() -> dict[str, Any]:
    from datetime import UTC, datetime

    from app.domain.enums import ToolVerificationStatus
    from app.models.factory import ToolFactoryPublish
    from app.models.mcp import MCPTool, MCPToolVerification
    from app.repositories.mcp_tool_verification import MCPToolVerificationRepository
    from sqlalchemy import and_, func, or_, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    db_url = _db_url()
    if not db_url:
        return {
            "status": "UNAVAILABLE",
            "reason": "NO_DATABASE_URL",
            "count": None,
            "breakdown": None,
        }

    engine = create_async_engine(db_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            total = await MCPToolVerificationRepository(
                session
            ).count_tools_with_effective_current_verification()
            as_of = datetime.now(UTC)
            verified_join = and_(
                MCPToolVerification.mcp_tool_version_id == MCPTool.current_version_id,
                MCPToolVerification.status == ToolVerificationStatus.VERIFIED,
                or_(
                    MCPToolVerification.expires_at.is_(None),
                    MCPToolVerification.expires_at > as_of,
                ),
            )
            base_where = and_(
                MCPTool.deleted_at.is_(None),
                MCPTool.current_version_id.is_not(None),
            )
            internal_stmt = (
                select(func.count(func.distinct(MCPTool.id)))
                .select_from(MCPTool)
                .join(MCPToolVerification, verified_join)
                .join(
                    ToolFactoryPublish,
                    ToolFactoryPublish.mcp_server_id == MCPTool.mcp_server_id,
                )
                .where(base_where)
            )
            external_stmt = (
                select(func.count(func.distinct(MCPTool.id)))
                .select_from(MCPTool)
                .join(MCPToolVerification, verified_join)
                .outerjoin(
                    ToolFactoryPublish,
                    ToolFactoryPublish.mcp_server_id == MCPTool.mcp_server_id,
                )
                .where(base_where, ToolFactoryPublish.id.is_(None))
            )
            internal = int((await session.execute(internal_stmt)).scalar_one())
            external = int((await session.execute(external_stmt)).scalar_one())
            return {
                "status": "OK",
                "reason": None,
                "count": total,
                "breakdown": {
                    "internal_factory_published": internal,
                    "external_or_manual": external,
                },
            }
    finally:
        await engine.dispose()


def measure_official_kpi_03(*, mode: str) -> dict[str, Any]:
    import asyncio

    targets = _targets()["targets"]["KPI-03"]
    target_min = targets.get("target_min")
    evidence_mode: EvidenceMode = (
        "live_official" if mode == "live" else "offline_regression"
    )
    try:
        inventory = asyncio.run(_count_verified_tools_async())
    except Exception as exc:  # noqa: BLE001
        inventory = {
            "status": "ERROR",
            "reason": type(exc).__name__,
            "count": None,
            "breakdown": None,
        }
    count = inventory.get("count")
    if inventory.get("status") != "OK" or not isinstance(count, int):
        return _not_measured(
            kpi_id="KPI-03",
            name="verified_mcp_tool_count",
            evidence_mode=evidence_mode,
            unit=targets.get("measured_value_unit", "count"),
            target=targets.get("target"),
            reason=f"INVENTORY_UNAVAILABLE: {inventory.get('reason')}",
            extra={
                "target_min": target_min,
                "inventory_status": inventory.get("status"),
                "counting_rule": targets.get("counting_rule"),
            },
        )
    gap = max(target_min - count, 0) if isinstance(target_min, int) else None
    return _official_metric(
        kpi_id="KPI-03",
        name="verified_mcp_tool_count",
        status="MEASURED",
        evidence_mode=evidence_mode,
        numerator=count,
        denominator=None,
        measured_value=count,
        unit=targets.get("measured_value_unit", "count"),
        target=targets.get("target"),
        evidence_complete=True,
        reason="VERIFIED_CURRENT_TOOLVERSION_AGGREGATE",
        extra={
            "target_min": target_min,
            "gap_to_target_min": gap,
            "meets_target_min": (
                isinstance(target_min, int) and count >= target_min
            ),
            "breakdown": inventory.get("breakdown"),
            "counting_rule": targets.get("counting_rule"),
            "notes": (
                "Distinct Tools with effective VERIFIED current ToolVersion. "
                "Does not fabricate Tools when below target_min=10."
            ),
        },
    )


def measure_official_kpi_04(*, mode: str) -> dict[str, Any]:
    import asyncio

    from tests.evaluation.harness.live_evidence import (
        evaluate_kpi_04_attempts,
        parse_uuid,
        with_session,
    )

    targets = _targets()["targets"]["KPI-04"]
    unit = targets.get("measured_value_unit", "percent")
    target = targets.get("target")
    attempts_doc = load_yaml(eval_root() / "registration" / "attempts.v1.yaml")
    attempts = list(attempts_doc.get("attempts") or [])
    denominator = len(attempts)

    if mode != "live":
        return _not_measured(
            kpi_id="KPI-04",
            name="mcp_connection_registration_success_rate",
            evidence_mode="offline_regression",
            unit=unit,
            target=target,
            reason=(
                "OFFLINE_MODE: official KPI-04 requires durable smoke "
                "check/discovery/registry/verification evidence "
                f"(denominator={denominator} declared). Pytest group health is "
                "offline diagnostic only."
            ),
            extra={
                "denominator_declared": denominator,
                "attempts_version": attempts_doc.get("attempts_version"),
            },
        )

    smoke_id = parse_uuid(os.environ.get("MCPFLOW_KPI_SMOKE_MCP_SERVER_ID"))
    if not _db_url() or smoke_id is None:
        return _official_metric(
            kpi_id="KPI-04",
            name="mcp_connection_registration_success_rate",
            status="INCOMPLETE",
            evidence_mode="live_official",
            numerator=0,
            denominator=denominator,
            measured_value=None,
            unit=unit,
            target=target,
            evidence_complete=False,
            reason=(
                "DURABLE_EVIDENCE_SELECTOR_MISSING: require DATABASE_URL/"
                "TEST_DATABASE_URL and MCPFLOW_KPI_SMOKE_MCP_SERVER_ID. "
                "Naked MCPFLOW_KPI_04_RESULT_* pass flags are ignored."
            ),
            extra={
                "attempts_version": attempts_doc.get("attempts_version"),
                "attempts": [
                    {
                        "attempt_id": a["attempt_id"],
                        "operation": a.get("operation"),
                        "status": "NOT_RUN",
                        "success": False,
                        "failure_category": "PREREQUISITE_MISSING",
                    }
                    for a in attempts
                ],
            },
        )

    check_id = parse_uuid(os.environ.get("MCPFLOW_KPI_SMOKE_CHECK_ID"))
    discovery_id = parse_uuid(os.environ.get("MCPFLOW_KPI_SMOKE_DISCOVERY_ID"))
    verification_id = parse_uuid(os.environ.get("MCPFLOW_KPI_SMOKE_VERIFICATION_ID"))
    try:
        evaluated = asyncio.run(
            with_session(
                lambda session: evaluate_kpi_04_attempts(
                    session,
                    smoke_server_id=smoke_id,
                    check_id=check_id,
                    discovery_id=discovery_id,
                    verification_id=verification_id,
                )
            )
        )
    except Exception as exc:  # noqa: BLE001
        return _not_measured(
            kpi_id="KPI-04",
            name="mcp_connection_registration_success_rate",
            evidence_mode="live_official",
            unit=unit,
            target=target,
            reason=f"DURABLE_EVIDENCE_READ_ERROR: {type(exc).__name__}",
            extra={"attempts_version": attempts_doc.get("attempts_version")},
        )

    details = [
        {
            "attempt_id": item.attempt_id,
            "operation": item.operation,
            "success": item.success,
            "status": item.status,
            "failure_category": item.failure_category,
            "evidence_ids": item.evidence_ids,
        }
        for item in evaluated
    ]
    passed = sum(1 for item in evaluated if item.status == "PASS")
    not_run = sum(1 for item in evaluated if item.status == "NOT_RUN")
    if not_run > 0:
        return _official_metric(
            kpi_id="KPI-04",
            name="mcp_connection_registration_success_rate",
            status="INCOMPLETE",
            evidence_mode="live_official",
            numerator=passed,
            denominator=denominator,
            measured_value=None,
            unit=unit,
            target=target,
            evidence_complete=False,
            reason="REQUIRED_ATTEMPTS_NOT_RUN",
            extra={
                "attempts_version": attempts_doc.get("attempts_version"),
                "attempts": details,
            },
        )
    return _official_metric(
        kpi_id="KPI-04",
        name="mcp_connection_registration_success_rate",
        status="MEASURED",
        evidence_mode="live_official",
        numerator=passed,
        denominator=denominator,
        measured_value=percent(passed, denominator),
        unit=unit,
        target=target,
        evidence_complete=True,
        reason="DURABLE_SMOKE_MCP_EVIDENCE",
        extra={
            "attempts_version": attempts_doc.get("attempts_version"),
            "attempts": details,
        },
    )


def measure_official_kpi_05(*, mode: str) -> dict[str, Any]:
    import asyncio

    from tests.evaluation.harness.live_evidence import (
        evaluate_kpi_05_scenarios,
        with_session,
    )

    targets = _targets()["targets"]["KPI-05"]
    unit = targets.get("measured_value_unit", "percent")
    target = targets.get("target")
    exec_doc = load_yaml(eval_root() / "workflow-scenarios" / "executable.v1.yaml")
    scenarios = list(exec_doc.get("scenarios") or [])
    denominator = len(scenarios)

    if mode != "live":
        return _not_measured(
            kpi_id="KPI-05",
            name="composite_scenario_completion_rate",
            evidence_mode="offline_regression",
            unit=unit,
            target=target,
            reason=(
                "OFFLINE_MODE: official KPI-05 requires durable Execution IDs "
                f"per scenario_id (declared={denominator}). Whole unit pytest "
                "modules are offline diagnostic only."
            ),
            extra={
                "denominator_declared": denominator,
                "scenarios_version": exec_doc.get("scenarios_version"),
            },
        )

    if not _db_url():
        return _official_metric(
            kpi_id="KPI-05",
            name="composite_scenario_completion_rate",
            status="INCOMPLETE",
            evidence_mode="live_official",
            numerator=0,
            denominator=denominator,
            measured_value=None,
            unit=unit,
            target=target,
            evidence_complete=False,
            reason=(
                "DURABLE_EVIDENCE_SELECTOR_MISSING: require DATABASE_URL/"
                "TEST_DATABASE_URL and MCPFLOW_KPI_EXECUTION_ID_<SCENARIO_ID>. "
                "Naked MCPFLOW_KPI_05_RESULT_* pass flags are ignored."
            ),
            extra={
                "scenarios_version": exec_doc.get("scenarios_version"),
                "scenarios": [
                    {
                        "scenario_id": s["scenario_id"],
                        "status": "NOT_RUN",
                        "success": False,
                        "reason": "NO_DATABASE_URL",
                    }
                    for s in scenarios
                ],
            },
        )

    try:
        evaluated = asyncio.run(
            with_session(lambda session: evaluate_kpi_05_scenarios(session, scenarios))
        )
    except Exception as exc:  # noqa: BLE001
        return _not_measured(
            kpi_id="KPI-05",
            name="composite_scenario_completion_rate",
            evidence_mode="live_official",
            unit=unit,
            target=target,
            reason=f"DURABLE_EVIDENCE_READ_ERROR: {type(exc).__name__}",
            extra={"scenarios_version": exec_doc.get("scenarios_version")},
        )

    details = [
        {
            "scenario_id": item.scenario_id,
            "status": item.status,
            "success": item.success,
            "execution_status": item.execution_status,
            "evidence_ids": item.evidence_ids,
            "reason": item.reason,
        }
        for item in evaluated
    ]
    passed = sum(1 for item in evaluated if item.status == "PASS")
    not_run = sum(1 for item in evaluated if item.status == "NOT_RUN")
    if not_run > 0:
        return _official_metric(
            kpi_id="KPI-05",
            name="composite_scenario_completion_rate",
            status="INCOMPLETE",
            evidence_mode="live_official",
            numerator=passed,
            denominator=denominator,
            measured_value=None,
            unit=unit,
            target=target,
            evidence_complete=False,
            reason="REQUIRED_SCENARIOS_NOT_RUN",
            extra={
                "scenarios_version": exec_doc.get("scenarios_version"),
                "scenarios": details,
                "pins": {
                    "agent_version_id": os.environ.get("MCPFLOW_KPI_AGENT_VERSION_ID"),
                    "workflow_version_id": os.environ.get(
                        "MCPFLOW_KPI_WORKFLOW_VERSION_ID"
                    ),
                },
            },
        )
    return _official_metric(
        kpi_id="KPI-05",
        name="composite_scenario_completion_rate",
        status="MEASURED",
        evidence_mode="live_official",
        numerator=passed,
        denominator=denominator,
        measured_value=percent(passed, denominator),
        unit=unit,
        target=target,
        evidence_complete=True,
        reason="DURABLE_EXECUTION_PLAN_SHAPE_AND_TERMINAL_EVIDENCE",
        extra={
            "scenarios_version": exec_doc.get("scenarios_version"),
            "scenarios": details,
            "pins": {
                "agent_version_id": os.environ.get("MCPFLOW_KPI_AGENT_VERSION_ID"),
                "workflow_version_id": os.environ.get(
                    "MCPFLOW_KPI_WORKFLOW_VERSION_ID"
                ),
            },
        },
    )


def measure_official_kpi_06(*, mode: str) -> dict[str, Any]:
    """Test-based official ops pass rate with fixed declared denominator."""
    targets = _targets()["targets"]["KPI-06"]
    cases_doc = load_yaml(eval_root() / "operation" / "cases.yaml")
    groups = list(cases_doc.get("cases") or [])
    required_categories = list(
        cases_doc.get("required_categories")
        or ["authorization", "approval", "audit", "cancellation", "recovery"]
    )
    result = _run_fixed_denominator_pytest_groups(
        groups,
        include_optional_integration=bool(_db_url()),
    )
    categories_present = sorted(
        {c.get("category") for c in groups if c.get("category")}
    )
    missing_required_cats = [
        cat for cat in required_categories if cat not in categories_present
    ]
    evidence_mode: EvidenceMode = (
        "live_official" if mode == "live" else "offline_regression"
    )
    denominator = result["declared"]
    numerator = result["passed"]

    if missing_required_cats or not result["evidence_complete"]:
        return _official_metric(
            kpi_id="KPI-06",
            name="operations_function_pass_rate",
            status="INCOMPLETE",
            evidence_mode=evidence_mode,
            numerator=numerator,
            denominator=denominator,
            measured_value=None,
            unit=targets.get("measured_value_unit", "percent"),
            target=targets.get("target"),
            evidence_complete=False,
            reason=(
                "REQUIRED_EVIDENCE_INCOMPLETE: "
                + (
                    f"missing_categories={missing_required_cats}; "
                    if missing_required_cats
                    else ""
                )
                + f"not_run={result['not_run']}"
            ),
            extra={
                "cases_version": cases_doc.get("cases_version"),
                "required_categories": required_categories,
                "categories_present": categories_present,
                "cases": result["details"],
                "failed": result["failed"],
                "not_run": result["not_run"],
                "counting_rule": (
                    "Passed declared ops cases / declared cases × 100; "
                    "NOT_RUN keeps denominator fixed; INCOMPLETE ⇒ measured_value null"
                ),
            },
        )

    return _official_metric(
        kpi_id="KPI-06",
        name="operations_function_pass_rate",
        status="MEASURED",
        evidence_mode=evidence_mode,
        numerator=numerator,
        denominator=denominator,
        measured_value=percent(numerator, denominator),
        unit=targets.get("measured_value_unit", "percent"),
        target=targets.get("target"),
        evidence_complete=True,
        reason="FIXED_DENOMINATOR_OPS_TESTS_COMPLETE",
        extra={
            "cases_version": cases_doc.get("cases_version"),
            "required_categories": required_categories,
            "categories_present": categories_present,
            "cases": result["details"],
            "failed": result["failed"],
            "not_run": result["not_run"],
            "counting_rule": (
                "Passed declared ops cases / declared cases × 100 "
                "(authorization, approval, audit, cancellation, recovery required)"
            ),
        },
    )


def collect_offline_diagnostics(*, include_heavy: bool = True) -> list[dict[str, Any]]:
    diagnostics = [
        diagnostic_selector_decide_timing(),
        diagnostic_selector_decision_regression(),
    ]
    if include_heavy:
        diagnostics.extend(
            [
                diagnostic_registration_pytest_health(),
                diagnostic_composite_pytest_health(),
            ]
        )
    return diagnostics


def measure_all(
    *,
    mode: str,
    include_diagnostics: bool = True,
    include_heavy_diagnostics: bool | None = None,
) -> dict[str, Any]:
    targets_doc = _targets()
    official = [
        measure_official_kpi_01(mode=mode),
        measure_official_kpi_02(mode=mode),
        measure_official_kpi_03(mode=mode),
        measure_official_kpi_04(mode=mode),
        measure_official_kpi_05(mode=mode),
        measure_official_kpi_06(mode=mode),
    ]
    report: dict[str, Any] = {
        "schema_version": "mcpflow.kpi_report.v2",
        "mode": mode,
        "dataset_bundle_version": targets_doc.get("dataset_bundle_version"),
        "measured_at": measured_at(),
        "git_sha": git_sha(),
        "environment": environment_label(),
        "official_metrics": official,
    }
    if include_diagnostics:
        heavy = (
            include_heavy_diagnostics
            if include_heavy_diagnostics is not None
            else (mode == "offline")
        )
        report["offline_diagnostics"] = collect_offline_diagnostics(
            include_heavy=heavy
        )
    return redact_secrets(report)


def print_human_summary(report: dict[str, Any]) -> None:
    print("=== MCPFlow KPI Measurement ===")
    print(f"mode           : {report.get('mode')}")
    print(f"dataset_bundle : {report.get('dataset_bundle_version')}")
    print(f"git_sha        : {report.get('git_sha')}")
    print(f"environment    : {report.get('environment')}")
    print(f"measured_at    : {report.get('measured_at')}")
    print("")
    print("--- official_metrics ---")
    for metric in report.get("official_metrics") or []:
        print(
            f"{metric['kpi_id']} {metric['name']}: status={metric['status']} "
            f"evidence_mode={metric['evidence_mode']} "
            f"value={metric.get('measured_value')} "
            f"num={metric.get('numerator')} den={metric.get('denominator')} "
            f"complete={metric.get('evidence_complete')} "
            f"reason={metric.get('reason')}"
        )
    diagnostics = report.get("offline_diagnostics") or []
    if diagnostics:
        print("")
        print("--- offline_diagnostics (not official KPI) ---")
        for item in diagnostics:
            print(
                f"{item.get('diagnostic_id')}: value={item.get('measured_value')} "
                f"num={item.get('numerator')} den={item.get('denominator')}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MCPFlow KPI measurement harness")
    parser.add_argument(
        "--mode",
        choices=("offline", "live"),
        default=os.environ.get("MCPFLOW_KPI_MODE", "offline"),
        help="offline=diagnostics + official NOT_MEASURED/INCOMPLETE where required; "
        "live=official evidence with pinned prerequisites",
    )
    parser.add_argument(
        "--out",
        default="",
        help="JSON output path (default: artifacts/kpi/latest.json)",
    )
    parser.add_argument(
        "--only",
        default="",
        help="Comma-separated KPI ids for official section (default: all)",
    )
    parser.add_argument(
        "--no-diagnostics",
        action="store_true",
        help="Omit offline_diagnostics section",
    )
    args = parser.parse_args(argv)
    mode = args.mode

    if args.only.strip():
        wanted = {part.strip().upper() for part in args.only.split(",") if part.strip()}
        mapping = {
            "KPI-01": lambda: measure_official_kpi_01(mode=mode),
            "KPI-02": lambda: measure_official_kpi_02(mode=mode),
            "KPI-03": lambda: measure_official_kpi_03(mode=mode),
            "KPI-04": lambda: measure_official_kpi_04(mode=mode),
            "KPI-05": lambda: measure_official_kpi_05(mode=mode),
            "KPI-06": lambda: measure_official_kpi_06(mode=mode),
        }
        official = [mapping[k]() for k in mapping if k in wanted]
        targets_doc = _targets()
        report = redact_secrets(
            {
                "schema_version": "mcpflow.kpi_report.v2",
                "mode": mode,
                "dataset_bundle_version": targets_doc.get("dataset_bundle_version"),
                "measured_at": measured_at(),
                "git_sha": git_sha(),
                "environment": environment_label(),
                "official_metrics": official,
                "offline_diagnostics": (
                    []
                    if args.no_diagnostics
                    else collect_offline_diagnostics(include_heavy=False)
                ),
            }
        )
    else:
        report = measure_all(
            mode=mode, include_diagnostics=not args.no_diagnostics
        )

    out_path = Path(args.out) if args.out else artifact_dir("kpi") / "latest.json"
    write_json(out_path, report)
    print_human_summary(report)
    print(f"\nJSON: {out_path}")

    # Live mode without any MEASURED official KPI should not look like success.
    if mode == "live":
        statuses = {m["status"] for m in report.get("official_metrics") or []}
        if "MEASURED" not in statuses:
            print(
                "\nNOTE: live mode produced no MEASURED official KPIs "
                f"(statuses={sorted(statuses)}); exit 2."
            )
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
