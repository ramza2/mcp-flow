"""Read-only durable evidence helpers for live_official KPI MEASURED claims.

Env vars may select IDs / configuration only. Raw pass/fail or numeric result
env values must never produce MEASURED by themselves. Declared pins and
scenario identity must match durable lineage before PASS/MEASURED.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.domain.enums import ApprovalStatus, ExecutionStatus
from app.models.approval import ApprovalRequest
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_discovery import MCPDiscoveryRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_verification import MCPToolVerificationRepository
from app.repositories.tool_selection import ToolSelectionRepository
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def db_url() -> str:
    return (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or ""
    ).strip()


def parse_uuid(value: str | None) -> uuid.UUID | None:
    if not value or not str(value).strip():
        return None
    try:
        return uuid.UUID(str(value).strip())
    except ValueError:
        return None


def pinned_ids() -> dict[str, uuid.UUID | None]:
    return {
        "agent_version_id": parse_uuid(os.environ.get("MCPFLOW_KPI_AGENT_VERSION_ID")),
        "llm_profile_id": parse_uuid(os.environ.get("MCPFLOW_KPI_LLM_PROFILE_ID")),
        "embedding_profile_id": parse_uuid(
            os.environ.get("MCPFLOW_KPI_EMBEDDING_PROFILE_ID")
        ),
        "workflow_version_id": parse_uuid(
            os.environ.get("MCPFLOW_KPI_WORKFLOW_VERSION_ID")
        ),
    }


def validate_agent_request_lineage(
    *,
    request_agent_version_id: uuid.UUID,
    selection_agent_version_id: uuid.UUID | None,
    selection_llm_profile_id: uuid.UUID | None,
    selection_embedding_profile_id: uuid.UUID | None,
    agent_version_llm_profile_id: uuid.UUID | None,
    pinned_agent_version_id: uuid.UUID | None,
    pinned_llm_profile_id: uuid.UUID | None,
    pinned_embedding_profile_id: uuid.UUID | None,
) -> list[str]:
    """Return mismatch reasons; empty means lineage OK for MEASURED."""
    reasons: list[str] = []
    if pinned_agent_version_id is None:
        reasons.append("PIN_AGENT_VERSION_MISSING")
    elif request_agent_version_id != pinned_agent_version_id:
        reasons.append("AGENT_VERSION_PIN_MISMATCH")
    if (
        selection_agent_version_id is not None
        and pinned_agent_version_id is not None
        and selection_agent_version_id != pinned_agent_version_id
    ):
        reasons.append("SELECTION_AGENT_VERSION_MISMATCH")
    if pinned_llm_profile_id is not None:
        if (
            agent_version_llm_profile_id is not None
            and agent_version_llm_profile_id != pinned_llm_profile_id
        ):
            reasons.append("AGENT_VERSION_LLM_PROFILE_MISMATCH")
        if (
            selection_llm_profile_id is not None
            and selection_llm_profile_id != pinned_llm_profile_id
        ):
            reasons.append("SELECTION_LLM_PROFILE_MISMATCH")
    if (
        pinned_embedding_profile_id is not None
        and selection_embedding_profile_id is not None
        and selection_embedding_profile_id != pinned_embedding_profile_id
    ):
        reasons.append("SELECTION_EMBEDDING_PROFILE_MISMATCH")
    return reasons


@dataclass(frozen=True, slots=True)
class Kpi01Evidence:
    agent_request_id: uuid.UUID
    tool_selection_run_id: uuid.UUID
    agent_version_id: uuid.UUID
    t_start: datetime
    t_end: datetime
    duration_ms: float
    decision: str
    lineage_ok: bool
    lineage_reasons: tuple[str, ...]
    evidence_ids: dict[str, str]


def compute_kpi_01_duration_ms(*, t_start: datetime, t_end: datetime) -> float:
    return round((t_end - t_start).total_seconds() * 1000.0, 3)


async def load_kpi_01_evidence(
    session: AsyncSession, *, agent_request_id: uuid.UUID
) -> Kpi01Evidence | None:
    pins = pinned_ids()
    request = await AgentRequestRepository(session).get(agent_request_id)
    if request is None:
        return None
    selection = await ToolSelectionRepository(session).get_latest_for_agent_request(
        agent_request_id
    )
    if selection is None:
        return None
    t_start = request.created_at
    t_end = selection.created_at
    if t_start is None or t_end is None:
        return None
    agent_version = await AgentVersionRepository(session).get(request.agent_version_id)
    reasons = validate_agent_request_lineage(
        request_agent_version_id=request.agent_version_id,
        selection_agent_version_id=selection.agent_version_id,
        selection_llm_profile_id=selection.llm_profile_id,
        selection_embedding_profile_id=selection.embedding_profile_id,
        agent_version_llm_profile_id=(
            agent_version.llm_profile_id if agent_version is not None else None
        ),
        pinned_agent_version_id=pins["agent_version_id"],
        pinned_llm_profile_id=pins["llm_profile_id"],
        pinned_embedding_profile_id=pins["embedding_profile_id"],
    )
    return Kpi01Evidence(
        agent_request_id=agent_request_id,
        tool_selection_run_id=selection.id,
        agent_version_id=request.agent_version_id,
        t_start=t_start,
        t_end=t_end,
        duration_ms=compute_kpi_01_duration_ms(t_start=t_start, t_end=t_end),
        decision=selection.decision,
        lineage_ok=not reasons,
        lineage_reasons=tuple(reasons),
        evidence_ids={
            "agent_request_id": str(agent_request_id),
            "tool_selection_run_id": str(selection.id),
            "agent_version_id": str(request.agent_version_id),
            "selection_llm_profile_id": str(selection.llm_profile_id),
            "selection_embedding_profile_id": str(selection.embedding_profile_id),
            "t_start": t_start.isoformat(),
            "t_end": t_end.isoformat(),
            "decision": selection.decision,
        },
    )


# ---------------------------------------------------------------------------
# KPI-02 — canonical dataset labels + selector-only evidence map
# ---------------------------------------------------------------------------


FORBIDDEN_EVIDENCE_MAP_KEYS = frozenset(
    {
        "expected_tool_version_id",
        "expected_tool_name",
        "expected_decision",
        "expected_labels",
        "correct",
        "pass",
        "measured_value",
    }
)


def load_canonical_kpi_02_cases(dataset: dict[str, Any]) -> list[dict[str, Any]]:
    """Eligible live cases = valid_for_top1 != false from versioned dataset."""
    cases = []
    for case in dataset.get("cases") or []:
        if case.get("valid_for_top1", True) is False:
            continue
        cases.append(case)
    return cases


def parse_kpi_02_selector_map(payload: dict[str, Any]) -> tuple[dict[str, uuid.UUID], list[str]]:
    """Parse selector-only map. Returns (case_id→request_id, errors)."""
    errors: list[str] = []
    raw_cases = list(payload.get("cases") or [])
    selectors: dict[str, uuid.UUID] = {}
    for item in raw_cases:
        if not isinstance(item, dict):
            errors.append("SELECTOR_ENTRY_NOT_OBJECT")
            continue
        banned = sorted(FORBIDDEN_EVIDENCE_MAP_KEYS.intersection(item.keys()))
        if banned:
            errors.append(f"FORBIDDEN_EXPECTED_LABEL_KEYS:{','.join(banned)}")
            continue
        case_id = str(item.get("case_id") or item.get("id") or "").strip()
        req_id = parse_uuid(
            str(item.get("agent_request_id")) if item.get("agent_request_id") else None
        )
        if not case_id:
            errors.append("MISSING_CASE_ID")
            continue
        if req_id is None:
            errors.append(f"MISSING_AGENT_REQUEST_ID:{case_id}")
            continue
        if case_id in selectors:
            errors.append(f"DUPLICATE_CASE_ID:{case_id}")
            continue
        selectors[case_id] = req_id
    return selectors, errors


def score_kpi_02_case(
    *,
    expected_tool_name: str | None,
    expected_decision: str | None,
    selected_tool_name: str | None,
    decision: str | None,
) -> bool:
    if expected_decision is not None and decision != expected_decision:
        return False
    if expected_tool_name is None:
        return selected_tool_name is None
    return selected_tool_name == expected_tool_name


@dataclass(frozen=True, slots=True)
class Kpi02CaseEvidence:
    case_id: str
    agent_request_id: uuid.UUID | None
    expected_tool_name: str | None
    expected_decision: str | None
    tool_selection_run_id: uuid.UUID | None
    selected_tool_name: str | None
    decision: str | None
    correct: bool | None
    status: str  # PASS | FAIL | NOT_RUN
    reason: str | None = None
    evidence_ids: dict[str, str] | None = None


async def evaluate_kpi_02_cases(
    session: AsyncSession,
    *,
    dataset_cases: list[dict[str, Any]],
    selectors: dict[str, uuid.UUID],
) -> list[Kpi02CaseEvidence]:
    pins = pinned_ids()
    tools = MCPToolRepository(session)
    out: list[Kpi02CaseEvidence] = []
    for case in dataset_cases:
        case_id = str(case["id"])
        expected_tool_name = case.get("expected_tool_name")
        expected_decision = case.get("expected_decision")
        req_id = selectors.get(case_id)
        if req_id is None:
            out.append(
                Kpi02CaseEvidence(
                    case_id=case_id,
                    agent_request_id=None,
                    expected_tool_name=expected_tool_name,
                    expected_decision=expected_decision,
                    tool_selection_run_id=None,
                    selected_tool_name=None,
                    decision=None,
                    correct=None,
                    status="NOT_RUN",
                    reason="MISSING_SELECTOR_FOR_DECLARED_CASE",
                )
            )
            continue
        request = await AgentRequestRepository(session).get(req_id)
        selection = await ToolSelectionRepository(session).get_latest_for_agent_request(
            req_id
        )
        if request is None or selection is None:
            out.append(
                Kpi02CaseEvidence(
                    case_id=case_id,
                    agent_request_id=req_id,
                    expected_tool_name=expected_tool_name,
                    expected_decision=expected_decision,
                    tool_selection_run_id=None,
                    selected_tool_name=None,
                    decision=None,
                    correct=None,
                    status="NOT_RUN",
                    reason="DURABLE_REQUEST_OR_SELECTION_MISSING",
                )
            )
            continue
        agent_version = await AgentVersionRepository(session).get(request.agent_version_id)
        lineage = validate_agent_request_lineage(
            request_agent_version_id=request.agent_version_id,
            selection_agent_version_id=selection.agent_version_id,
            selection_llm_profile_id=selection.llm_profile_id,
            selection_embedding_profile_id=selection.embedding_profile_id,
            agent_version_llm_profile_id=(
                agent_version.llm_profile_id if agent_version is not None else None
            ),
            pinned_agent_version_id=pins["agent_version_id"],
            pinned_llm_profile_id=pins["llm_profile_id"],
            pinned_embedding_profile_id=pins["embedding_profile_id"],
        )
        if lineage:
            out.append(
                Kpi02CaseEvidence(
                    case_id=case_id,
                    agent_request_id=req_id,
                    expected_tool_name=expected_tool_name,
                    expected_decision=expected_decision,
                    tool_selection_run_id=selection.id,
                    selected_tool_name=None,
                    decision=selection.decision,
                    correct=None,
                    status="NOT_RUN",
                    reason="LINEAGE_MISMATCH:" + ",".join(lineage),
                    evidence_ids={
                        "agent_request_id": str(req_id),
                        "tool_selection_run_id": str(selection.id),
                        "agent_version_id": str(request.agent_version_id),
                    },
                )
            )
            continue
        selected_name: str | None = None
        if selection.selected_tool_version_id is not None:
            version = await tools.get_version(selection.selected_tool_version_id)
            if version is not None:
                tool = await tools.get(version.mcp_tool_id)
                selected_name = tool.remote_name if tool is not None else None
        correct = score_kpi_02_case(
            expected_tool_name=expected_tool_name,
            expected_decision=expected_decision,
            selected_tool_name=selected_name,
            decision=selection.decision,
        )
        out.append(
            Kpi02CaseEvidence(
                case_id=case_id,
                agent_request_id=req_id,
                expected_tool_name=expected_tool_name,
                expected_decision=expected_decision,
                tool_selection_run_id=selection.id,
                selected_tool_name=selected_name,
                decision=selection.decision,
                correct=correct,
                status="PASS" if correct else "FAIL",
                evidence_ids={
                    "agent_request_id": str(req_id),
                    "tool_selection_run_id": str(selection.id),
                    "agent_version_id": str(request.agent_version_id),
                    "selected_tool_version_id": (
                        str(selection.selected_tool_version_id)
                        if selection.selected_tool_version_id
                        else ""
                    ),
                    "selected_tool_name": selected_name or "",
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# KPI-04
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Kpi04AttemptEvidence:
    attempt_id: str
    operation: str
    success: bool
    status: str  # PASS | FAIL | NOT_RUN
    evidence_ids: dict[str, str]
    failure_category: str | None = None


def discovery_has_durable_apply(raw_response: dict[str, Any] | None) -> bool:
    if not isinstance(raw_response, dict):
        return False
    if raw_response.get("factory_apply_executed") is True:
        return True
    fr = raw_response.get("factory_reconciliation")
    if isinstance(fr, dict) and fr.get("status") == "CLEAN" and (
        raw_response.get("apply_changes") or fr.get("apply_executed") is True
    ):
        return True
    return False


async def evaluate_kpi_04_attempts(
    session: AsyncSession,
    *,
    smoke_server_id: uuid.UUID,
    check_id: uuid.UUID | None,
    discovery_id: uuid.UUID | None,
    verification_id: uuid.UUID | None,
) -> list[Kpi04AttemptEvidence]:
    disco = MCPDiscoveryRepository(session)
    tools = MCPToolRepository(session)
    verifications = MCPToolVerificationRepository(session)
    results: list[Kpi04AttemptEvidence] = []

    check = await disco.get_check(check_id) if check_id else None
    if check is None and check_id is None:
        check = await disco.get_latest_check(smoke_server_id)
    if check is None or check.mcp_server_id != smoke_server_id:
        results.append(
            Kpi04AttemptEvidence(
                attempt_id="smoke-connect",
                operation="connect",
                success=False,
                status="NOT_RUN",
                evidence_ids={"mcp_server_id": str(smoke_server_id)},
                failure_category="CHECK_EVIDENCE_MISSING",
            )
        )
    else:
        ok = check.status == "SUCCEEDED"
        results.append(
            Kpi04AttemptEvidence(
                attempt_id="smoke-connect",
                operation="connect",
                success=ok,
                status="PASS" if ok else "FAIL",
                evidence_ids={
                    "mcp_server_id": str(smoke_server_id),
                    "mcp_server_check_id": str(check.id),
                    "checked_at": check.checked_at.isoformat() if check.checked_at else "",
                },
                failure_category=None if ok else "CHECK_NOT_SUCCEEDED",
            )
        )

    discovery = await disco.get_discovery(discovery_id) if discovery_id else None
    if discovery is None and discovery_id is None:
        latest_d, _ = await disco.list_discoveries(
            mcp_server_id=smoke_server_id, page=1, page_size=1
        )
        discovery = latest_d[0] if latest_d else None
    if discovery is None or discovery.mcp_server_id != smoke_server_id:
        results.append(
            Kpi04AttemptEvidence(
                attempt_id="smoke-discovery",
                operation="discovery",
                success=False,
                status="NOT_RUN",
                evidence_ids={"mcp_server_id": str(smoke_server_id)},
                failure_category="DISCOVERY_EVIDENCE_MISSING",
            )
        )
        discovery_ok = False
    else:
        discovery_ok = bool(discovery.success)
        results.append(
            Kpi04AttemptEvidence(
                attempt_id="smoke-discovery",
                operation="discovery",
                success=discovery_ok,
                status="PASS" if discovery_ok else "FAIL",
                evidence_ids={
                    "mcp_server_id": str(smoke_server_id),
                    "mcp_server_discovery_id": str(discovery.id),
                    "finished_at": (
                        discovery.finished_at.isoformat()
                        if discovery.finished_at
                        else ""
                    ),
                },
                failure_category=None if discovery_ok else "DISCOVERY_NOT_SUCCESS",
            )
        )

    # apply/register — require durable discovery apply lineage bound to smoke server
    if (
        discovery is not None
        and discovery.mcp_server_id == smoke_server_id
        and discovery_has_durable_apply(discovery.raw_response)
    ):
        results.append(
            Kpi04AttemptEvidence(
                attempt_id="smoke-apply-register",
                operation="apply/register",
                success=True,
                status="PASS",
                evidence_ids={
                    "mcp_server_id": str(smoke_server_id),
                    "mcp_server_discovery_id": str(discovery.id),
                    "factory_apply_executed": "true",
                },
            )
        )
    else:
        results.append(
            Kpi04AttemptEvidence(
                attempt_id="smoke-apply-register",
                operation="apply/register",
                success=False,
                status="NOT_RUN",
                evidence_ids={
                    "mcp_server_id": str(smoke_server_id),
                    "mcp_server_discovery_id": (
                        str(discovery.id) if discovery is not None else ""
                    ),
                },
                failure_category="DURABLE_APPLY_EVIDENCE_MISSING",
            )
        )

    # verification — must resolve to Tool on smoke_server_id
    if verification_id is not None:
        row = await verifications.get(verification_id)
        if row is None:
            results.append(
                Kpi04AttemptEvidence(
                    attempt_id="smoke-verification",
                    operation="verification",
                    success=False,
                    status="NOT_RUN",
                    evidence_ids={"mcp_tool_verification_id": str(verification_id)},
                    failure_category="VERIFICATION_NOT_FOUND",
                )
            )
        else:
            version = await tools.get_version(row.mcp_tool_version_id)
            tool = (
                await tools.get(version.mcp_tool_id) if version is not None else None
            )
            if tool is None or tool.mcp_server_id != smoke_server_id:
                results.append(
                    Kpi04AttemptEvidence(
                        attempt_id="smoke-verification",
                        operation="verification",
                        success=False,
                        status="FAIL",
                        evidence_ids={
                            "mcp_tool_verification_id": str(row.id),
                            "mcp_tool_version_id": str(row.mcp_tool_version_id),
                            "mcp_tool_id": str(tool.id) if tool is not None else "",
                            "mcp_server_id": (
                                str(tool.mcp_server_id) if tool is not None else ""
                            ),
                            "smoke_server_id": str(smoke_server_id),
                        },
                        failure_category="VERIFICATION_SERVER_MISMATCH",
                    )
                )
            else:
                ok = row.status == "VERIFIED"
                results.append(
                    Kpi04AttemptEvidence(
                        attempt_id="smoke-verification",
                        operation="verification",
                        success=ok,
                        status="PASS" if ok else "FAIL",
                        evidence_ids={
                            "mcp_tool_verification_id": str(row.id),
                            "mcp_tool_version_id": str(row.mcp_tool_version_id),
                            "mcp_tool_id": str(tool.id),
                            "mcp_server_id": str(tool.mcp_server_id),
                        },
                        failure_category=None if ok else "VERIFICATION_NOT_VERIFIED",
                    )
                )
    else:
        tool_rows, _ = await tools.list_tools(
            mcp_server_id=smoke_server_id, page=1, page_size=5
        )
        if tool_rows and tool_rows[0].current_version_id is not None:
            version_id = tool_rows[0].current_version_id
            ok = await verifications.has_effective_verified(version_id)
            results.append(
                Kpi04AttemptEvidence(
                    attempt_id="smoke-verification",
                    operation="verification",
                    success=ok,
                    status="PASS" if ok else "FAIL",
                    evidence_ids={
                        "mcp_tool_id": str(tool_rows[0].id),
                        "mcp_tool_version_id": str(version_id),
                        "mcp_server_id": str(smoke_server_id),
                    },
                    failure_category=None if ok else "NO_EFFECTIVE_VERIFIED",
                )
            )
        else:
            results.append(
                Kpi04AttemptEvidence(
                    attempt_id="smoke-verification",
                    operation="verification",
                    success=False,
                    status="NOT_RUN",
                    evidence_ids={"mcp_server_id": str(smoke_server_id)},
                    failure_category="VERIFICATION_EVIDENCE_MISSING",
                )
            )

    return results


# ---------------------------------------------------------------------------
# KPI-05 — scenario shape + unique execution IDs
# ---------------------------------------------------------------------------


_TERMINAL_PASS = {ExecutionStatus.SUCCEEDED.value}
_TERMINAL_FAIL = {
    ExecutionStatus.FAILED.value,
    ExecutionStatus.CANCELLED.value,
    ExecutionStatus.TIMED_OUT.value,
    ExecutionStatus.PARTIALLY_SUCCEEDED.value,
}


@dataclass(frozen=True, slots=True)
class Kpi05ScenarioEvidence:
    scenario_id: str
    execution_id: uuid.UUID | None
    status: str  # PASS | FAIL | NOT_RUN
    success: bool
    execution_status: str | None
    evidence_ids: dict[str, str]
    reason: str | None = None


def score_execution_terminal(status: str) -> str:
    if status in _TERMINAL_PASS:
        return "PASS"
    if status in _TERMINAL_FAIL:
        return "FAIL"
    return "NOT_RUN"


def plan_matches_scenario_kind(
    plan_snapshot: dict[str, Any] | None, kind: str
) -> tuple[bool, str | None]:
    """Validate persisted plan_snapshot shape for declared scenario kind."""
    if not isinstance(plan_snapshot, dict):
        return False, "PLAN_SNAPSHOT_MISSING"
    steps = list(plan_snapshot.get("steps") or [])
    if not steps:
        return False, "PLAN_STEPS_EMPTY"
    by_type: dict[str, list[dict[str, Any]]] = {}
    for step in steps:
        if not isinstance(step, dict):
            continue
        by_type.setdefault(str(step.get("type") or ""), []).append(step)

    if kind == "sequential":
        tools = by_type.get("TOOL") or []
        if len(tools) < 2:
            return False, "SEQUENTIAL_REQUIRES_MULTI_TOOL"
        # At least one dependency edge among TOOL steps.
        tool_ids = {str(t.get("id")) for t in tools}
        has_chain = False
        for tool in tools:
            deps = [str(d) for d in (tool.get("depends_on") or [])]
            if any(dep in tool_ids for dep in deps):
                has_chain = True
                break
        if not has_chain:
            return False, "SEQUENTIAL_DEPENDENCY_CHAIN_MISSING"
        return True, None

    if kind == "parallel":
        joins = by_type.get("JOIN") or []
        if not joins:
            return False, "PARALLEL_JOIN_MISSING"
        # Fan-in: JOIN depends on >=2 predecessors, or >=2 TOOL roots (empty depends_on).
        join = joins[0]
        deps = list(join.get("depends_on") or [])
        tool_roots = [
            t
            for t in (by_type.get("TOOL") or [])
            if not list(t.get("depends_on") or [])
        ]
        if len(deps) < 2 and len(tool_roots) < 2:
            return False, "PARALLEL_WAVE_SHAPE_MISSING"
        return True, None

    if kind == "condition":
        if not (by_type.get("CONDITION") or []):
            return False, "CONDITION_STEP_MISSING"
        return True, None

    if kind == "foreach":
        loops = by_type.get("LOOP") or []
        if not any(
            isinstance(step.get("config"), dict)
            and step["config"].get("mode") == "FOR_EACH"
            for step in loops
        ):
            return False, "FOREACH_LOOP_MISSING"
        return True, None

    if kind == "while":
        loops = by_type.get("LOOP") or []
        if not any(
            isinstance(step.get("config"), dict)
            and step["config"].get("mode") == "WHILE"
            for step in loops
        ):
            return False, "WHILE_LOOP_MISSING"
        return True, None

    if kind == "approval":
        # Plan may be TOOL-only with ToolPolicy approval; approval evidence checked separately.
        return True, None

    return False, f"UNKNOWN_SCENARIO_KIND:{kind}"


async def _execution_has_approval_evidence(
    session: AsyncSession, *, execution_id: uuid.UUID
) -> tuple[bool, str | None]:
    stmt = (
        select(ApprovalRequest)
        .where(ApprovalRequest.execution_id == execution_id)
        .order_by(ApprovalRequest.requested_at.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return False, None
    return True, str(row.id)


async def evaluate_kpi_05_scenarios(
    session: AsyncSession, scenarios: list[dict[str, Any]]
) -> list[Kpi05ScenarioEvidence]:
    repo = ExecutionRepository(session)
    pins = pinned_ids()
    allow_reuse = os.environ.get("MCPFLOW_KPI_05_ALLOW_EXECUTION_REUSE", "").strip() in {
        "1",
        "true",
        "yes",
    }
    seen_exec_ids: dict[uuid.UUID, str] = {}
    out: list[Kpi05ScenarioEvidence] = []

    for scenario in scenarios:
        scenario_id = str(scenario["scenario_id"])
        kind = str(scenario.get("kind") or "")
        env_key = "MCPFLOW_KPI_EXECUTION_ID_" + scenario_id.upper().replace("-", "_")
        exec_id = parse_uuid(os.environ.get(env_key))
        if exec_id is None:
            out.append(
                Kpi05ScenarioEvidence(
                    scenario_id=scenario_id,
                    execution_id=None,
                    status="NOT_RUN",
                    success=False,
                    execution_status=None,
                    evidence_ids={},
                    reason=f"MISSING_SELECTOR:{env_key}",
                )
            )
            continue
        if not allow_reuse and exec_id in seen_exec_ids:
            out.append(
                Kpi05ScenarioEvidence(
                    scenario_id=scenario_id,
                    execution_id=exec_id,
                    status="FAIL",
                    success=False,
                    execution_status=None,
                    evidence_ids={
                        "execution_id": str(exec_id),
                        "reused_from_scenario_id": seen_exec_ids[exec_id],
                    },
                    reason="EXECUTION_ID_REUSED_ACROSS_SCENARIOS",
                )
            )
            continue
        seen_exec_ids[exec_id] = scenario_id

        execution = await repo.get(exec_id)
        if execution is None:
            out.append(
                Kpi05ScenarioEvidence(
                    scenario_id=scenario_id,
                    execution_id=exec_id,
                    status="NOT_RUN",
                    success=False,
                    execution_status=None,
                    evidence_ids={"execution_id": str(exec_id)},
                    reason="EXECUTION_NOT_FOUND",
                )
            )
            continue

        if (
            pins["workflow_version_id"] is not None
            and execution.workflow_version_id is not None
            and execution.workflow_version_id != pins["workflow_version_id"]
        ):
            out.append(
                Kpi05ScenarioEvidence(
                    scenario_id=scenario_id,
                    execution_id=exec_id,
                    status="FAIL",
                    success=False,
                    execution_status=execution.status,
                    evidence_ids={
                        "execution_id": str(exec_id),
                        "workflow_version_id": str(execution.workflow_version_id),
                        "pinned_workflow_version_id": str(pins["workflow_version_id"]),
                    },
                    reason="WORKFLOW_VERSION_PIN_MISMATCH",
                )
            )
            continue
        if (
            pins["agent_version_id"] is not None
            and execution.agent_version_id is not None
            and execution.agent_version_id != pins["agent_version_id"]
        ):
            out.append(
                Kpi05ScenarioEvidence(
                    scenario_id=scenario_id,
                    execution_id=exec_id,
                    status="FAIL",
                    success=False,
                    execution_status=execution.status,
                    evidence_ids={
                        "execution_id": str(exec_id),
                        "agent_version_id": str(execution.agent_version_id),
                        "pinned_agent_version_id": str(pins["agent_version_id"]),
                    },
                    reason="AGENT_VERSION_PIN_MISMATCH",
                )
            )
            continue

        terminal = score_execution_terminal(execution.status)
        if terminal != "PASS":
            out.append(
                Kpi05ScenarioEvidence(
                    scenario_id=scenario_id,
                    execution_id=exec_id,
                    status=terminal,
                    success=False,
                    execution_status=execution.status,
                    evidence_ids={
                        "execution_id": str(exec_id),
                        "execution_status": execution.status,
                    },
                    reason=(
                        "EXECUTION_NOT_TERMINAL"
                        if terminal == "NOT_RUN"
                        else "EXECUTION_NOT_SUCCEEDED"
                    ),
                )
            )
            continue

        plan_ok, plan_reason = plan_matches_scenario_kind(
            execution.plan_snapshot if isinstance(execution.plan_snapshot, dict) else None,
            kind,
        )
        if not plan_ok:
            out.append(
                Kpi05ScenarioEvidence(
                    scenario_id=scenario_id,
                    execution_id=exec_id,
                    status="FAIL",
                    success=False,
                    execution_status=execution.status,
                    evidence_ids={
                        "execution_id": str(exec_id),
                        "execution_status": execution.status,
                        "scenario_kind": kind,
                    },
                    reason=plan_reason,
                )
            )
            continue

        approval_id: str | None = None
        if kind == "approval":
            has_apr, approval_id = await _execution_has_approval_evidence(
                session, execution_id=exec_id
            )
            if not has_apr:
                # Also accept WAITING_APPROVAL historical via status in result_summary? No —
                # require durable ApprovalRequest row.
                out.append(
                    Kpi05ScenarioEvidence(
                        scenario_id=scenario_id,
                        execution_id=exec_id,
                        status="FAIL",
                        success=False,
                        execution_status=execution.status,
                        evidence_ids={"execution_id": str(exec_id)},
                        reason="APPROVAL_EVIDENCE_MISSING",
                    )
                )
                continue

        out.append(
            Kpi05ScenarioEvidence(
                scenario_id=scenario_id,
                execution_id=exec_id,
                status="PASS",
                success=True,
                execution_status=execution.status,
                evidence_ids={
                    "execution_id": str(exec_id),
                    "execution_status": execution.status,
                    "scenario_kind": kind,
                    "finished_at": (
                        execution.finished_at.isoformat()
                        if execution.finished_at
                        else ""
                    ),
                    "workflow_version_id": (
                        str(execution.workflow_version_id)
                        if execution.workflow_version_id
                        else ""
                    ),
                    "approval_request_id": approval_id or "",
                },
            )
        )
    return out


async def with_session(coro_factory):  # type: ignore[no-untyped-def]
    url = db_url()
    if not url:
        raise RuntimeError("NO_DATABASE_URL")
    engine = create_async_engine(url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            return await coro_factory(session)
    finally:
        await engine.dispose()


# Re-export Path helper used by kpi_measure
def load_json_path(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
