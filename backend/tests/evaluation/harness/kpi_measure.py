"""KPI measurement harness for the six project KPIs (docs/09 §25–27)."""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path
from typing import Any
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


def _targets() -> dict[str, Any]:
    return load_yaml(eval_root() / "targets.yaml")


def _metric_base(
    *,
    kpi_id: str,
    name: str,
    numerator: int | None,
    denominator: int | None,
    measured_value: float | int | None,
    unit: str,
    target: Any,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kpi_id": kpi_id,
        "name": name,
        "numerator": numerator,
        "denominator": denominator,
        "measured_value": measured_value,
        "unit": unit,
        "target": target,
        "measured_at": measured_at(),
        "git_sha": git_sha(),
        "environment": environment_label(),
    }
    if extra:
        payload.update(extra)
    return payload


def measure_kpi_01_response_time() -> dict[str, Any]:
    """Wall-clock segments for a representative frozen Tool selection decide path."""
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
        "request_accept_ms": round((t_build - t0) * 1000.0, 3),
        "planning_ms": round((t_decide - t_build) * 1000.0, 3),
        "total_response_ms": round((t_decide - t0) * 1000.0, 3),
    }
    # KPI-01 has no approved numeric target in-repo; report measurement only.
    targets = _targets()["targets"]["KPI-01"]
    return _metric_base(
        kpi_id="KPI-01",
        name="response_time",
        numerator=None,
        denominator=None,
        measured_value=segments["total_response_ms"],
        unit=targets.get("measured_value_unit", "milliseconds"),
        target=targets.get("target"),
        extra={
            "segments": segments,
            "fixture_case_id": case["id"],
            "dataset_version": dataset["dataset_version"],
            "notes": (
                "Representative frozen ToolSelector._decide path timing. "
                "Not an end-to-end HTTP SLA; NFR-PERF targets not configured in-repo."
            ),
            "decision": outcome.decision,
        },
    )


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


def measure_kpi_02_tool_mapping() -> dict[str, Any]:
    """Top-1 accuracy over frozen dataset (docs/09 §26)."""
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

    targets = _targets()["targets"]["KPI-02"]
    value = percent(correct, valid)
    return _metric_base(
        kpi_id="KPI-02",
        name="nl_to_tool_mapping_top1_accuracy",
        numerator=correct,
        denominator=valid,
        measured_value=value,
        unit=targets.get("measured_value_unit", "percent"),
        target=targets.get("target"),
        extra={
            "dataset_version": dataset["dataset_version"],
            "counting_rule": targets.get("counting_rule"),
            "cases": case_results,
            "notes": (
                "Top-1 accuracy = Correct Top-1 / Valid Evaluation Cases × 100. "
                "Frozen rerank pins model output (FNC-AGT-010). "
                "Cases with valid_for_top1=false are excluded from the denominator."
            ),
        },
    )


async def _count_verified_tools_async() -> dict[str, Any]:
    """Query live DB for KPI-03 when TEST_DATABASE_URL / DATABASE_URL is available."""
    import os

    from app.domain.enums import ToolVerificationStatus
    from app.models.factory import ToolFactoryPublish
    from app.models.mcp import MCPTool, MCPToolVerification
    from app.repositories.mcp_tool_verification import MCPToolVerificationRepository
    from sqlalchemy import and_, func, or_, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    db_url = (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or ""
    ).strip()
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

            # Breakdown: factory-published servers = internal; others = external/manual.
            from datetime import UTC, datetime

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


def measure_kpi_03_tool_count() -> dict[str, Any]:
    import asyncio

    targets = _targets()["targets"]["KPI-03"]
    target_min = targets.get("target_min")
    try:
        inventory = asyncio.run(_count_verified_tools_async())
    except Exception as exc:  # noqa: BLE001 — harness must not crash measurement
        inventory = {
            "status": "ERROR",
            "reason": type(exc).__name__,
            "count": None,
            "breakdown": None,
        }

    count = inventory.get("count")
    gap = None
    if isinstance(count, int) and isinstance(target_min, int):
        gap = max(target_min - count, 0)

    return _metric_base(
        kpi_id="KPI-03",
        name="verified_mcp_tool_count",
        numerator=count,
        denominator=None,
        measured_value=count,
        unit=targets.get("measured_value_unit", "count"),
        target=targets.get("target"),
        extra={
            "target_min": target_min,
            "gap_to_target_min": gap,
            "meets_target_min": (
                isinstance(count, int)
                and isinstance(target_min, int)
                and count >= target_min
            ),
            "inventory_status": inventory.get("status"),
            "inventory_reason": inventory.get("reason"),
            "breakdown": inventory.get("breakdown"),
            "counting_rule": targets.get("counting_rule"),
            "notes": (
                "Count distinct Tools with effective VERIFIED current ToolVersion. "
                "Does not fabricate Tools when below target_min=10."
            ),
        },
    )


