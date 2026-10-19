"""Unit tests for FNC-TOOL-006 server-derived Tool Verification evidence."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.core.config import get_settings
from app.core.errors import AppError
from app.domain.enums import (
    ExecutionSourceType,
    ExecutionStatus,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
    ToolVerificationStatus,
    ToolVersionValidationStatus,
)
from app.infrastructure.object_storage import (
    InMemoryObjectStorage,
    ObjectStorageError,
    reset_memory_object_storage,
)
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_verification import MCPToolVerificationRepository
from app.schemas.mcp_tool import ToolVerificationCreate
from app.services.mcp_tool_verification import (
    MCPToolVerificationService,
    verification_evidence_object_key,
)
from sqlalchemy.ext.asyncio import AsyncSession

from tests.helpers.tool_verification import (
    seed_manual_evidence_execution,
    seed_verification_context,
)


class _FailingObjectStorage(InMemoryObjectStorage):
    async def put_bytes(self, *, bucket: str, object_key: str, data: bytes, content_type: str):
        raise ObjectStorageError("PUT_FAILED", "simulated put failure")


@pytest.fixture(autouse=True)
def _fresh_memory_storage() -> None:
    reset_memory_object_storage()


async def _pair(session: AsyncSession, ctx: dict) -> tuple[uuid.UUID, uuid.UUID]:
    normal = await seed_manual_evidence_execution(
        session,
        requester_id=ctx["user_id"],
        tool_id=ctx["tool_id"],
        version_id=ctx["version_id"],
        server_id=ctx["server_id"],
        execution_status=ExecutionStatus.SUCCEEDED.value,
    )
    error = await seed_manual_evidence_execution(
        session,
        requester_id=ctx["user_id"],
        tool_id=ctx["tool_id"],
        version_id=ctx["version_id"],
        server_id=ctx["server_id"],
        execution_status=ExecutionStatus.FAILED.value,
        attempt_error_code="MCP_NETWORK_ERROR",
        attempt_error_layer="NETWORK",
        tool_call_status=ToolCallNormalizedStatus.FAILED.value,
    )
    await session.commit()
    return normal.id, error.id


@pytest.mark.asyncio
async def test_verified_success_derives_evidence_and_verified_by(
    db_session: AsyncSession,
) -> None:
    store = InMemoryObjectStorage()
    ctx = await seed_verification_context(db_session)
    normal_id, error_id = await _pair(db_session, ctx)

    svc = MCPToolVerificationService(db_session, object_storage=store)
    row = await svc.create(
        ctx["tool_id"],
        ctx["version_id"],
        ToolVerificationCreate(
            status=ToolVerificationStatus.VERIFIED,
            criteria_version="tool-verification-v1",
            test_execution_id=normal_id,
            error_test_execution_id=error_id,
        ),
        verified_by=ctx["user_id"],
    )
    assert row.status == ToolVerificationStatus.VERIFIED.value
    assert row.verified_by == ctx["user_id"]
    assert row.evidence_blob_id is not None
    assert row.result_summary is not None
    assert row.result_summary["schema_valid"] is True
    assert row.result_summary["normal_call_passed"] is True
    assert row.result_summary["error_handling_checked"] is True
    assert row.result_summary["error_test_execution_id"] == str(error_id)

    key = verification_evidence_object_key(row.evidence_blob_id)
    bucket = get_settings().object_storage_bucket
    assert store.has_object(bucket=bucket, object_key=key)
    report = json.loads(await store.get_bytes(bucket=bucket, object_key=key))
    assert report["schema_version"] == "tool-verification-evidence.v1"
    assert report["verified_by"] == str(ctx["user_id"])
    assert report["tool_id"] == str(ctx["tool_id"])
    assert report["tool_version_id"] == str(ctx["version_id"])
    assert report["server_id"] == str(ctx["server_id"])
    assert report["connection_check"]["id"] == str(ctx["check_id"])
    assert report["discovery"]["id"] == str(ctx["discovery_id"])
    assert report["normal_test"]["execution_id"] == str(normal_id)
    assert report["error_test"]["execution_id"] == str(error_id)
    assert "arguments" not in json.dumps(report)
    assert await svc.has_effective_current_verification(ctx["tool_id"]) is True
    assert await svc.count_tools_with_effective_current_verification() == 1


@pytest.mark.asyncio
async def test_verified_rejects_client_evidence_fields(db_session: AsyncSession) -> None:
    ctx = await seed_verification_context(db_session)
    normal_id, error_id = await _pair(db_session, ctx)
    svc = MCPToolVerificationService(db_session, object_storage=InMemoryObjectStorage())

    with pytest.raises(AppError) as blob_exc:
        await svc.create(
            ctx["tool_id"],
            ctx["version_id"],
            ToolVerificationCreate(
                status=ToolVerificationStatus.VERIFIED,
                criteria_version="tool-verification-v1",
                test_execution_id=normal_id,
                error_test_execution_id=error_id,
                evidence_blob_id=uuid.uuid4(),
            ),
            verified_by=ctx["user_id"],
        )
    assert blob_exc.value.status_code == 422

    with pytest.raises(AppError) as summary_exc:
        await svc.create(
            ctx["tool_id"],
            ctx["version_id"],
            ToolVerificationCreate(
                status=ToolVerificationStatus.VERIFIED,
                criteria_version="tool-verification-v1",
                test_execution_id=normal_id,
                error_test_execution_id=error_id,
                result_summary={"schema_valid": True},
            ),
            verified_by=ctx["user_id"],
        )
    assert summary_exc.value.status_code == 422


@pytest.mark.asyncio
async def test_verified_rejects_random_and_wrong_lineage(db_session: AsyncSession) -> None:
    ctx = await seed_verification_context(db_session)
    normal_id, error_id = await _pair(db_session, ctx)
    svc = MCPToolVerificationService(db_session, object_storage=InMemoryObjectStorage())

    with pytest.raises(AppError) as missing:
        await svc.create(
            ctx["tool_id"],
            ctx["version_id"],
            ToolVerificationCreate(
                status=ToolVerificationStatus.VERIFIED,
                criteria_version="tool-verification-v1",
                test_execution_id=uuid.uuid4(),
                error_test_execution_id=error_id,
            ),
            verified_by=ctx["user_id"],
        )
    assert missing.value.status_code == 422

    agent = await seed_manual_evidence_execution(
        db_session,
        requester_id=ctx["user_id"],
        tool_id=ctx["tool_id"],
        version_id=ctx["version_id"],
        server_id=ctx["server_id"],
        execution_status=ExecutionStatus.SUCCEEDED.value,
        source_type=ExecutionSourceType.AGENT_REQUEST.value,
    )
    await db_session.commit()
    with pytest.raises(AppError) as agent_exc:
        await svc.create(
            ctx["tool_id"],
            ctx["version_id"],
            ToolVerificationCreate(
                status=ToolVerificationStatus.VERIFIED,
                criteria_version="tool-verification-v1",
                test_execution_id=agent.id,
                error_test_execution_id=error_id,
            ),
            verified_by=ctx["user_id"],
        )
    assert agent_exc.value.status_code == 409

    workflow = await seed_manual_evidence_execution(
        db_session,
        requester_id=ctx["user_id"],
        tool_id=ctx["tool_id"],
        version_id=ctx["version_id"],
        server_id=ctx["server_id"],
        execution_status=ExecutionStatus.SUCCEEDED.value,
        source_type=ExecutionSourceType.WORKFLOW_VERSION.value,
    )
    await db_session.commit()
    with pytest.raises(AppError) as wf_exc:
        await svc.create(
            ctx["tool_id"],
            ctx["version_id"],
            ToolVerificationCreate(
                status=ToolVerificationStatus.VERIFIED,
                criteria_version="tool-verification-v1",
                test_execution_id=workflow.id,
                error_test_execution_id=error_id,
            ),
            verified_by=ctx["user_id"],
        )
    assert wf_exc.value.status_code == 409

    other = await seed_verification_context(db_session)
    wrong_version_exec = await seed_manual_evidence_execution(
        db_session,
        requester_id=ctx["user_id"],
        tool_id=ctx["tool_id"],
        version_id=ctx["version_id"],
        server_id=ctx["server_id"],
        execution_status=ExecutionStatus.SUCCEEDED.value,
        plan_tool_id=other["tool_id"],
        plan_version_id=other["version_id"],
    )
    await db_session.commit()
    with pytest.raises(AppError) as wrong_exc:
        await svc.create(
            ctx["tool_id"],
            ctx["version_id"],
            ToolVerificationCreate(
                status=ToolVerificationStatus.VERIFIED,
                criteria_version="tool-verification-v1",
                test_execution_id=wrong_version_exec.id,
                error_test_execution_id=error_id,
            ),
            verified_by=ctx["user_id"],
        )
    assert wrong_exc.value.status_code == 409

    failed_normal = await seed_manual_evidence_execution(
        db_session,
        requester_id=ctx["user_id"],
        tool_id=ctx["tool_id"],
        version_id=ctx["version_id"],
        server_id=ctx["server_id"],
        execution_status=ExecutionStatus.FAILED.value,
        attempt_error_code="MCP_NETWORK_ERROR",
        attempt_error_layer="NETWORK",
        tool_call_status=ToolCallNormalizedStatus.FAILED.value,
    )
    await db_session.commit()
    with pytest.raises(AppError) as failed_exc:
        await svc.create(
            ctx["tool_id"],
            ctx["version_id"],
            ToolVerificationCreate(
                status=ToolVerificationStatus.VERIFIED,
                criteria_version="tool-verification-v1",
                test_execution_id=failed_normal.id,
                error_test_execution_id=error_id,
            ),
            verified_by=ctx["user_id"],
        )
    assert failed_exc.value.status_code == 409


@pytest.mark.asyncio
async def test_verified_rejects_missing_or_weak_error_evidence(
    db_session: AsyncSession,
) -> None:
    ctx = await seed_verification_context(db_session)
    normal = await seed_manual_evidence_execution(
        db_session,
        requester_id=ctx["user_id"],
        tool_id=ctx["tool_id"],
        version_id=ctx["version_id"],
        server_id=ctx["server_id"],
        execution_status=ExecutionStatus.SUCCEEDED.value,
    )
    await db_session.commit()
    svc = MCPToolVerificationService(db_session, object_storage=InMemoryObjectStorage())

    with pytest.raises(AppError) as missing_err:
        await svc.create(
            ctx["tool_id"],
            ctx["version_id"],
            ToolVerificationCreate(
                status=ToolVerificationStatus.VERIFIED,
                criteria_version="tool-verification-v1",
                test_execution_id=normal.id,
            ),
            verified_by=ctx["user_id"],
        )
    assert missing_err.value.status_code == 422

    weak = await seed_manual_evidence_execution(
        db_session,
        requester_id=ctx["user_id"],
        tool_id=ctx["tool_id"],
        version_id=ctx["version_id"],
        server_id=ctx["server_id"],
        execution_status=ExecutionStatus.FAILED.value,
        attempt_status=StepAttemptStatus.SUCCEEDED.value,
        tool_call_status=None,
    )
    await db_session.commit()
    with pytest.raises(AppError) as weak_exc:
        await svc.create(
            ctx["tool_id"],
            ctx["version_id"],
            ToolVerificationCreate(
                status=ToolVerificationStatus.VERIFIED,
                criteria_version="tool-verification-v1",
                test_execution_id=normal.id,
                error_test_execution_id=weak.id,
            ),
            verified_by=ctx["user_id"],
        )
    assert weak_exc.value.status_code == 409

    unknown = await seed_manual_evidence_execution(
        db_session,
        requester_id=ctx["user_id"],
        tool_id=ctx["tool_id"],
        version_id=ctx["version_id"],
        server_id=ctx["server_id"],
        execution_status=ExecutionStatus.FAILED.value,
        step_status=StepStatus.UNKNOWN_OUTCOME.value,
        attempt_status=StepAttemptStatus.UNKNOWN_OUTCOME.value,
        tool_call_status=ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value,
    )
    await db_session.commit()
    with pytest.raises(AppError) as unk_exc:
        await svc.create(
            ctx["tool_id"],
            ctx["version_id"],
            ToolVerificationCreate(
                status=ToolVerificationStatus.VERIFIED,
                criteria_version="tool-verification-v1",
                test_execution_id=normal.id,
                error_test_execution_id=unknown.id,
            ),
            verified_by=ctx["user_id"],
        )
    assert unk_exc.value.status_code == 409

    other = await seed_verification_context(db_session)
    wrong_err = await seed_manual_evidence_execution(
        db_session,
        requester_id=ctx["user_id"],
        tool_id=ctx["tool_id"],
        version_id=ctx["version_id"],
        server_id=ctx["server_id"],
        execution_status=ExecutionStatus.FAILED.value,
        attempt_error_code="MCP_NETWORK_ERROR",
        attempt_error_layer="NETWORK",
        tool_call_status=ToolCallNormalizedStatus.FAILED.value,
        plan_tool_id=other["tool_id"],
        plan_version_id=other["version_id"],
    )
    await db_session.commit()
    with pytest.raises(AppError) as wrong_err_exc:
        await svc.create(
            ctx["tool_id"],
            ctx["version_id"],
            ToolVerificationCreate(
                status=ToolVerificationStatus.VERIFIED,
                criteria_version="tool-verification-v1",
                test_execution_id=normal.id,
                error_test_execution_id=wrong_err.id,
            ),
            verified_by=ctx["user_id"],
        )
    assert wrong_err_exc.value.status_code == 409


@pytest.mark.asyncio
async def test_verified_rejects_invalid_version_check_discovery_metadata_expires(
    db_session: AsyncSession,
) -> None:
    store = InMemoryObjectStorage()
    svc = MCPToolVerificationService(db_session, object_storage=store)

    invalid = await seed_verification_context(
        db_session, validation_status=ToolVersionValidationStatus.INVALID.value
    )
    n1, e1 = await _pair(db_session, invalid)
    with pytest.raises(AppError) as inv:
        await svc.create(
            invalid["tool_id"],
            invalid["version_id"],
            ToolVerificationCreate(
                status=ToolVerificationStatus.VERIFIED,
                criteria_version="tool-verification-v1",
                test_execution_id=n1,
                error_test_execution_id=e1,
            ),
            verified_by=invalid["user_id"],
        )
    assert inv.value.status_code == 409

    no_check = await seed_verification_context(db_session, with_check=False)
    n2, e2 = await _pair(db_session, no_check)
    with pytest.raises(AppError) as chk:
        await svc.create(
            no_check["tool_id"],
            no_check["version_id"],
            ToolVerificationCreate(
                status=ToolVerificationStatus.VERIFIED,
                criteria_version="tool-verification-v1",
                test_execution_id=n2,
                error_test_execution_id=e2,
            ),
            verified_by=no_check["user_id"],
        )
    assert chk.value.status_code == 409

    no_disc = await seed_verification_context(db_session, with_discovery=False)
    n3, e3 = await _pair(db_session, no_disc)
    with pytest.raises(AppError) as disc:
        await svc.create(
            no_disc["tool_id"],
            no_disc["version_id"],
            ToolVerificationCreate(
                status=ToolVerificationStatus.VERIFIED,
                criteria_version="tool-verification-v1",
                test_execution_id=n3,
                error_test_execution_id=e3,
            ),
            verified_by=no_disc["user_id"],
        )
    assert disc.value.status_code == 409

    no_meta = await seed_verification_context(db_session, with_protocol_metadata=False)
    n4, e4 = await _pair(db_session, no_meta)
    with pytest.raises(AppError) as meta:
        await svc.create(
            no_meta["tool_id"],
            no_meta["version_id"],
            ToolVerificationCreate(
                status=ToolVerificationStatus.VERIFIED,
                criteria_version="tool-verification-v1",
                test_execution_id=n4,
                error_test_execution_id=e4,
            ),
            verified_by=no_meta["user_id"],
        )
    assert meta.value.status_code == 409

    ok = await seed_verification_context(db_session)
    n5, e5 = await _pair(db_session, ok)
    with pytest.raises(AppError) as exp:
        await svc.create(
            ok["tool_id"],
            ok["version_id"],
            ToolVerificationCreate(
                status=ToolVerificationStatus.VERIFIED,
                criteria_version="tool-verification-v1",
                test_execution_id=n5,
                error_test_execution_id=e5,
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            ),
            verified_by=ok["user_id"],
        )
    assert exp.value.status_code == 422


@pytest.mark.asyncio
async def test_object_storage_failure_creates_no_verified_row(
    db_session: AsyncSession,
) -> None:
    ctx = await seed_verification_context(db_session)
    normal_id, error_id = await _pair(db_session, ctx)
    svc = MCPToolVerificationService(db_session, object_storage=_FailingObjectStorage())
    with pytest.raises(AppError) as exc:
        await svc.create(
            ctx["tool_id"],
            ctx["version_id"],
            ToolVerificationCreate(
                status=ToolVerificationStatus.VERIFIED,
                criteria_version="tool-verification-v1",
                test_execution_id=normal_id,
                error_test_execution_id=error_id,
            ),
            verified_by=ctx["user_id"],
        )
    assert exc.value.status_code == 409
    items, total = await MCPToolVerificationRepository(db_session).list_for_version(
        mcp_tool_version_id=ctx["version_id"], page=1, page_size=20
    )
    assert total == 0
    assert items == []


@pytest.mark.asyncio
async def test_verified_requires_auth_grant(db_session: AsyncSession) -> None:
    ctx = await seed_verification_context(db_session, setup_auth=False)
    normal_id, error_id = await _pair(db_session, ctx)
    svc = MCPToolVerificationService(db_session, object_storage=InMemoryObjectStorage())
    with pytest.raises(AppError) as exc:
        await svc.create(
            ctx["tool_id"],
            ctx["version_id"],
            ToolVerificationCreate(
                status=ToolVerificationStatus.VERIFIED,
                criteria_version="tool-verification-v1",
                test_execution_id=normal_id,
                error_test_execution_id=error_id,
            ),
            verified_by=ctx["user_id"],
        )
    assert exc.value.code == "FORBIDDEN"


@pytest.mark.asyncio
async def test_pending_failed_unchanged_and_no_version_inheritance(
    db_session: AsyncSession,
) -> None:
    ctx = await seed_verification_context(db_session)
    svc = MCPToolVerificationService(db_session, object_storage=InMemoryObjectStorage())
    pending = await svc.create(
        ctx["tool_id"],
        ctx["version_id"],
        ToolVerificationCreate(
            status=ToolVerificationStatus.PENDING,
            criteria_version="tool-verification-v1",
        ),
        verified_by=ctx["user_id"],
    )
    assert pending.status == "PENDING"
    assert pending.verified_by is None
    assert pending.evidence_blob_id is None

    normal_id, error_id = await _pair(db_session, ctx)
    verified = await svc.create(
        ctx["tool_id"],
        ctx["version_id"],
        ToolVerificationCreate(
            status=ToolVerificationStatus.VERIFIED,
            criteria_version="tool-verification-v1",
            test_execution_id=normal_id,
            error_test_execution_id=error_id,
        ),
        verified_by=ctx["user_id"],
    )
    assert verified.status == "VERIFIED"

    tools = MCPToolRepository(db_session)
    v2 = await tools.create_version(
        mcp_tool_id=ctx["tool_id"],
        version_no=2,
        content_hash=uuid.uuid4().hex,
        validation_status=ToolVersionValidationStatus.VALID.value,
    )
    tool = await tools.get(ctx["tool_id"])
    assert tool is not None
    tool.current_version_id = v2.id
    await db_session.commit()

    items, total = await svc.list(ctx["tool_id"], v2.id, page=1, page_size=20)
    assert total == 0
    assert items == []
    assert await svc.has_effective_current_verification(ctx["tool_id"]) is False
