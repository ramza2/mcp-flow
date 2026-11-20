"""Prepare six isolated L3-04 evaluation Workflows and least-privilege grants.

In the repository root on the MCPFlow server:
  docker compose --env-file .env.server -f compose.yaml -f compose.server.yaml \
    exec -T api /entrypoint.sh python - --preflight < scripts/ops/l304_prepare.py
  docker compose --env-file .env.server -f compose.yaml -f compose.server.yaml \
    exec -T api /entrypoint.sh python - --apply < scripts/ops/l304_prepare.py

The default preflight is SELECT-only. --apply atomically creates six new
Workflow/Version resources (PUBLISHED/VALID, Workflow ACTIVE) and seven grants
for ramza, through existing application services. Existing plans are only read.
No Execution, approval decision, MCP call, schedule or ToolPolicy mutation.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.agent.complex_plan_validator import StaticComplexPlanValidator
from app.core.config import get_settings
from app.models.approval import ApprovalPolicy
from app.models.auth import ResourceGrant
from app.models.mcp import MCPServer, MCPTool, MCPToolPolicy, MCPToolVersion
from app.models.workflow import Workflow, WorkflowVersion
from app.repositories.role import RoleRepository, UserRoleRepository
from app.repositories.user import UserRepository
from app.schemas.auth import ResourceGrantCreate
from app.schemas.execution_plan import ExecutionPlanV1
from app.schemas.workflow import WorkflowCreate, WorkflowUpdate, WorkflowVersionCreate
from app.services.authorization import AuthorizationResolver, ResourceGrantService
from app.services.workflow import WorkflowService
from app.services.workflow_version import WorkflowVersionService

USER_ID = uuid.UUID("eda43c3a-e301-4508-b3c2-e71079e04287")
TOOL_ID = uuid.UUID("477b6574-56b7-47a7-a60e-9b0d72547b06")
POLICY_ID = uuid.UUID("c9719cc4-70fa-4451-9ba6-22c9ee2238db")
EXECUTOR_ROLE = "l304_executor"
ROLE_ID = uuid.UUID("e312f36d-ae92-4a3f-a0bc-e64f3a79889c")
REQUIRED_PERMISSIONS = {"workflow.execute", "mcp.tool.execute"}

SOURCES = (
    ("sequential_tool_chain", "sequential", "96331293-7857-4439-b05c-dc25874b20eb"),
    ("parallel_join_wave", "parallel", "fa6c0d77-9f36-4976-8131-767368c36a97"),
    ("condition_branch", "condition", "3bfab6c7-4bab-4899-b0d3-fe2690d3b22b"),
    ("foreach_loop", "foreach", "60cf53c6-6472-495e-9907-24b9ecbe678f"),
    ("while_loop", "while", "583ded78-c476-4c21-bc82-c60e6a579988"),
    ("approval_wait_resume", "approval", "251b095d-16d6-47b7-af32-189f7dc70e27"),
)


def workflow_name(case_id: str) -> str:
    return f"L3-04 Official Evaluation {case_id} 202611"


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise RuntimeError(reason)


def retarget_plan(source: dict, new_workflow_id: uuid.UUID, kind: str) -> dict:
    plan = copy.deepcopy(source)
    require(
        isinstance(plan.get("source"), dict)
        and plan["source"].get("type") == "WORKFLOW",
        "SOURCE_PLAN_NOT_WORKFLOW",
    )
    plan["source"]["workflow_id"] = str(new_workflow_id)
    if kind == "approval":
        steps = plan.get("steps", [])
        require(
            len(steps) == 1 and steps[0]["type"] == "TOOL",
            "APPROVAL_SOURCE_MUST_HAVE_ONE_TOOL",
        )
        require(not steps[0].get("depends_on"), "APPROVAL_SOURCE_TOOL_NOT_ROOT")
        require(not steps[0].get("when"), "APPROVAL_SOURCE_TOOL_HAS_WHEN")
        require(steps[0]["id"] != "l304_gate", "APPROVAL_GATE_ID_COLLISION")
        steps[0]["depends_on"] = ["l304_gate"]
        plan["steps"] = [
            {
                "id": "l304_gate",
                "name": "L3-04 human approval gate",
                "type": "APPROVAL",
                "required": True,
                "depends_on": [],
                "when": None,
                "timeout_seconds": 300,
                "on_error": "FAIL_EXECUTION",
                "config": {"approval_policy_id": str(POLICY_ID)},
            },
            *steps,
        ]
    return plan


async def inspect(session: AsyncSession) -> tuple[dict, list]:
    """Read-only checks, including validation of transformed candidate plans."""
    user = await UserRepository(session).get(USER_ID)
    require(
        user is not None
        and user.username == "ramza"
        and user.status == "ACTIVE"
        and user.password_hash is not None,
        "RAMZA_IDENTITY_PRECHECK_FAIL",
    )
    role = await RoleRepository(session).get_by_code(EXECUTOR_ROLE)
    require(role is not None and role.id == ROLE_ID, "L304_EXECUTOR_ROLE_MISMATCH")
    memberships = UserRoleRepository(session)
    user_role_ids = await memberships.list_role_ids(USER_ID)
    require(ROLE_ID in user_role_ids, "L304_EXECUTOR_ROLE_NOT_ASSIGNED")
    user_roles = await memberships.list_roles(USER_ID)
    require(not any(r.code == "admin" for r in user_roles), "RAMZA_CANNOT_HAVE_ADMIN_ROLE")
    auth = AuthorizationResolver(session)
    actual = await auth.get_effective_permission_codes(USER_ID)
    require(REQUIRED_PERMISSIONS.issubset(actual), "RAMZA_PERMISSIONS_MISSING")
    require("approval.decide" not in actual, "RAMZA_CANNOT_APPROVE")

    policy = await session.get(ApprovalPolicy, POLICY_ID)
    require(
        policy is not None
        and policy.code == "l304-evaluation-approval"
        and policy.status == "ACTIVE"
        and policy.decision_mode == "ANY"
        and policy.required_approvals == 1
        and policy.approver_scope == {"role_codes": ["admin"]}
        and policy.allow_self_approval is False
        and policy.default_expiry_seconds == 3600,
        "L304_APPROVAL_POLICY_CHANGED",
    )

    tool = await session.get(MCPTool, TOOL_ID)
    require(
        tool is not None
        and tool.status == "ACTIVE"
        and tool.deleted_at is None
        and tool.remote_name == "suggest_questions",
        "L304_TOOL_INACTIVE",
    )
    version = await session.get(MCPToolVersion, tool.current_version_id)
    require(
        version is not None
        and version.mcp_tool_id == TOOL_ID
        and version.validation_status == "VALID",
        "L304_TOOL_VERSION_NOT_VALID_CURRENT",
    )
    server = await session.get(MCPServer, tool.mcp_server_id)
    require(
        server is not None
        and server.status == "ACTIVE"
        and server.deleted_at is None,
        "L304_SERVER_INACTIVE",
    )
    tool_policy = (
        await session.execute(
            select(MCPToolPolicy).where(MCPToolPolicy.mcp_tool_id == TOOL_ID)
        )
    ).scalar_one_or_none()
    require(
        tool_policy is not None
        and tool_policy.risk_class == "READ_ONLY"
        and tool_policy.requires_confirmation is False
        and tool_policy.requires_approval is False
        and tool_policy.timeout_ms == 30000,
        "L304_TOOL_POLICY_DRIFT",
    )

    names = [workflow_name(k) for k, _, _ in SOURCES]
    existing = (
        await session.execute(select(Workflow.name).where(Workflow.name.in_(names)))
    ).scalars().all()
    require(not existing, "L304_WORKFLOW_NAME_ALREADY_EXISTS")

    plans = []
    validator = StaticComplexPlanValidator()
    for case_id, kind, source_id in SOURCES:
        old = await session.get(WorkflowVersion, uuid.UUID(source_id))
        require(
            old is not None
            and old.status == "PUBLISHED"
            and old.validation_status == "VALID",
            f"{case_id}:SOURCE_NOT_PUBLISHED_VALID",
        )
        source_workflow = await session.get(Workflow, old.workflow_id)
        require(
            source_workflow is not None and source_workflow.deleted_at is None,
            f"{case_id}:SOURCE_WORKFLOW_MISSING",
        )
        require(
            old.plan_definition.get("source")
            == {"type": "WORKFLOW", "workflow_id": str(old.workflow_id)},
            f"{case_id}:SOURCE_LINEAGE_INVALID",
        )
        candidate = retarget_plan(old.plan_definition, uuid.uuid4(), kind)
        try:
            canonical = ExecutionPlanV1.model_validate(candidate)
        except Exception as exc:
            raise RuntimeError(f"{case_id}:PLAN_SCHEMA_INVALID") from exc
        result = validator.validate(canonical)
        require(
            result.ok,
            f"{case_id}:STATIC_PLAN_INVALID:{','.join(result.error_codes[:3])}",
        )
        used_tool_versions = {
            step["config"]["tool_version_id"]
            for step in candidate["steps"]
            if step["type"] == "TOOL"
        }
        require(
            used_tool_versions == {str(version.id)},
            f"{case_id}:TOOL_VERSION_CHANGED",
        )
        for key in ("input_schema", "output_schema", "policy_defaults"):
            require(isinstance(getattr(old, key), dict), f"{case_id}:{key}_INVALID")
        plans.append((case_id, kind, old))
    return {"user_id": USER_ID, "tool_id": TOOL_ID}, plans


async def prepare(session: AsyncSession, plans: list) -> list[tuple]:
    """Use domain services within the caller's single outer transaction."""
    grants = ResourceGrantService(session)
    workflows = WorkflowService(session)
    versions = WorkflowVersionService(session)
    exists = (
        await session.execute(
            select(ResourceGrant.id).where(
                ResourceGrant.user_id == USER_ID,
                ResourceGrant.resource_type == "MCP_TOOL",
                ResourceGrant.resource_id == TOOL_ID,
            )
        )
    ).scalar_one_or_none()
    if exists is None:
        await grants.create_for_user(
            USER_ID, ResourceGrantCreate(resource_type="MCP_TOOL", resource_id=TOOL_ID)
        )

    records = []
    for case_id, kind, source_version in plans:
        workflow = await workflows.create(
            WorkflowCreate(
                name=workflow_name(case_id),
                description=f"L3-04 isolated prospective case: {case_id}",
                visibility="PRIVATE",
            )
        )
        cloned = retarget_plan(source_version.plan_definition, workflow.id, kind)
        version = await versions.create_version(
            workflow.id,
            WorkflowVersionCreate(
                plan_definition=cloned,
                plan_schema_version=source_version.plan_schema_version,
                input_schema=copy.deepcopy(source_version.input_schema),
                output_schema=copy.deepcopy(source_version.output_schema),
                policy_defaults=copy.deepcopy(source_version.policy_defaults),
                change_summary=f"L3-04 evaluation: {case_id}, independent from history",
            ),
        )
        checked = await versions.validate(workflow.id, version.id)
        require(checked.validation_status == "VALID", f"{case_id}:VALIDATION_FAILED")
        published = await versions.publish(workflow.id, version.id)
        require(published.status == "PUBLISHED", f"{case_id}:PUBLISH_FAILED")
        await session.refresh(workflow)
        active = await workflows.update(
            workflow.id,
            WorkflowUpdate(status="ACTIVE"),
            expected_lock_version=workflow.lock_version,
        )
        require(
            active.status == "ACTIVE" and active.current_version_id == published.id,
            f"{case_id}:ACTIVATION_FAILED",
        )
        await grants.create_for_user(
            USER_ID,
            ResourceGrantCreate(resource_type="WORKFLOW", resource_id=workflow.id),
        )
        records.append((case_id, workflow.id, published.id))

    auth = AuthorizationResolver(session)
    for case_id, workflow_id, _version_id in records:
        decision = await auth.authorize_resource(
            USER_ID, "workflow.execute", "WORKFLOW", workflow_id
        )
        require(decision.allowed, f"{case_id}:WORKFLOW_GRANT_NOT_EFFECTIVE")
    tool_decision = await auth.authorize_resource(
        USER_ID, "mcp.tool.execute", "MCP_TOOL", TOOL_ID
    )
    require(tool_decision.allowed, "TOOL_GRANT_NOT_EFFECTIVE")
    return records


async def main(mode: str) -> None:
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        if mode == "preflight":
            async with AsyncSession(engine) as session:
                await session.execute(text("SET TRANSACTION READ ONLY"))
                meta, plans = await inspect(session)
                await session.rollback()
            print("L304_PREPARATION_READONLY_PASS")
            print(f'USER_ID={meta["user_id"]}')
            print(f'TOOL_ID={meta["tool_id"]}')
            for case_id, kind, _ in plans:
                print(f"READY {case_id} {kind}")
        else:
            async with engine.begin() as connection:
                async with AsyncSession(
                    bind=connection,
                    join_transaction_mode="create_savepoint",
                    expire_on_commit=False,
                ) as session:
                    await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                    _meta, plans = await inspect(session)
                    records = await prepare(session, plans)
            print("L304_PREPARATION_APPLIED_PASS")
            print(f"TOOL_ID={TOOL_ID}")
            for case_id, workflow_id, version_id in records:
                print(f"CREATED {case_id} {workflow_id} {version_id}")
            print("NO_EXECUTION_STARTED=PASS")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--preflight", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(main("preflight" if args.preflight else "apply"))
