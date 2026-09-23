"""Unit tests for Approval query / pending inbox (A–P foundation)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.approval.decision import ApprovalDecisionService
from app.approval.query import (
    ApprovalQueryService,
    mask_resolved_input,
    parse_approval_sort,
    project_safe_context,
)
from app.core.errors import AppError
from app.domain.enums import ApprovalStatus, UserStatus
from app.repositories.approval_decision import ApprovalDecisionRepository
from app.repositories.approval_request import ApprovalRequestRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.outbox import OutboxRepository
from app.repositories.role import (
    PermissionRepository,
    RolePermissionRepository,
    RoleRepository,
    UserRoleRepository,
)
from app.repositories.user import UserRepository
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.test_approval_decision_resume import (
    _create_approver,
    _enter_waiting,
    _grant_decide,
)


def test_parse_approval_sort_default_and_variants() -> None:
    assert parse_approval_sort("expires_at") == ("expires_at", "asc")
    assert parse_approval_sort("-expires_at") == ("expires_at", "desc")
    assert parse_approval_sort("requested_at") == ("requested_at", "asc")
    assert parse_approval_sort("-requested_at") == ("requested_at", "desc")
    with pytest.raises(AppError) as exc:
        parse_approval_sort("updated_at")
    assert exc.value.status_code == 422


def test_mask_secret_ref_and_safe_context() -> None:
    masked = mask_resolved_input(
        {
            "token": {"kind": "SECRET_REF", "secret_id": "sec-1"},
            "name": "ok",
        }
    )
    assert masked == {
        "token": {"kind": "SECRET_REF", "masked": True},
        "name": "ok",
    }
    assert "secret_id" not in masked["token"]
    projected = project_safe_context(
        {
            "step_key": "s1",
            "mcp_tool_version_id": str(uuid.uuid4()),
            "risk_class": "DESTRUCTIVE",
            "resolved_input": {
                "token": {"kind": "SECRET_REF", "secret_id": "sec-1"},
            },
        }
    )
    assert set(projected.keys()) == {
        "step_key",
        "mcp_tool_version_id",
        "risk_class",
        "resolved_input",
    }
    assert projected["resolved_input"]["token"] == {
        "kind": "SECRET_REF",
        "masked": True,
    }


@pytest.mark.asyncio
async def test_default_pending_inbox_eligible(
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
        await _grant_decide(session, user_id=requester_id)
        await session.commit()

        result = await ApprovalQueryService(session).list_for_actor(
            actor_user_id=requester_id
        )
        assert result.total == 1
        assert result.items[0].id == approval_id
        assert result.items[0].can_decide is True
        assert result.items[0].status == ApprovalStatus.PENDING.value
        dumped = result.items[0]
        assert not hasattr(dumped, "context_snapshot")
        assert not hasattr(dumped, "context_hash")
        assert not hasattr(dumped, "approval_scope")


@pytest.mark.asyncio
async def test_missing_permission_forbidden(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _execution_id, approval_id, requester_id = await _enter_waiting(
        db_session_factory, monkeypatch, allow_self_approval=True
    )
    async with db_session_factory() as session:
        # No approval.decide grant.
        with pytest.raises(AppError) as list_exc:
            await ApprovalQueryService(session).list_for_actor(
                actor_user_id=requester_id
            )
        assert list_exc.value.status_code == 403
        assert list_exc.value.code == "AUTH_FORBIDDEN"

        with pytest.raises(AppError) as detail_exc:
            await ApprovalQueryService(session).get_for_actor(
                approval_id=approval_id, actor_user_id=requester_id
            )
        assert detail_exc.value.status_code == 403
        assert detail_exc.value.code == "AUTH_FORBIDDEN"


@pytest.mark.asyncio
async def test_role_scope_match_and_mismatch(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _execution_id, approval_id, _requester_id = await _enter_waiting(
        db_session_factory,
        monkeypatch,
        allow_self_approval=False,
        approver_scope={"role_codes": ["ROLE_APPROVER_A"]},
    )
    async with db_session_factory() as session:
        match_id = await _create_approver(session, role_code="ROLE_APPROVER_A")
        mismatch_id = await _create_approver(session, role_code="ROLE_APPROVER_B")
        await session.commit()

        visible = await ApprovalQueryService(session).list_for_actor(
            actor_user_id=match_id
        )
        assert visible.total == 1
        assert visible.items[0].id == approval_id

        hidden = await ApprovalQueryService(session).list_for_actor(
            actor_user_id=mismatch_id
        )
        assert hidden.total == 0

        detail = await ApprovalQueryService(session).get_for_actor(
            approval_id=approval_id, actor_user_id=match_id
        )
        assert detail.item.id == approval_id

        with pytest.raises(AppError) as exc:
            await ApprovalQueryService(session).get_for_actor(
                approval_id=approval_id, actor_user_id=mismatch_id
            )
        assert exc.value.status_code == 404
        assert exc.value.code == "NOT_FOUND"


@pytest.mark.asyncio
async def test_null_and_empty_scope_with_decide(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for scope in (None, {}):
        _execution_id, approval_id, _requester_id = await _enter_waiting(
            db_session_factory,
            monkeypatch,
            allow_self_approval=False,
            approver_scope=scope,
        )
        async with db_session_factory() as session:
            actor = await _create_approver(session)
            await session.commit()
            result = await ApprovalQueryService(session).list_for_actor(
                actor_user_id=actor
            )
            assert any(item.id == approval_id for item in result.items)


@pytest.mark.asyncio
async def test_self_approval_false_hides_requester(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _execution_id, approval_id, requester_id = await _enter_waiting(
        db_session_factory,
        monkeypatch,
        allow_self_approval=False,
        approver_scope={},
    )
    async with db_session_factory() as session:
        await _grant_decide(session, user_id=requester_id)
        await session.commit()

        inbox = await ApprovalQueryService(session).list_for_actor(
            actor_user_id=requester_id
        )
        assert all(item.id != approval_id for item in inbox.items)

        with pytest.raises(AppError) as exc:
            await ApprovalQueryService(session).get_for_actor(
                approval_id=approval_id, actor_user_id=requester_id
            )
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_self_approval_true_shows_requester(
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
        await _grant_decide(session, user_id=requester_id)
        await session.commit()
        inbox = await ApprovalQueryService(session).list_for_actor(
            actor_user_id=requester_id
        )
        assert any(item.id == approval_id for item in inbox.items)


@pytest.mark.asyncio
async def test_already_decided_excluded_from_pending_inbox(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _execution_id, approval_id, _requester_id = await _enter_waiting(
        db_session_factory,
        monkeypatch,
        decision_mode="ALL",
        required_approvals=2,
        allow_self_approval=True,
        approver_scope={},
    )
    async with db_session_factory() as session:
        actor_a = await _create_approver(session)
        actor_b = await _create_approver(session)
        await session.commit()

        await ApprovalDecisionService(session).decide(
            approval_id=approval_id,
            actor_user_id=actor_a,
            decision="APPROVE",
        )
        # Decision service commits — reopen for queries.
    async with db_session_factory() as session:
        request = await ApprovalRequestRepository(session).get(approval_id)
        assert request is not None
        assert request.status == ApprovalStatus.PENDING.value

        inbox_a = await ApprovalQueryService(session).list_for_actor(
            actor_user_id=actor_a
        )
        assert all(item.id != approval_id for item in inbox_a.items)

        inbox_b = await ApprovalQueryService(session).list_for_actor(
            actor_user_id=actor_b
        )
        assert any(item.id == approval_id for item in inbox_b.items)
        assert inbox_b.items[0].can_decide is True

        # Detail still visible to A (already decided) with can_decide=false.
        detail_a = await ApprovalQueryService(session).get_for_actor(
            approval_id=approval_id, actor_user_id=actor_a
        )
        assert detail_a.item.can_decide is False
        assert detail_a.item.approve_count == 1


@pytest.mark.asyncio
async def test_expired_unswept_pending_absent_no_mutation(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _execution_id, approval_id, _requester_id = await _enter_waiting(
        db_session_factory,
        monkeypatch,
        allow_self_approval=True,
        approver_scope={},
        default_expiry_seconds=3600,
    )
    async with db_session_factory() as session:
        actor = await _create_approver(session)
        request = await ApprovalRequestRepository(session).get(approval_id)
        assert request is not None
        past = datetime.now(UTC) - timedelta(minutes=5)
        request.expires_at = past
        await session.commit()
        before_status = request.status
        before_resolved = request.resolved_at

        inbox = await ApprovalQueryService(session).list_for_actor(actor_user_id=actor)
        assert all(item.id != approval_id for item in inbox.items)

        # Detail still visible; GET must not expire.
        detail = await ApprovalQueryService(session).get_for_actor(
            approval_id=approval_id, actor_user_id=actor
        )
        assert detail.item.can_decide is False

        refreshed = await ApprovalRequestRepository(session).get(approval_id)
        assert refreshed is not None
        assert refreshed.status == before_status == ApprovalStatus.PENDING.value
        assert refreshed.resolved_at == before_resolved


@pytest.mark.asyncio
async def test_status_filter_and_execution_requested_by(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, approval_id, requester_id = await _enter_waiting(
        db_session_factory,
        monkeypatch,
        allow_self_approval=True,
        approver_scope={},
    )
    async with db_session_factory() as session:
        actor = await _create_approver(session)
        await session.commit()
        await ApprovalDecisionService(session).decide(
            approval_id=approval_id,
            actor_user_id=actor,
            decision="APPROVE",
        )

    async with db_session_factory() as session:
        approved = await ApprovalQueryService(session).list_for_actor(
            actor_user_id=actor, status="APPROVED"
        )
        assert any(item.id == approval_id for item in approved.items)

        pending = await ApprovalQueryService(session).list_for_actor(
            actor_user_id=actor, status="PENDING"
        )
        assert all(item.id != approval_id for item in pending.items)

        by_exec = await ApprovalQueryService(session).list_for_actor(
            actor_user_id=actor,
            status="APPROVED",
            execution_id=execution_id,
        )
        assert len(by_exec.items) == 1

        by_req = await ApprovalQueryService(session).list_for_actor(
            actor_user_id=actor,
            status="APPROVED",
            requested_by=requester_id,
        )
        assert len(by_req.items) == 1

        other_user = uuid.uuid4()
        empty = await ApprovalQueryService(session).list_for_actor(
            actor_user_id=actor,
            status="APPROVED",
            requested_by=other_user,
        )
        assert empty.total == 0


@pytest.mark.asyncio
async def test_pagination_after_authorization(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed eligible / wrong-role / self-forbidden / decided / expired → totals."""
    # Eligible open-scope requests
    eligible_ids: list[uuid.UUID] = []
    for _ in range(3):
        _e, aid, _r = await _enter_waiting(
            db_session_factory,
            monkeypatch,
            allow_self_approval=True,
            approver_scope={},
        )
        eligible_ids.append(aid)

    # Wrong role
    _e, wrong_role_id, _r = await _enter_waiting(
        db_session_factory,
        monkeypatch,
        allow_self_approval=False,
        approver_scope={"role_codes": ["ROLE_OTHER"]},
    )

    # Self-forbidden for actor who is requester — create separately
    _e, self_forbidden_id, requester_id = await _enter_waiting(
        db_session_factory,
        monkeypatch,
        allow_self_approval=False,
        approver_scope={},
    )

    # Already decided (still PENDING via ALL)
    _e, decided_id, _r = await _enter_waiting(
        db_session_factory,
        monkeypatch,
        decision_mode="ALL",
        required_approvals=2,
        allow_self_approval=True,
        approver_scope={},
    )

    # Expired unswept
    _e, expired_id, _r = await _enter_waiting(
        db_session_factory,
        monkeypatch,
        allow_self_approval=True,
        approver_scope={},
    )

    async with db_session_factory() as session:
        # Actor with decide + not the self-forbidden requester
        actor = await _create_approver(session)
        # Make requester the self-forbidden case's requester — actor is different
        # so self-forbidden is visible to actor (open scope). Need actor == requester
        # for self-forbidden exclusion — grant decide to requester and query as them.
        await _grant_decide(session, user_id=requester_id)

        # Expire one
        expired = await ApprovalRequestRepository(session).get(expired_id)
        assert expired is not None
        expired.expires_at = datetime.now(UTC) - timedelta(hours=1)
        await session.commit()

        await ApprovalDecisionService(session).decide(
            approval_id=decided_id,
            actor_user_id=actor,
            decision="APPROVE",
        )

    async with db_session_factory() as session:
        # As dedicated approver: eligible(3) + self_forbidden(open, other requester)
        # + wrong_role hidden + decided hidden + expired hidden
        # self_forbidden has open scope and requester != actor → visible
        result = await ApprovalQueryService(session).list_for_actor(
            actor_user_id=actor, page=1, page_size=2, sort="expires_at"
        )
        # eligible 3 + self_forbidden 1 = 4
        assert result.total == 4
        assert len(result.items) == 2
        assert result.has_next is True

        page2 = await ApprovalQueryService(session).list_for_actor(
            actor_user_id=actor, page=2, page_size=2, sort="expires_at"
        )
        assert page2.total == 4
        assert len(page2.items) == 2
        assert page2.has_next is False

        ids = {i.id for i in result.items} | {i.id for i in page2.items}
        assert wrong_role_id not in ids
        assert decided_id not in ids
        assert expired_id not in ids
        assert self_forbidden_id in ids
        assert set(eligible_ids).issubset(ids)

        # As requester with self_forbidden: that request hidden
        as_req = await ApprovalQueryService(session).list_for_actor(
            actor_user_id=requester_id
        )
        assert all(item.id != self_forbidden_id for item in as_req.items)