def _run_case_pytest_groups(
    groups: list[dict[str, Any]],
    *,
    path_key: str = "pytest",
    include_integration: bool,
) -> tuple[int, int, list[dict[str, Any]]]:
    passed = 0
    attempted = 0
    details: list[dict[str, Any]] = []
    for group in groups:
        paths = list(group.get(path_key) or [])
        if include_integration:
            paths.extend(list(group.get("optional_integration") or []))
        present = existing_paths(paths)
        extra = list(group.get("pytest_args") or [])
        if not present:
            details.append(
                {
                    "id": group["id"],
                    "status": "SKIP",
                    "reason": "NO_PATHS_FOUND",
                    "paths": paths,
                }
            )
            continue
        # Prefer unit/api (not integration) unless only integration paths remain.
        unitish = [p for p in present if "/integration/" not in p]
        integ = [p for p in present if "/integration/" in p]
        ok = True
        if unitish:
            proc = run_pytest(unitish, extra_args=extra, integration=False)
            ok = ok and proc.returncode == 0
        if include_integration and integ:
            proc = run_pytest(integ, extra_args=extra, integration=True)
            ok = ok and proc.returncode == 0
        if not unitish and not (include_integration and integ):
            details.append(
                {
                    "id": group["id"],
                    "status": "SKIP",
                    "reason": "INTEGRATION_DISABLED",
                    "paths": present,
                }
            )
            continue
        attempted += 1
        if ok:
            passed += 1
        details.append(
            {
                "id": group["id"],
                "title": group.get("title"),
                "status": "PASS" if ok else "FAIL",
                "paths": present,
            }
        )
    return passed, attempted, details


def measure_kpi_04_registration() -> dict[str, Any]:
    import os

    cases_doc = load_yaml(eval_root() / "registration" / "cases.yaml")
    include_integration = bool(os.environ.get("TEST_DATABASE_URL", "").strip())
    passed, attempted, details = _run_case_pytest_groups(
        list(cases_doc.get("cases") or []),
        include_integration=include_integration,
    )
    targets = _targets()["targets"]["KPI-04"]
    value = percent(passed, attempted)
    return _metric_base(
        kpi_id="KPI-04",
        name="mcp_connection_registration_success_rate",
        numerator=passed,
        denominator=attempted,
        measured_value=value,
        unit=targets.get("measured_value_unit", "percent"),
        target=targets.get("target"),
        extra={
            "cases_version": cases_doc.get("cases_version"),
            "denominator_zero_policy": cases_doc.get("denominator_zero_policy"),
            "cases": details,
            "counting_rule": targets.get("counting_rule"),
            "notes": (
                "Attempt = predefined registration case with at least one existing "
                "pytest path executed. Denominator 0 → measured_value null."
            ),
        },
    )


def measure_kpi_05_composite() -> dict[str, Any]:
    import os

    cases_doc = load_yaml(eval_root() / "workflow-scenarios" / "scenarios.yaml")
    include_integration = bool(os.environ.get("TEST_DATABASE_URL", "").strip())
    passed, attempted, details = _run_case_pytest_groups(
        list(cases_doc.get("scenarios") or []),
        include_integration=include_integration,
    )
    targets = _targets()["targets"]["KPI-05"]
    value = percent(passed, attempted)
    return _metric_base(
        kpi_id="KPI-05",
        name="composite_scenario_completion_rate",
        numerator=passed,
        denominator=attempted,
        measured_value=value,
        unit=targets.get("measured_value_unit", "percent"),
        target=targets.get("target"),
        extra={
            "scenarios_version": cases_doc.get("scenarios_version"),
            "denominator_zero_policy": cases_doc.get("denominator_zero_policy"),
            "scenarios": details,
            "counting_rule": targets.get("counting_rule"),
            "notes": (
                "Uses real executable workflow/execution pytest modules "
                "(not mock-only success placeholders)."
            ),
        },
    )


