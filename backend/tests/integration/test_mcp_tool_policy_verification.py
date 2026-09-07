"""PostgreSQL integration tests for Tool Policy / Verification slice."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from app.domain.enums import (
    ApprovalPolicyStatus,
    ToolVerificationStatus,
    ToolVersionValidationStatus,
)
from app.models.mcp import MCPTool
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.mcp_discovery import MCPDiscoveryRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.repositories.mcp_tool_verification import MCPToolVerificationRepository
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.integration
def test_alembic_downgrade_upgrade_roundtrip(integration_database_url: str) -> None:
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    cfg = Config(os.path.join(backend_dir, "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", integration_database_url)
    os.environ["MCPFLOW_DATABASE_URL"] = integration_database_url
    from app.core.config import get_settings

    get_settings.cache_clear()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "20260904_0001")
    command.upgrade(cfg, "head")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tool_policy_unique_fk_and_checks(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        server = await MCPServerRepository(session).create(
            code=f"pol-{uuid.uuid4().hex[:8]}",
            name="Policy Server",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/mcp",
        )
        tool = await MCPToolRepository(session).create_tool(
            mcp_server_id=server.id,
            remote_name="echo",
        )
        approval = await ApprovalPolicyRepository(session).create(
            code=f"ap-{uuid.uuid4().hex[:8]}",
            name="Active",
            status=ApprovalPolicyStatus.ACTIVE,
        )
        await session.commit()
        tool_id = tool.id
        approval_id = approval.id

    async with integration_session_factory() as session:
        policies = MCPToolPolicyRepository(session)
        await policies.create(
            mcp_tool_id=tool_id,
            risk_class="READ_ONLY",
            requires_confirmation=False,
            requires_approval=False,
            approval_policy_id=None,
            timeout_ms=1000,
            max_attempts=1,
            backoff_policy=None,
            max_result_bytes=100,
            allow_auto_select=True,
            data_classification=None,
            policy_metadata=None,
        )
        await session.commit()

    async with integration_session_factory() as session:
        policies = MCPToolPolicyRepository(session)
        with pytest.raises(IntegrityError):
            await policies.create(
                mcp_tool_id=tool_id,
                risk_class="READ_ONLY",
                requires_confirmation=False,
                requires_approval=False,
                approval_policy_id=None,
                timeout_ms=1000,
                max_attempts=1,
                backoff_policy=None,
                max_result_bytes=100,
                allow_auto_select=True,
                data_classification=None,
                policy_metadata=None,
            )
            await session.commit()
        await session.rollback()

    async with integration_session_factory() as session:
        policies = MCPToolPolicyRepository(session)
        with pytest.raises(IntegrityError):
            await policies.create(
                mcp_tool_id=tool_id,
                risk_class="READ_ONLY",
                requires_confirmation=False,
                requires_approval=True,
                approval_policy_id=None,
                timeout_ms=1000,
                max_attempts=1,
                backoff_policy=None,
                max_result_bytes=100,
                allow_auto_select=True,
                data_classification=None,
                policy_metadata=None,
            )
            await session.commit()
        await session.rollback()

    async with integration_session_factory() as session:
        # Invalid risk_class CHECK
        from sqlalchemy import text

        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO mcp_tool_policies "
                    "(id, mcp_tool_id, risk_class, requires_confirmation, requires_approval, "
                    "timeout_ms, max_attempts, max_result_bytes, allow_auto_select, lock_version) "
                    "VALUES (:id, :tool_id, 'WRITE', false, false, 1, 1, 1, true, 1)"
                ),
                {"id": uuid.uuid4(), "tool_id": tool_id},
            )
            await session.commit()
        await session.rollback()

    # approval FK exists
    async with integration_session_factory() as session:
        assert await ApprovalPolicyRepository(session).get(approval_id) is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_verification_fk_check_and_kpi_helper(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        server = await MCPServerRepository(session).create(
            code=f"ver-{uuid.uuid4().hex[:8]}",
            name="Verification Server",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/mcp",
        )
        server.discovery_mode = "INFERRED_CURRENT"
        server.negotiated_protocol_version = "2026-07-28"
        tools = MCPToolRepository(session)
        tool = await tools.create_tool(mcp_server_id=server.id, remote_name="echo")
        v1 = await tools.create_version(
            mcp_tool_id=tool.id,
            version_no=1,
            content_hash="hash-v1",
            validation_status=ToolVersionValidationStatus.VALID,
        )
        tool.current_version_id = v1.id
        await session.commit()
        tool_id = tool.id
        v1_id = v1.id
        server_id = server.id

    async with integration_session_factory() as session:
        verifications = MCPToolVerificationRepository(session)
        assert await verifications.count_tools_with_effective_current_verification() == 0

        await verifications.create(
            mcp_tool_version_id=v1_id,
            status=ToolVerificationStatus.VERIFIED,
            criteria_version="tool-verification-v1",
            test_execution_id=uuid.uuid4(),
            evidence_blob_id=uuid.uuid4(),
            result_summary={
                "schema_valid": True,
                "normal_call_passed": True,
                "error_handling_checked": True,
            },
        )
        await session.commit()
        assert await verifications.count_tools_with_effective_current_verification() == 1
        assert await verifications.has_effective_verified(v1_id) is True

    async with integration_session_factory() as session:
        tools = MCPToolRepository(session)
        v2 = await tools.create_version(
            mcp_tool_id=tool_id,
            version_no=2,
            content_hash="hash-v2",
            validation_status=ToolVersionValidationStatus.VALID,
        )
        tool = await tools.get(tool_id)
        assert tool is not None
        tool.current_version_id = v2.id
        await session.commit()
        v2_id = v2.id

    async with integration_session_factory() as session:
        verifications = MCPToolVerificationRepository(session)
        assert await verifications.count_tools_with_effective_current_verification() == 0
        assert await verifications.has_effective_verified(v2_id) is False
        assert await verifications.has_effective_verified(v1_id) is True

        await verifications.create(
            mcp_tool_version_id=v2_id,
            status=ToolVerificationStatus.VERIFIED,
            criteria_version="tool-verification-v1",
            test_execution_id=uuid.uuid4(),
            evidence_blob_id=uuid.uuid4(),
            result_summary={"ok": True},
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
        await session.commit()
        assert await verifications.count_tools_with_effective_current_verification() == 1

        # Expired evidence does not count
        await verifications.create(
            mcp_tool_version_id=v2_id,
            status=ToolVerificationStatus.VERIFIED,
            criteria_version="tool-verification-v1",
            test_execution_id=uuid.uuid4(),
            evidence_blob_id=uuid.uuid4(),
            result_summary={"ok": True},
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        await session.commit()
        # Still 1 because non-expired VERIFIED exists
        assert await verifications.count_tools_with_effective_current_verification() == 1

    # Status CHECK
    async with integration_session_factory() as session:
        from sqlalchemy import text

        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO mcp_tool_verifications "
                    "(id, mcp_tool_version_id, status, criteria_version) "
                    "VALUES (:id, :vid, 'PASSED', 'v1')"
                ),
                {"id": uuid.uuid4(), "vid": v2_id},
            )
            await session.commit()
        await session.rollback()

    # discovery helper still usable
    async with integration_session_factory() as session:
        assert await MCPDiscoveryRepository(session).has_succeeded_check(server_id) is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_tool_atomic_patch(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        server = await MCPServerRepository(session).create(
            code=f"cas-{uuid.uuid4().hex[:8]}",
            name="CAS Server",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/mcp",
        )
        tool = await MCPToolRepository(session).create_tool(
            mcp_server_id=server.id,
            remote_name="echo",
            display_name="Original",
        )
        await session.commit()
        tool_id = tool.id
        assert tool.lock_version == 1

    async def patch_name(name: str) -> MCPTool | None:
        async with integration_session_factory() as session:
            updated = await MCPToolRepository(session).update_atomic(
                tool_id,
                expected_lock_version=1,
                display_name=name,
            )
            if updated is not None:
                await session.commit()
            return updated

    winner_a, winner_b = await asyncio.gather(
        patch_name("Winner A"),
        patch_name("Winner B"),
    )
    winners = [row for row in (winner_a, winner_b) if row is not None]
    losers = [row for row in (winner_a, winner_b) if row is None]
    assert len(winners) == 1
    assert len(losers) == 1
    assert winners[0].lock_version == 2

    async with integration_session_factory() as session:
        final = await MCPToolRepository(session).get(tool_id)
        assert final is not None
        assert final.lock_version == 2
        assert final.display_name in {"Winner A", "Winner B"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_tool_deactivate_cas(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.core.errors import AppError
    from app.domain.enums import MCPToolStatus
    from app.services.mcp_tool import MCPToolService

    async with integration_session_factory() as session:
        server = await MCPServerRepository(session).create(
            code=f"deact-{uuid.uuid4().hex[:8]}",
            name="Deactivate CAS Server",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/mcp",
        )
        tools = MCPToolRepository(session)
        tool = await tools.create_tool(
            mcp_server_id=server.id,
            remote_name="echo",
            status=MCPToolStatus.ACTIVE,
        )
        version = await tools.create_version(
            mcp_tool_id=tool.id,
            version_no=1,
            content_hash="hash-active",
            validation_status=ToolVersionValidationStatus.VALID,
        )
        tool.current_version_id = version.id
        await session.commit()
        tool_id = tool.id
        assert tool.lock_version == 1

    async def deactivate_once() -> tuple[str, int | None]:
        async with integration_session_factory() as session:
            service = MCPToolService(session)
            try:
                row = await service.deactivate(tool_id, expected_lock_version=1)
                return ("ok", int(row.lock_version))
            except AppError as exc:
                return (exc.code, None)

    result_a, result_b = await asyncio.gather(deactivate_once(), deactivate_once())
    codes = sorted(code for code, _ in (result_a, result_b))
    assert codes == ["RESOURCE_VERSION_CONFLICT", "ok"]
    success_locks = [lock for code, lock in (result_a, result_b) if code == "ok"]
    assert success_locks == [2]

    async with integration_session_factory() as session:
        final = await MCPToolRepository(session).get(tool_id)
        assert final is not None
        assert final.status == MCPToolStatus.INACTIVE
        assert final.lock_version == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_policy_first_create(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.core.errors import AppError
    from app.models.mcp import MCPToolPolicy
    from app.schemas.mcp_tool import MCPToolPolicyPut
    from app.services.mcp_tool_policy import MCPToolPolicyService
    from sqlalchemy import func, select

    async with integration_session_factory() as session:
        server = await MCPServerRepository(session).create(
            code=f"pcreate-{uuid.uuid4().hex[:8]}",
            name="Policy Create Server",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/mcp",
        )
        tool = await MCPToolRepository(session).create_tool(
            mcp_server_id=server.id,
            remote_name="echo",
        )
        await session.commit()
        tool_id = tool.id

    payload = MCPToolPolicyPut(
        risk_class="READ_ONLY",
        requires_confirmation=False,
        requires_approval=False,
        timeout_ms=1000,
        max_attempts=1,
        max_result_bytes=100,
        allow_auto_select=True,
    )

    async def put_once() -> tuple[str, int | None]:
        async with integration_session_factory() as session:
            service = MCPToolPolicyService(session)
            try:
                row = await service.put(
                    tool_id,
                    payload,
                    expected_lock_version=None,
                )
                return ("ok", int(row.lock_version))
            except AppError as exc:
                return (exc.code, None)

    result_a, result_b = await asyncio.gather(put_once(), put_once())
    codes = sorted(code for code, _ in (result_a, result_b))
    assert codes == ["RESOURCE_CONFLICT", "ok"]
    success_locks = [lock for code, lock in (result_a, result_b) if code == "ok"]
    assert success_locks == [1]

    async with integration_session_factory() as session:
        count = int(
            (
                await session.execute(
                    select(func.count()).select_from(MCPToolPolicy).where(
                        MCPToolPolicy.mcp_tool_id == tool_id
                    )
                )
            ).scalar_one()
        )
        assert count == 1
        policy = await MCPToolPolicyRepository(session).get_by_tool_id(tool_id)
        assert policy is not None
        assert policy.lock_version == 1
