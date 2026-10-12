"""PostgreSQL concurrency / persistence evidence for Tool Factory idempotency."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from app.core.errors import AppError
from app.domain.enums import UserStatus
from app.factory import ARTIFACT_TYPE_OPENAPI_ANALYSIS, JOB_TYPE_OPENAPI_ANALYZE
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.role import PermissionRepository
from app.schemas.auth import (
    RoleCreate,
    RolePermissionReplaceRequest,
    UserCreate,
    UserRoleReplaceRequest,
)
from app.services.factory import FactoryService
from app.services.role import RoleService
from app.services.user import UserService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_OPERATION_SCOPE = "FACTORY_OPENAPI_ANALYZE_V1"
_RESOURCE_TYPE = "TOOL_FACTORY_JOB"

MINIMAL_JSON = json.dumps(
    {
        "openapi": "3.0.3",
        "info": {"title": "Demo", "version": "1.0.0"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/ping": {
                "get": {
                    "operationId": "ping",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    },
    separators=(",", ":"),
).encode("utf-8")

OTHER_JSON = json.dumps(
    {
        "openapi": "3.0.3",
        "info": {"title": "Other", "version": "2.0.0"},
        "servers": [{"url": "https://other.example.com"}],
        "paths": {
            "/pong": {
                "get": {
                    "operationId": "pong",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    },
    separators=(",", ":"),
).encode("utf-8")


async def _seed_manage_user(session: AsyncSession) -> uuid.UUID:
    user = await UserService(session).create(
        UserCreate(
            username=f"fac-pg-{uuid.uuid4().hex[:10]}",
            display_name="Factory PG Tester",
            email=f"fac-pg-{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE,
        )
    )
    role = await RoleService(session).create(
        RoleCreate(code=f"fac-pg-r-{uuid.uuid4().hex[:8]}", name="Factory PG Role")
    )
    perm = await PermissionRepository(session).get_by_code("mcp.server.manage")
    assert perm is not None
    await RoleService(session).replace_permissions(
        role.id,
        RolePermissionReplaceRequest(permission_ids=[perm.id]),
        expected_lock_version=1,
    )
    await UserService(session).replace_roles(
        user.id,
        UserRoleReplaceRequest(role_ids=[role.id]),
        expected_lock_version=int(user.lock_version),
    )
    await session.commit()
    return user.id


def _install_create_completed_barrier(
    monkeypatch: pytest.MonkeyPatch,
    *,
    parties: int = 2,
) -> asyncio.Barrier:
    """Hold both callers at the final idempotency insert until both arrive."""

    barrier = asyncio.Barrier(parties)
    original = IdempotencyRepository.create_completed

    async def _synced(
        self: IdempotencyRepository,
        **kwargs: Any,
    ) -> Any:
        await barrier.wait()
        return await original(self, **kwargs)

    monkeypatch.setattr(IdempotencyRepository, "create_completed", _synced)
    return barrier


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_concurrent_same_source_idempotency_one_job(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with integration_session_factory() as session:
        user_id = await _seed_manage_user(session)

    key = f"fac-race-{uuid.uuid4().hex}"
    filename = "race.json"
    _install_create_completed_barrier(monkeypatch, parties=2)

    async def _run() -> Any:
        async with integration_session_factory() as session:
            try:
                return await FactoryService(session).create_openapi_analyze_job(
                    actor_user_id=user_id,
                    source_bytes=MINIMAL_JSON,
                    filename=filename,
                    idempotency_key=key,
                )
            except AppError as exc:
                return exc

    results = await asyncio.gather(_run(), _run())
    outcomes = [r for r in results if not isinstance(r, AppError)]
    errors = [r for r in results if isinstance(r, AppError)]
    assert not errors, f"unexpected AppError in same-source race: {errors}"
    assert len(outcomes) == 2
    replay_flags = sorted(o.replayed for o in outcomes)
    assert replay_flags == [False, True]
    job_ids = {o.result.id for o in outcomes}
    assert len(job_ids) == 1
    surviving_id = next(iter(job_ids))

    async with integration_session_factory() as session:
        job_count = (
            await session.execute(
                text(
                    """
                    SELECT count(*) FROM tool_factory_jobs
                    WHERE requested_by = :uid
                      AND job_type = :jt
                      AND source_name = :name
                    """
                ),
                {
                    "uid": str(user_id),
                    "jt": JOB_TYPE_OPENAPI_ANALYZE,
                    "name": filename,
                },
            )
        ).scalar_one()
        assert int(job_count) == 1

        artifact_count = (
            await session.execute(
                text(
                    """
                    SELECT count(*) FROM tool_factory_artifacts
                    WHERE job_id = :jid AND artifact_type = :atype
                    """
                ),
                {
                    "jid": str(surviving_id),
                    "atype": ARTIFACT_TYPE_OPENAPI_ANALYSIS,
                },
            )
        ).scalar_one()
        assert int(artifact_count) == 1

        idemp_rows = (
            await session.execute(
                text(
                    """
                    SELECT resource_type, resource_id::text, operation_scope
                    FROM api_idempotency_records
                    WHERE principal_key = :pk
                      AND operation_scope = :scope
                      AND idempotency_key = :key
                    """
                ),
                {
                    "pk": str(user_id),
                    "scope": _OPERATION_SCOPE,
                    "key": key,
                },
            )
        ).all()
        assert len(idemp_rows) == 1
        resource_type, resource_id, operation_scope = idemp_rows[0]
        assert operation_scope == _OPERATION_SCOPE
        assert resource_type == _RESOURCE_TYPE
        assert resource_id == str(surviving_id)

        orphan_jobs = (
            await session.execute(
                text("SELECT count(*) FROM tool_factory_jobs WHERE requested_by = :uid"),
                {"uid": str(user_id)},
            )
        ).scalar_one()
        assert int(orphan_jobs) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_concurrent_same_key_different_source_one_job(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with integration_session_factory() as session:
        user_id = await _seed_manage_user(session)

    key = f"fac-diff-{uuid.uuid4().hex}"
    filename = "race.json"
    _install_create_completed_barrier(monkeypatch, parties=2)

    async def _run(source: bytes) -> Any:
        async with integration_session_factory() as session:
            try:
                return await FactoryService(session).create_openapi_analyze_job(
                    actor_user_id=user_id,
                    source_bytes=source,
                    filename=filename,
                    idempotency_key=key,
                )
            except AppError as exc:
                return exc

    results = await asyncio.gather(_run(MINIMAL_JSON), _run(OTHER_JSON))
    outcomes = [r for r in results if not isinstance(r, AppError)]
    errors = [r for r in results if isinstance(r, AppError)]
    assert len(outcomes) == 1
    assert outcomes[0].replayed is False
    assert len(errors) == 1
    assert errors[0].code == "IDEMPOTENCY_KEY_REUSED"
    assert errors[0].status_code == 409
    surviving_id = outcomes[0].result.id

    async with integration_session_factory() as session:
        job_count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM tool_factory_jobs WHERE requested_by = :uid"
                ),
                {"uid": str(user_id)},
            )
        ).scalar_one()
        assert int(job_count) == 1

        artifact_count = (
            await session.execute(
                text(
                    """
                    SELECT count(*) FROM tool_factory_artifacts
                    WHERE job_id = :jid AND artifact_type = :atype
                    """
                ),
                {
                    "jid": str(surviving_id),
                    "atype": ARTIFACT_TYPE_OPENAPI_ANALYSIS,
                },
            )
        ).scalar_one()
        assert int(artifact_count) == 1

        idemp_count = (
            await session.execute(
                text(
                    """
                    SELECT count(*) FROM api_idempotency_records
                    WHERE principal_key = :pk
                      AND operation_scope = :scope
                      AND idempotency_key = :key
                      AND resource_type = :rtype
                      AND resource_id = :rid
                    """
                ),
                {
                    "pk": str(user_id),
                    "scope": _OPERATION_SCOPE,
                    "key": key,
                    "rtype": _RESOURCE_TYPE,
                    "rid": str(surviving_id),
                },
            )
        ).scalar_one()
        assert int(idemp_count) == 1