@pytest.mark.asyncio
async def test_detail_safe_context_and_decisions(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _execution_id, approval_id, _requester_id = await _enter_waiting(
        db_session_factory,
        monkeypatch,
        decision_mode="ALL",
        required_approvals=2,
        allow_self_approval=True,
        approver_scope={},
    )
    async with db_session_factory() as session:
        actor_a = await _create_approver(session)
        actor_b = await _create_approver(session)
        await session.commit()
        await ApprovalDecisionService(session).decide(
            approval_id=approval_id,
            actor_user_id=actor_a,
            decision="APPROVE",
            comment="ok",
        )

    async with db_session_factory() as session:
        # Inject SECRET_REF into stored snapshot for projection check.
        request = await ApprovalRequestRepository(session).get(approval_id)
        assert request is not None
        snap = dict(request.context_snapshot)
        resolved = dict(snap.get("resolved_input") or {})
        resolved["api_key"] = {"kind": "SECRET_REF", "secret_id": "must-not-leak"}
        snap["resolved_input"] = resolved
        from app.approval.context import compute_approval_context_hash

        request.context_snapshot = snap
        request.context_hash = compute_approval_context_hash(snap)
        await session.commit()

        detail = await ApprovalQueryService(session).get_for_actor(
            approval_id=approval_id, actor_user_id=actor_b
        )
        ctx = detail.safe_context
        assert "context_hash" not in ctx
        assert "context_snapshot" not in str(ctx)
        assert ctx["resolved_input"]["api_key"] == {
            "kind": "SECRET_REF",
            "masked": True,
        }
        assert "secret_id" not in ctx["resolved_input"]["api_key"]
        assert detail.item.approve_count == 1
        assert detail.item.reject_count == 0
        assert len(detail.decisions) == 1
        assert detail.decisions[0].decision == "APPROVE"
        assert not hasattr(detail.decisions[0], "context_hash")
        # Ensure raw fields absent from response dataclass
        assert not hasattr(detail.item, "context_hash")


@pytest.mark.asyncio
async def test_idor_role_scoped_404(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _e, approval_id, _r = await _enter_waiting(
        db_session_factory,
        monkeypatch,
        allow_self_approval=False,
        approver_scope={"role_codes": ["ROLE_B_ONLY"]},
    )
    async with db_session_factory() as session:
        actor_a = await _create_approver(session, role_code="ROLE_A_ONLY")
        await session.commit()
        with pytest.raises(AppError) as exc:
            await ApprovalQueryService(session).get_for_actor(
                approval_id=approval_id, actor_user_id=actor_a
            )
        assert exc.value.status_code == 404
        assert exc.value.code == "NOT_FOUND"


@pytest.mark.asyncio
async def test_get_list_no_mutation(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, approval_id, _requester_id = await _enter_waiting(
        db_session_factory,
        monkeypatch,
        allow_self_approval=True,
        approver_scope={},
    )
    async with db_session_factory() as session:
        actor = await _create_approver(session)
        await session.commit()

        before_req = await ApprovalRequestRepository(session).get(approval_id)
        assert before_req is not None
        before = (
            before_req.status,
            before_req.resolved_at,
            before_req.lock_version,
        )
        exec_before = await ExecutionRepository(session).get(execution_id)
        assert exec_before is not None
        step_before = (await ExecutionRepository(session).list_steps(execution_id))[0]
        exec_snap = (exec_before.status, exec_before.lock_version)
        step_snap = (step_before.status, step_before.lock_version)
        decisions_before = await ApprovalDecisionRepository(session).list_for_request(
            approval_id
        )
        from sqlalchemy import select
        from app.models.outbox import OutboxEvent

        outbox_before = list(
            (await session.execute(select(OutboxEvent))).scalars().all()
        )

        await ApprovalQueryService(session).list_for_actor(actor_user_id=actor)
        await ApprovalQueryService(session).get_for_actor(
            approval_id=approval_id, actor_user_id=actor
        )
        await session.commit()  # should be no dirty state; commit is no-op for mutations

        after_req = await ApprovalRequestRepository(session).get(approval_id)
        assert after_req is not None
        assert (
            after_req.status,
            after_req.resolved_at,
            after_req.lock_version,
        ) == before
        exec_after = await ExecutionRepository(session).get(execution_id)
        assert exec_after is not None
        step_after = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert (exec_after.status, exec_after.lock_version) == exec_snap
        assert (step_after.status, step_after.lock_version) == step_snap
        decisions_after = await ApprovalDecisionRepository(session).list_for_request(
            approval_id
        )
        assert len(decisions_after) == len(decisions_before)
        outbox_after = list(
            (await session.execute(select(OutboxEvent))).scalars().all()
        )
        assert len(outbox_after) == len(outbox_before)


@pytest.mark.asyncio
async def test_sqlite_scope_semantics_match_validate_approver_scope(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQLite list/detail mirrors validate_approver_scope fail-closed rules."""
    cases: list[tuple[str, object, bool]] = [
        ("mixed_non_string", {"role_codes": ["ROLE_A", 123]}, False),
        ("empty_string", {"role_codes": [""]}, False),
        ("blank_whitespace", {"role_codes": ["   "]}, False),
        ("trimmed_match", {"role_codes": [" ROLE_A "]}, True),
        ("extra_key", {"role_codes": ["ROLE_A"], "legacy": True}, False),
    ]

    ids: list[tuple[str, uuid.UUID, bool]] = []
    for label, scope, expect in cases:
        _e, aid, _r = await _enter_waiting(
            db_session_factory,
            monkeypatch,
            allow_self_approval=True,
            approver_scope={"role_codes": ["ROLE_A"]},
        )
        async with db_session_factory() as session:
            request = await ApprovalRequestRepository(session).get(aid)
            assert request is not None
            request.approval_scope = scope  # type: ignore[assignment]
            await session.commit()
        ids.append((label, aid, expect))

    async with db_session_factory() as session:
        actor = await _create_approver(session, role_code="ROLE_A")
        await session.commit()
        inbox = await ApprovalQueryService(session).list_for_actor(actor_user_id=actor)
        inbox_ids = {item.id for item in inbox.items}
        for label, aid, expect in ids:
            if expect:
                assert aid in inbox_ids, label
                detail = await ApprovalQueryService(session).get_for_actor(
                    approval_id=aid, actor_user_id=actor
                )
                assert detail.item.id == aid, label
            else:
                assert aid not in inbox_ids, label
                with pytest.raises(AppError) as exc:
                    await ApprovalQueryService(session).get_for_actor(
                        approval_id=aid, actor_user_id=actor
                    )
                assert exc.value.status_code == 404, label


@pytest.mark.asyncio
async def test_sqlite_multi_role_unique_bind_names(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Actor with multiple roles must match when any role intersects (unique binds)."""
    _e, approval_id, _r = await _enter_waiting(
        db_session_factory,
        monkeypatch,
        allow_self_approval=False,
        approver_scope={"role_codes": ["ROLE_SECOND"]},
    )
    async with db_session_factory() as session:
        actor = await UserRepository(session).create(
            username=f"multi_{uuid.uuid4().hex[:8]}",
            display_name="Multi",
            email=f"multi_{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE.value,
        )
        await _grant_decide(session, user_id=actor.id, role_code="ROLE_FIRST")
        await _grant_decide(session, user_id=actor.id, role_code="ROLE_SECOND")
        await session.commit()

        roles = await UserRoleRepository(session).list_roles(actor.id)
        assert {r.code for r in roles} >= {"ROLE_FIRST", "ROLE_SECOND"}

        inbox = await ApprovalQueryService(session).list_for_actor(
            actor_user_id=actor.id
        )
        assert any(item.id == approval_id for item in inbox.items)
        detail = await ApprovalQueryService(session).get_for_actor(
            approval_id=approval_id, actor_user_id=actor.id
        )
        assert detail.item.id == approval_id
