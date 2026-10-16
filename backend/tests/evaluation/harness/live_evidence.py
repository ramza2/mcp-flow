"""Read-only durable evidence helpers for live_official KPI MEASURED claims.

Env vars may select IDs / configuration only. Raw pass/fail or numeric result
env values must never produce MEASURED by themselves.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.domain.enums import ExecutionStatus
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_discovery import MCPDiscoveryRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_verification import MCPToolVerificationRepository
from app.repositories.tool_selection import ToolSelectionRepository
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


@dataclass(frozen=True, slots=True)
class Kpi01Evidence:
    agent_request_id: uuid.UUID
    tool_selection_run_id: uuid.UUID
    agent_version_id: uuid.UUID
    t_start: datetime
    t_end: datetime
    duration_ms: float
    decision: str


def compute_kpi_01_duration_ms(*, t_start: datetime, t_end: datetime) -> float:
    return round((t_end - t_start).total_seconds() * 1000.0, 3)


async def load_kpi_01_evidence(
    session: AsyncSession, *, agent_request_id: uuid.UUID
) -> Kpi01Evidence | None:
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
    return Kpi01Evidence(
        agent_request_id=agent_request_id,
        tool_selection_run_id=selection.id,
        agent_version_id=request.agent_version_id,
        t_start=t_start,
        t_end=t_end,
        duration_ms=compute_kpi_01_duration_ms(t_start=t_start, t_end=t_end),
        decision=selection.decision,
    )


@dataclass(frozen=True, slots=True)
class Kpi02CaseEvidence:
    case_id: str
    agent_request_id: uuid.UUID | None
    expected_tool_version_id: uuid.UUID | None
    expected_decision: str | None
    tool_selection_run_id: uuid.UUID | None
    selected_tool_version_id: uuid.UUID | None
    decision: str | None
    correct: bool | None
    status: str  # PASS | FAIL | NOT_RUN
    reason: str | None = None


def score_kpi_02_case(
    *,
    expected_tool_version_id: uuid.UUID | None,
    expected_decision: str | None,
    selected_tool_version_id: uuid.UUID | None,
    decision: str | None,
) -> bool:
    if expected_decision is not None and decision != expected_decision:
        return False
    if expected_tool_version_id is None:
        return selected_tool_version_id is None
    return selected_tool_version_id == expected_tool_version_id


def load_kpi_02_evidence_map(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("cases") or [])


async def evaluate_kpi_02_cases(
    session: AsyncSession, cases: list[dict[str, Any]]
) -> list[Kpi02CaseEvidence]:
    out: list[Kpi02CaseEvidence] = []
    for case in cases:
        case_id = str(case.get("case_id") or case.get("id") or "")
        req_id = parse_uuid(str(case.get("agent_request_id") or ""))
        raw_expected = case.get("expected_tool_version_id")
        expected_tool = parse_uuid(str(raw_expected) if raw_expected else None)
        expected_decision = case.get("expected_decision")
        if req_id is None:
            out.append(
                Kpi02CaseEvidence(
                    case_id=case_id,
                    agent_request_id=None,
                    expected_tool_version_id=expected_tool,
                    expected_decision=expected_decision,
                    tool_selection_run_id=None,
                    selected_tool_version_id=None,
                    decision=None,
                    correct=None,
                    status="NOT_RUN",
                    reason="MISSING_AGENT_REQUEST_ID",
                )
            )
            continue
        selection = await ToolSelectionRepository(session).get_latest_for_agent_request(
            req_id
        )
        if selection is None:
            out.append(
                Kpi02CaseEvidence(
                    case_id=case_id,
                    agent_request_id=req_id,
                    expected_tool_version_id=expected_tool,
                    expected_decision=expected_decision,
                    tool_selection_run_id=None,
                    selected_tool_version_id=None,
                    decision=None,
                    correct=None,
                    status="NOT_RUN",
                    reason="TOOL_SELECTION_RUN_NOT_FOUND",
                )
            )
            continue
        correct = score_kpi_02_case(
            expected_tool_version_id=expected_tool,
            expected_decision=expected_decision,
            selected_tool_version_id=selection.selected_tool_version_id,
            decision=selection.decision,
        )
        out.append(
            Kpi02CaseEvidence(
                case_id=case_id,
                agent_request_id=req_id,
                expected_tool_version_id=expected_tool,
                expected_decision=expected_decision,
                tool_selection_run_id=selection.id,
                selected_tool_version_id=selection.selected_tool_version_id,
                decision=selection.decision,
                correct=correct,
                status="PASS" if correct else "FAIL",
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class Kpi04AttemptEvidence:
    attempt_id: str
    operation: str
    success: bool
    status: str  # PASS | FAIL | NOT_RUN
    evidence_ids: dict[str, str]
    failure_category: str | None = None


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

    # connect
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

    # discovery
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
    else:
        ok = bool(discovery.success)
        results.append(
            Kpi04AttemptEvidence(
                attempt_id="smoke-discovery",
                operation="discovery",
                success=ok,
                status="PASS" if ok else "FAIL",
                evidence_ids={
                    "mcp_server_id": str(smoke_server_id),
                    "mcp_server_discovery_id": str(discovery.id),
                    "finished_at": (
                        discovery.finished_at.isoformat()
                        if discovery.finished_at
                        else ""
                    ),
                },
                failure_category=None if ok else "DISCOVERY_NOT_SUCCESS",
            )
        )

    # apply/register — durable registry rows for smoke server
    tool_rows, tool_total = await tools.list_tools(
        mcp_server_id=smoke_server_id, page=1, page_size=5
    )
    if tool_total < 1:
        results.append(
            Kpi04AttemptEvidence(
                attempt_id="smoke-apply-register",
                operation="apply/register",
                success=False,
                status="NOT_RUN",
                evidence_ids={"mcp_server_id": str(smoke_server_id)},
                failure_category="NO_REGISTERED_TOOLS",
            )
        )
    else:
        results.append(
            Kpi04AttemptEvidence(
                attempt_id="smoke-apply-register",
                operation="apply/register",
                success=True,
                status="PASS",
                evidence_ids={
                    "mcp_server_id": str(smoke_server_id),
                    "registered_tool_count": str(tool_total),
                    "sample_mcp_tool_id": str(tool_rows[0].id),
                },
            )
        )

    # verification — explicit verification id or first tool current version
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
                        "verified_at": (
                            row.verified_at.isoformat() if row.verified_at else ""
                        ),
                    },
                    failure_category=None if ok else "VERIFICATION_NOT_VERIFIED",
                )
            )
    elif tool_rows and tool_rows[0].current_version_id is not None:
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


_TERMINAL_PASS = {
    ExecutionStatus.SUCCEEDED.value,
}
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


async def evaluate_kpi_05_scenarios(
    session: AsyncSession, scenarios: list[dict[str, Any]]
) -> list[Kpi05ScenarioEvidence]:
    repo = ExecutionRepository(session)
    out: list[Kpi05ScenarioEvidence] = []
    for scenario in scenarios:
        scenario_id = str(scenario["scenario_id"])
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
        scored = score_execution_terminal(execution.status)
        out.append(
            Kpi05ScenarioEvidence(
                scenario_id=scenario_id,
                execution_id=exec_id,
                status=scored,
                success=scored == "PASS",
                execution_status=execution.status,
                evidence_ids={
                    "execution_id": str(exec_id),
                    "execution_status": execution.status,
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
                },
                reason=None if scored != "NOT_RUN" else "EXECUTION_NOT_TERMINAL",
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
