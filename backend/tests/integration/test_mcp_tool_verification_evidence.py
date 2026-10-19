"""PostgreSQL: FNC-TOOL-006 VERIFIED evidence report vertical slice."""

from __future__ import annotations

import json
import uuid

import pytest
from app.core.config import get_settings
from app.domain.enums import ExecutionStatus, ToolCallNormalizedStatus, ToolVerificationStatus
from app.infrastructure.object_storage import InMemoryObjectStorage
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_verification import MCPToolVerificationRepository
from app.schemas.mcp_tool import ToolVerificationCreate
from app.services.mcp_tool_verification import (
    MCPToolVerificationService,
    verification_evidence_object_key,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.helpers.tool_verification import (
    seed_manual_evidence_execution,
    seed_verification_context,
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_verified_evidence_report_and_kpi(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = InMemoryObjectStorage()

    async with integration_session_factory() as session:
        ctx = await seed_verification_context(session)
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
            execution_status=ExecutionStatus.TIMED_OUT.value,
            attempt_error_code="MCP_CONNECTION_TIMEOUT",
            attempt_error_layer="TIMEOUT",
            tool_call_status=ToolCallNormalizedStatus.TIMED_OUT.value,
        )
        await session.commit()
        tool_id = ctx["tool_id"]
        version_id = ctx["version_id"]
        user_id = ctx["user_id"]
        normal_id = normal.id
        error_id = error.id

    async with integration_session_factory() as session:
        svc = MCPToolVerificationService(session, object_storage=store)
        row = await svc.create(
            tool_id,
            version_id,
            ToolVerificationCreate(
                status=ToolVerificationStatus.VERIFIED,
                criteria_version="tool-verification-v1",
                test_execution_id=normal_id,
                error_test_execution_id=error_id,
            ),
            verified_by=user_id,
        )
        assert row.status == ToolVerificationStatus.VERIFIED.value
        assert row.verified_by == user_id
        assert row.evidence_blob_id is not None
        evidence_blob_id = row.evidence_blob_id
        verification_id = row.id
        assert row.result_summary is not None
        assert row.result_summary["schema_valid"] is True
        assert row.result_summary["normal_call_passed"] is True
        assert row.result_summary["error_handling_checked"] is True
        assert await svc.has_effective_current_verification(tool_id) is True
        assert await svc.count_tools_with_effective_current_verification() == 1

    bucket = get_settings().object_storage_bucket
    key = verification_evidence_object_key(evidence_blob_id)
    assert store.has_object(bucket=bucket, object_key=key)
    report = json.loads(await store.get_bytes(bucket=bucket, object_key=key))
    assert report["schema_version"] == "tool-verification-evidence.v1"
    assert report["tool_id"] == str(tool_id)
    assert report["tool_version_id"] == str(version_id)
    assert report["verified_by"] == str(user_id)
    assert report["normal_test"]["execution_id"] == str(normal_id)
    assert report["error_test"]["execution_id"] == str(error_id)
    assert report["connection_check"]["id"]
    assert report["discovery"]["id"]
    assert report["tool_version_validation_status"] == "VALID"

    async with integration_session_factory() as session:
        persisted = await MCPToolVerificationRepository(session).get(verification_id)
        assert persisted is not None
        assert persisted.status == ToolVerificationStatus.VERIFIED.value
        assert persisted.evidence_blob_id == evidence_blob_id
        assert persisted.verified_by == user_id

        # New ToolVersion must not inherit VERIFIED.
        tools = MCPToolRepository(session)
        v2 = await tools.create_version(
            mcp_tool_id=tool_id,
            version_no=2,
            content_hash=uuid.uuid4().hex,
            validation_status="VALID",
        )
        tool = await tools.get(tool_id)
        assert tool is not None
        tool.current_version_id = v2.id
        await session.commit()
        assert (
            await MCPToolVerificationRepository(session).has_effective_verified(v2.id)
            is False
        )
        assert (
            await MCPToolVerificationRepository(
                session
            ).count_tools_with_effective_current_verification()
            == 0
        )
