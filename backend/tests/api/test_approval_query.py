"""API tests for GET /approvals and GET /approvals/{id}."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.domain.enums import ApprovalStatus
from app.repositories.approval_request import ApprovalRequestRepository
from app.repositories.role import (
    PermissionRepository,
    RolePermissionRepository,
    RoleRepository,
    UserRoleRepository,
)
from app.repositories.user import UserRepository
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.api.test_approval_decisions import (
    APPROVALS,
    _grant_decide,
    _login_as,
)
from tests.api.test_auth_session import PASSWORD, _provision_user
from tests.unit.test_approval_decision_resume import _create_approver, _enter_waiting
from tests.unit.test_execution_creation import _install_no_side_effects


@pytest.mark.asyncio
async def test_get_approvals_default_inbox(
    db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _execution_id, approval_id, requester_id = await _enter_waiting(
        db_session_factory,
        monkeypatch,
        allow_self_approval=True,
        approver_scope={},
    )
    async with db_session_factory() as session:
        await _grant_decide(session, requester_id)
        await session.commit()

    await _login_as(db_client, db_session_factory, user_id=requester_id)
    resp = await db_client.get(APPROVALS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["page"] == 1
    assert body["page_size"] == 20
    assert body["has_next"] is False
    item = body["items"][0]
    assert item["id"] == str(approval_id)
    assert item["can_decide"] is True
    assert item["status"] == ApprovalStatus.PENDING.value
    for forbidden in (
        "context_snapshot",
        "context_hash",
        "approval_scope",
        "plan_hash",
        "secret_id",
    ):
        assert forbidden not in item


@pytest.mark.asyncio
async def test_get_approvals_missing_permission_403(
    db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _execution_id, approval_id, requester_id = await _enter_waiting(
        db_session_factory, monkeypatch, allow_self_approval=True
    )
    async with db_session_factory() as session:
        from app.auth.passwords import hash_password

        await UserRepository(session).set_password_hash(
            requester_id, hash_password(PASSWORD)
        )
        await session.commit()

    await _login_as(db_client, db_session_factory, user_id=requester_id)
    list_resp = await db_client.get(APPROVALS)
    assert list_resp.status_code == 403
    assert list_resp.json()["error"]["code"] == "AUTH_FORBIDDEN"

    detail_resp = await db_client.get(f"{APPROVALS}/{approval_id}")
    assert detail_resp.status_code == 403
    assert detail_resp.json()["error"]["code"] == "AUTH_FORBIDDEN"


@pytest.mark.asyncio
async def test_get_approval_detail_masking_and_idor(
    db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _execution_id, approval_id, _requester_id = await _enter_waiting(
        db_session_factory,
        monkeypatch,
        allow_self_approval=False,
        approver_scope={"role_codes": ["ROLE_DETAIL_A"]},
    )
    async with db_session_factory() as session:
        match = await _create_approver(session, role_code="ROLE_DETAIL_A")
        mismatch = await _create_approver(session, role_code="ROLE_DETAIL_B")
        from app.auth.passwords import hash_password
        from app.approval.context import compute_approval_context_hash

        await UserRepository(session).set_password_hash(
            match, hash_password(PASSWORD)
        )
        await UserRepository(session).set_password_hash(
            mismatch, hash_password(PASSWORD)
        )
        request = await ApprovalRequestRepository(session).get(approval_id)
        assert request is not None
        snap = dict(request.context_snapshot)
        resolved = dict(snap.get("resolved_input") or {})
        resolved["credential"] = {
            "kind": "SECRET_REF",
            "secret_id": "should-not-appear",
        }
        snap["resolved_input"] = resolved
        request.context_snapshot = snap
        request.context_hash = compute_approval_context_hash(snap)
        await session.commit()

    await _login_as(db_client, db_session_factory, user_id=match)
    ok = await db_client.get(f"{APPROVALS}/{approval_id}")
    assert ok.status_code == 200, ok.text
    detail = ok.json()
    assert "context_hash" not in detail
    assert "context_snapshot" not in detail
    assert detail["safe_context"]["resolved_input"]["credential"] == {
        "kind": "SECRET_REF",
        "masked": True,
    }
    assert "secret_id" not in detail["safe_context"]["resolved_input"]["credential"]
    assert "decisions" in detail

    await _login_as(db_client, db_session_factory, user_id=mismatch)
    idor = await db_client.get(f"{APPROVALS}/{approval_id}")
    assert idor.status_code == 404
    assert idor.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_get_approvals_invalid_sort_and_status(
    db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _execution_id, _approval_id, requester_id = await _enter_waiting(
        db_session_factory, monkeypatch, allow_self_approval=True
    )
    async with db_session_factory() as session:
        await _grant_decide(session, requester_id)
        await session.commit()

    await _login_as(db_client, db_session_factory, user_id=requester_id)
    bad_sort = await db_client.get(APPROVALS, params={"sort": "updated_at"})
    assert bad_sort.status_code == 422

    bad_status = await db_client.get(APPROVALS, params={"status": "WAITING"})
    assert bad_status.status_code == 422


@pytest.mark.asyncio
async def test_get_approvals_sort_expires_at_default(
    db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids: list[uuid.UUID] = []
    requester_id: uuid.UUID | None = None
    for _ in range(2):
        _e, aid, rid = await _enter_waiting(
            db_session_factory,
            monkeypatch,
            allow_self_approval=True,
            approver_scope={},
        )
        ids.append(aid)
        requester_id = rid

    assert requester_id is not None
    async with db_session_factory() as session:
        await _grant_decide(session, requester_id)
        # Make first expire sooner
        first = await ApprovalRequestRepository(session).get(ids[0])
        second = await ApprovalRequestRepository(session).get(ids[1])
        assert first is not None and second is not None
        now = datetime.now(UTC)
        first.expires_at = now + timedelta(minutes=10)
        second.expires_at = now + timedelta(minutes=60)
        await session.commit()

    await _login_as(db_client, db_session_factory, user_id=requester_id)
    resp = await db_client.get(APPROVALS)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) >= 2
    # Default expires_at ASC → sooner first
    assert items[0]["id"] == str(ids[0])
