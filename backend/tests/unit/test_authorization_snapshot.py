"""AuthorizationResolver single-snapshot TOCTOU regression tests."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.repositories.authorization import AuthorizationSnapshot
from app.schemas.auth import AuthorizationDecision
from app.services.authorization import AuthorizationResolver


@pytest.mark.asyncio
async def test_authorize_resource_uses_single_snapshot_query() -> None:
    session = MagicMock()
    resolver = AuthorizationResolver(session)
    snapshot = AuthorizationSnapshot(
        user_exists=True,
        user_active=True,
        permission_present=True,
        resource_grant_present=True,
    )
    resolver._authorization.get_resource_authorization_snapshot = AsyncMock(
        return_value=snapshot
    )
    resolver.has_permission = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("authorize_resource must not call has_permission")
    )
    resolver._grants.has_effective_grant = AsyncMock(
        side_effect=AssertionError(
            "authorize_resource must not call has_effective_grant"
        )
    )
    resolver._users.get = AsyncMock(
        side_effect=AssertionError("authorize_resource must not call users.get")
    )

    user_id = uuid.uuid4()
    resource_id = uuid.uuid4()
    decision = await resolver.authorize_resource(
        user_id, "mcp.tool.execute", "MCP_TOOL", resource_id
    )

    assert decision == AuthorizationDecision(allowed=True, reason_code="ALLOWED")
    resolver._authorization.get_resource_authorization_snapshot.assert_awaited_once_with(
        user_id,
        permission_code="mcp.tool.execute",
        resource_type="MCP_TOOL",
        resource_id=resource_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (
            AuthorizationSnapshot(False, False, False, False),
            "USER_NOT_FOUND",
        ),
        (
            AuthorizationSnapshot(True, False, True, True),
            "USER_NOT_ACTIVE",
        ),
        (
            AuthorizationSnapshot(True, True, False, True),
            "PERMISSION_MISSING",
        ),
        (
            AuthorizationSnapshot(True, True, True, False),
            "RESOURCE_GRANT_MISSING",
        ),
    ],
)
async def test_authorize_resource_reason_priority(
    snapshot: AuthorizationSnapshot, reason: str
) -> None:
    resolver = AuthorizationResolver(MagicMock())
    resolver._authorization.get_resource_authorization_snapshot = AsyncMock(
        return_value=snapshot
    )
    decision = await resolver.authorize_resource(
        uuid.uuid4(), "mcp.tool.execute", "MCP_TOOL", uuid.uuid4()
    )
    assert decision.allowed is False
    assert decision.reason_code == reason