def measure_kpi_06_operations() -> dict[str, Any]:
    import os

    cases_doc = load_yaml(eval_root() / "operation" / "cases.yaml")
    include_integration = bool(os.environ.get("TEST_DATABASE_URL", "").strip())
    passed, attempted, details = _run_case_pytest_groups(
        list(cases_doc.get("cases") or []),
        include_integration=include_integration,
    )
    targets = _targets()["targets"]["KPI-06"]
    value = percent(passed, attempted)
    categories = sorted(
        {
            c.get("category")
            for c in (cases_doc.get("cases") or [])
            if c.get("category")
        }
    )
    return _metric_base(
        kpi_id="KPI-06",
        name="operations_function_pass_rate",
        numerator=passed,
        denominator=attempted,
        measured_value=value,
        unit=targets.get("measured_value_unit", "percent"),
        target=targets.get("target"),
        extra={
            "cases_version": cases_doc.get("cases_version"),
            "denominator_zero_policy": cases_doc.get("denominator_zero_policy"),
            "required_categories": [
                "authorization",
                "approval",
                "audit",
                "cancellation",
                "recovery",
            ],
            "categories_present": categories,
            "cases": details,
            "counting_rule": targets.get("counting_rule"),
        },
    )


def measure_all() -> dict[str, Any]:
    targets_doc = _targets()
    metrics = [
        measure_kpi_01_response_time(),
        measure_kpi_02_tool_mapping(),
        measure_kpi_03_tool_count(),
        measure_kpi_04_registration(),
        measure_kpi_05_composite(),
        measure_kpi_06_operations(),
    ]
    report = {
        "schema_version": "mcpflow.kpi_report.v1",
        "dataset_bundle_version": targets_doc.get("dataset_bundle_version"),
        "measured_at": measured_at(),
        "git_sha": git_sha(),
        "environment": environment_label(),
        "metrics": metrics,
    }
    return redact_secrets(report)


def print_human_summary(report: dict[str, Any]) -> None:
    print("=== MCPFlow KPI Measurement ===")
    print(f"dataset_bundle : {report.get('dataset_bundle_version')}")
    print(f"git_sha        : {report.get('git_sha')}")
    print(f"environment    : {report.get('environment')}")
    print(f"measured_at    : {report.get('measured_at')}")
    print("")
    for metric in report["metrics"]:
        num = metric.get("numerator")
        den = metric.get("denominator")
        value = metric.get("measured_value")
        target = metric.get("target")
        line = (
            f"{metric['kpi_id']} {metric['name']}: "
            f"value={value} {metric.get('unit')} "
            f"(numerator={num}, denominator={den}, target={target})"
        )
        if metric["kpi_id"] == "KPI-03":
            line += (
                f" target_min={metric.get('target_min')} "
                f"gap={metric.get('gap_to_target_min')} "
                f"inventory={metric.get('inventory_status')}"
            )
        print(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MCPFlow KPI measurement harness")
    parser.add_argument(
        "--out",
        default="",
        help="JSON output path (default: artifacts/kpi/latest.json)",
    )
    parser.add_argument(
        "--only",
        default="",
        help="Comma-separated KPI ids to measure (default: all)",
    )
    args = parser.parse_args(argv)

    if args.only.strip():
        wanted = {part.strip().upper() for part in args.only.split(",") if part.strip()}
        mapping = {
            "KPI-01": measure_kpi_01_response_time,
            "KPI-02": measure_kpi_02_tool_mapping,
            "KPI-03": measure_kpi_03_tool_count,
            "KPI-04": measure_kpi_04_registration,
            "KPI-05": measure_kpi_05_composite,
            "KPI-06": measure_kpi_06_operations,
        }
        metrics = [mapping[k]() for k in mapping if k in wanted]
        targets_doc = _targets()
        report = redact_secrets(
            {
                "schema_version": "mcpflow.kpi_report.v1",
                "dataset_bundle_version": targets_doc.get("dataset_bundle_version"),
                "measured_at": measured_at(),
                "git_sha": git_sha(),
                "environment": environment_label(),
                "metrics": metrics,
            }
        )
    else:
        report = measure_all()

    out_path = Path(args.out) if args.out else artifact_dir("kpi") / "latest.json"
    write_json(out_path, report)
    print_human_summary(report)
    print(f"\nJSON: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
