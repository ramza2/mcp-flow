"""PostgreSQL integration tests for append-only Audit ledger (PR #57)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.integrity import verify_audit_event_integrity
from app.audit.sanitize import REDACTION_MARKER
from app.audit.writer import (
    ACTION_APPROVAL_DECISION,
    ACTION_AUTH_LOGIN,
    ACTION_EXECUTION_CANCEL,
    ACTION_EXECUTION_CREATE,
    ACTION_SCHEDULE_TRIGGER,
    AuditWriter,
)
from app.bootstrap.permissions import permission_seed_id
from app.domain.enums import AuditActorType, AuditResult
from app.models.audit import AuditEvent
from app.models.auth import Permission
from app.repositories.audit import AuditRepository

pytestmark = pytest.mark.integration


async def _append_event(
    session: AsyncSession,
    *,
    action: str = "auth.login",
    occurred_at: datetime | None = None,
    request_id: str | None = "req-pg",
    actor_id: str | None = None,
    change_set: dict[str, Any] | None = None,
) -> AuditEvent:
    return await AuditWriter(session).append(
        actor_type=AuditActorType.USER,
        actor_id=actor_id or str(uuid.uuid4()),
        action=action,
        result=AuditResult.SUCCESS,
        resource_type="USER",
        resource_id=str(uuid.uuid4()),
        request_id=request_id,
        change_set=change_set,
        occurred_at=occurred_at or datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_pg_append_only_rejects_update_and_delete(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        row = await _append_event(session)
        await session.commit()
        event_id = row.event_id
        integrity = row.integrity_hash

    async with integration_session_factory() as session:
        with pytest.raises(Exception) as upd_exc:
            await session.execute(
                text(
                    "UPDATE audit_events SET result = 'FAILURE' "
                    "WHERE event_id = :eid"
                ),
                {"eid": event_id},
            )
            await session.commit()
        assert "append-only" in str(upd_exc.value).lower()
        await session.rollback()

        with pytest.raises(Exception) as del_exc:
            await session.execute(
                text("DELETE FROM audit_events WHERE event_id = :eid"),
                {"eid": event_id},
            )
            await session.commit()
        assert "append-only" in str(del_exc.value).lower()
        await session.rollback()

        kept = await AuditRepository(session).get_by_event_id(event_id)
        assert kept is not None
        assert kept.result == AuditResult.SUCCESS.value
        assert kept.integrity_hash == integrity
        assert verify_audit_event_integrity(kept) is True


@pytest.mark.asyncio
async def test_pg_secret_sanitizer_persists_redacted_only(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dirty = {
        "password": "p",
        "Authorization": "Bearer abc",
        "nested": {"accessToken": "token", "requestState": "opaque"},
    }
    async with integration_session_factory() as session:
        row = await _append_event(session, change_set=dirty)
        await session.commit()
        event_id = row.event_id

    async with integration_session_factory() as session:
        raw = (
            await session.execute(
                text(
                    "SELECT change_set::text FROM audit_events WHERE event_id = :eid"
                ),
                {"eid": event_id},
            )
        ).scalar_one()
        assert "Bearer abc" not in raw
        assert "opaque" not in raw
        # token may appear inside REDACTED marker spelling — ensure plaintext gone
        assert '"accessToken": "token"' not in raw
        assert REDACTION_MARKER in raw
        row = await AuditRepository(session).get_by_event_id(event_id)
        assert row is not None
        assert row.change_set["password"] == REDACTION_MARKER
        assert row.change_set["Authorization"] == REDACTION_MARKER
        assert row.change_set["nested"]["accessToken"] == REDACTION_MARKER
        assert row.change_set["nested"]["requestState"] == REDACTION_MARKER
        assert verify_audit_event_integrity(row) is True


@pytest.mark.asyncio
async def test_pg_identical_timestamps_stable_cursor_order(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ts = datetime(2026, 10, 6, 15, 0, 0, tzinfo=UTC)
    async with integration_session_factory() as session:
        for i in range(3):
            await _append_event(
                session,
                action=f"probe.{i}",
                occurred_at=ts,
                request_id=f"same-ts-{i}-{uuid.uuid4().hex[:6]}",
            )
        await session.commit()

    async with integration_session_factory() as session:
        rows = await AuditRepository(session).list_events(
            limit=10,
            from_time=ts,
            to_time=ts.replace(microsecond=1) if False else datetime(
                2026, 10, 6, 15, 0, 1, tzinfo=UTC
            ),
            action=None,
        )
        # Filter to our probe actions
        probes = [r for r in rows if r.action.startswith("probe.")]
        assert len(probes) >= 3
        # Same occurred_at → id DESC
        same = [r for r in probes if r.occurred_at == ts]
        ids = [r.id for r in same]
        assert ids == sorted(ids, reverse=True)


@pytest.mark.asyncio
async def test_pg_concurrent_append_no_global_lock(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def _one(n: int) -> uuid.UUID:
        async with integration_session_factory() as session:
            row = await _append_event(
                session,
                action="concurrent.append",
                request_id=f"c-{n}-{uuid.uuid4().hex}",
            )
            await session.commit()
            return row.event_id

    ids = await asyncio.gather(*[_one(i) for i in range(8)])
    assert len(set(ids)) == 8


@pytest.mark.asyncio
async def test_pg_audit_export_permission_seeded(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    expected_id = permission_seed_id("audit.export")
    async with integration_session_factory() as session:
        row = await session.get(Permission, expected_id)
        assert row is not None
        assert row.code == "audit.export"
        by_code = (
            await session.execute(
                select(Permission).where(Permission.code == "audit.export")
            )
        ).scalar_one()
        assert by_code.id == expected_id


@pytest.mark.asyncio
async def test_pg_success_mutation_audit_atomic_rollback(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Business staging + failing Audit append must roll back together."""
    from app.domain.enums import UserStatus
    from app.models.auth import User
    from app.repositories.user import UserRepository

    marker = f"audit-atomic-{uuid.uuid4().hex[:8]}"
    async with integration_session_factory() as session:
        await UserRepository(session).create(
            username=marker,
            display_name="Atomic",
            email=f"{marker}@example.com",
            status=UserStatus.ACTIVE,
        )
        with pytest.raises(Exception):
            await AuditRepository(session).append(
                event_id=uuid.uuid4(),
                occurred_at=datetime.now(UTC),
                actor_type="USER",
                actor_id=marker,
                action="",  # violates length BETWEEN 1 AND 128
                resource_type="USER",
                resource_id=marker,
                execution_id=None,
                result="SUCCESS",
                request_id=None,
                trace_id=None,
                source_ip_hash=None,
                before_data=None,
                after_data=None,
                change_set=None,
                reason=None,
                integrity_hash="0" * 64,
            )
            await session.commit()
        await session.rollback()

    async with integration_session_factory() as session:
        user_row = (
            await session.execute(select(User).where(User.username == marker))
        ).scalar_one_or_none()
        assert user_row is None
        audit_rows = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.actor_id == marker)
            )
        ).scalars().all()
        assert audit_rows == []
