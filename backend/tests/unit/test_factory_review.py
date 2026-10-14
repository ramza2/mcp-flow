"""Unit tests for Tool Factory Review foundation (Issue #93)."""

from __future__ import annotations

import json
import uuid

import pytest
from app.core.canonical_hash import compute_canonical_json_hash
from app.core.errors import AppError
from app.domain.enums import JobStatus, UserStatus
from app.factory.errors import (
    FACTORY_PACKAGE_HASH_MISMATCH,
    FACTORY_PACKAGE_LINEAGE_MISMATCH,
    FACTORY_REVIEW_ALREADY_EXISTS,
    FACTORY_REVIEW_EVIDENCE_MISMATCH,
    FACTORY_REVIEW_NOT_ELIGIBLE,
)
from app.factory.package_generator import encode_canonical_json_bytes
from app.infrastructure.object_storage import InMemoryObjectStorage
from app.models.factory import (
    ToolFactoryGeneratedPackage,
    ToolFactoryReview,
    ToolFactoryTestRun,
)
from app.models.mcp import MCPServer, MCPTool
from app.repositories.role import PermissionRepository
from app.schemas.auth import (
    RoleCreate,
    RolePermissionReplaceRequest,
    UserCreate,
    UserRoleReplaceRequest,
)
from app.services.factory import FactoryService
from app.services.factory_test_worker import FactoryTestWorkerService
from app.services.factory_worker import FactoryWorkerService
from app.services.role import RoleService
from app.services.user import UserService
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

MINIMAL_JSON = json.dumps(
    {
        "openapi": "3.0.3",
        "info": {"title": "Demo", "version": "1.0.0"},
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


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user = await UserService(session).create(
        UserCreate(
            username=f"fac-rv-{uuid.uuid4().hex[:10]}",
            display_name="Factory Review",
            email=f"fac-rv-{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE,
        )
    )
    role = await RoleService(session).create(
        RoleCreate(code=f"fac-rv-r-{uuid.uuid4().hex[:8]}", name="Fac Review")
    )
    manage = await PermissionRepository(session).get_by_code("mcp.server.manage")
    read = await PermissionRepository(session).get_by_code("mcp.server.read")
    assert manage is not None and read is not None
    await RoleService(session).replace_permissions(
        role.id,
        RolePermissionReplaceRequest(permission_ids=[manage.id, read.id]),
        expected_lock_version=1,
    )
    await UserService(session).replace_roles(
        user.id,
        UserRoleReplaceRequest(role_ids=[role.id]),
        expected_lock_version=int(user.lock_version),
    )
    await session.commit()
    return user.id


async def _pass_test_run(
    session: AsyncSession,
) -> tuple[FactoryService, InMemoryObjectStorage, uuid.UUID, uuid.UUID]:
    user_id = await _seed_user(session)
    store = InMemoryObjectStorage()
    svc = FactoryService(session, storage=store)
    job = await svc.create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=MINIMAL_JSON,
        filename="pets.json",
        idempotency_key=f"job-{uuid.uuid4().hex}",
    )
    await FactoryWorkerService(session, storage=store, worker_id="u-a").process_job(
        job_id=job.result.id
    )
    await session.commit()
    cands = await svc.list_candidates_for_job(user_id, job.result.id)
    build = await svc.create_build(
        actor_user_id=user_id,
        job_id=job.result.id,
        candidate_ids=[item.id for item in cands.items],
        idempotency_key=f"build-{uuid.uuid4().hex}",
    )
    pkg = await svc.generate_package(
        actor_user_id=user_id,
        build_id=build.result.id,
        idempotency_key=f"pkg-{uuid.uuid4().hex}",
    )
    tr = await svc.create_test_run(
        actor_user_id=user_id,
        package_id=pkg.result.id,
        idempotency_key=f"tr-{uuid.uuid4().hex}",
    )
    done = await FactoryTestWorkerService(
        session, storage=store, worker_id="u-t"
    ).process_test_run(test_run_id=tr.result.id)
    await session.commit()
    assert done.status == JobStatus.SUCCEEDED.value
    return svc, store, user_id, tr.result.id


@pytest.mark.asyncio
async def test_review_approve_and_reject_happy_paths(db_session: AsyncSession) -> None:
    svc, _store, user_id, test_run_id = await _pass_test_run(db_session)
    key = f"rv-{uuid.uuid4().hex}"
    approved = await svc.create_review(
        actor_user_id=user_id,
        test_run_id=test_run_id,
        decision="APPROVE",
        comment="looks good",
        idempotency_key=key,
    )
    assert approved.http_status == 201
    assert approved.replayed is False
    assert approved.result.decision == "APPROVE"
    assert approved.result.comment == "looks good"
    assert approved.result.test_run_id == test_run_id
    assert len(approved.result.review_evidence_hash) == 64
    assert len(approved.result.test_evidence_hash) == 64
    assert "object_key" not in approved.result.model_dump()

    replay = await svc.create_review(
        actor_user_id=user_id,
        test_run_id=test_run_id,
        decision="APPROVE",
        comment="looks good",
        idempotency_key=key,
    )
    assert replay.replayed is True
    assert replay.result.id == approved.result.id

    detail = await svc.get_review(user_id, approved.result.id)
    assert detail.id == approved.result.id
    by_run = await svc.get_review_for_test_run(user_id, test_run_id)
    assert by_run.id == approved.result.id

    tools = (
        await db_session.execute(select(func.count()).select_from(MCPTool))
    ).scalar_one()
    servers = (
        await db_session.execute(select(func.count()).select_from(MCPServer))
    ).scalar_one()
    assert int(tools) == 0
    assert int(servers) == 0

    # Separate PASS TestRun for REJECT.
    svc2, _s2, user_id2, test_run_id2 = await _pass_test_run(db_session)
    rejected = await svc2.create_review(
        actor_user_id=user_id2,
        test_run_id=test_run_id2,
        decision="REJECT",
        comment="needs work",
        idempotency_key=f"rv-rej-{uuid.uuid4().hex}",
    )
    assert rejected.result.decision == "REJECT"
    assert rejected.result.comment == "needs work"


@pytest.mark.asyncio
async def test_review_rejects_non_pass_and_evidence_tamper(
    db_session: AsyncSession,
) -> None:
    user_id = await _seed_user(db_session)
    store = InMemoryObjectStorage()
    svc = FactoryService(db_session, storage=store)
    job = await svc.create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=MINIMAL_JSON,
        filename="x.json",
        idempotency_key=f"job-{uuid.uuid4().hex}",
    )
    await FactoryWorkerService(db_session, storage=store, worker_id="a").process_job(
        job_id=job.result.id
    )
    await db_session.commit()
    cands = await svc.list_candidates_for_job(user_id, job.result.id)
    build = await svc.create_build(
        actor_user_id=user_id,
        job_id=job.result.id,
        candidate_ids=[cands.items[0].id],
        idempotency_key=f"b-{uuid.uuid4().hex}",
    )
    pkg = await svc.generate_package(
        actor_user_id=user_id,
        build_id=build.result.id,
        idempotency_key=f"p-{uuid.uuid4().hex}",
    )
    # Force FAILED TestRun without going through worker success.
    tr = await svc.create_test_run(
        actor_user_id=user_id,
        package_id=pkg.result.id,
        idempotency_key=f"tr-{uuid.uuid4().hex}",
    )
    row = await db_session.get(ToolFactoryTestRun, tr.result.id)
    assert row is not None
    row.status = JobStatus.FAILED.value
    row.result = "FAIL"
    row.evidence_hash = "a" * 64
    row.security_result = {"passed": False}
    row.contract_result = {"passed": False}
    row.evidence = {"result": "FAIL"}
    await db_session.commit()

    with pytest.raises(AppError) as exc:
        await svc.create_review(
            actor_user_id=user_id,
            test_run_id=tr.result.id,
            decision="APPROVE",
            comment=None,
            idempotency_key=f"rv-{uuid.uuid4().hex}",
        )
    assert exc.value.code == FACTORY_REVIEW_NOT_ELIGIBLE

    # PASS path then tamper evidence hash.
    svc2, _store2, user_id2, test_run_id2 = await _pass_test_run(db_session)
    row2 = await db_session.get(ToolFactoryTestRun, test_run_id2)
    assert row2 is not None
    row2.evidence_hash = "b" * 64
    await db_session.commit()
    with pytest.raises(AppError) as hash_exc:
        await svc2.create_review(
            actor_user_id=user_id2,
            test_run_id=test_run_id2,
            decision="APPROVE",
            comment=None,
            idempotency_key=f"rv-{uuid.uuid4().hex}",
        )
    assert hash_exc.value.code == FACTORY_REVIEW_EVIDENCE_MISMATCH


@pytest.mark.asyncio
async def test_review_rejects_package_missing_tamper_lineage(
    db_session: AsyncSession,
) -> None:
    svc, store, user_id, test_run_id = await _pass_test_run(db_session)
    tr = await db_session.get(ToolFactoryTestRun, test_run_id)
    assert tr is not None
    package = await db_session.get(ToolFactoryGeneratedPackage, tr.generated_package_id)
    assert package is not None

    await store.delete_object(
        bucket=package.object_bucket,
        object_key=package.object_key,
    )
    with pytest.raises(AppError) as miss:
        await svc.create_review(
            actor_user_id=user_id,
            test_run_id=test_run_id,
            decision="APPROVE",
            comment=None,
            idempotency_key=f"rv-miss-{uuid.uuid4().hex}",
        )
    assert miss.value.status_code in {409, 503}

    svc2, store2, user_id2, test_run_id2 = await _pass_test_run(db_session)
    tr2 = await db_session.get(ToolFactoryTestRun, test_run_id2)
    assert tr2 is not None
    package2 = await db_session.get(
        ToolFactoryGeneratedPackage, tr2.generated_package_id
    )
    assert package2 is not None
    original = await store2.get_bytes(
        bucket=package2.object_bucket,
        object_key=package2.object_key,
    )
    payload = json.loads(original.decode("utf-8"))
    payload["tools"][0]["summary"] = "tampered"
    tampered = encode_canonical_json_bytes(payload)
    await store2.put_bytes(
        bucket=package2.object_bucket,
        object_key=package2.object_key,
        data=tampered,
        content_type="application/json",
    )
    with pytest.raises(AppError) as tamper:
        await svc2.create_review(
            actor_user_id=user_id2,
            test_run_id=test_run_id2,
            decision="APPROVE",
            comment=None,
            idempotency_key=f"rv-tamp-{uuid.uuid4().hex}",
        )
    assert tamper.value.code in {
        FACTORY_PACKAGE_HASH_MISMATCH,
        "FACTORY_PACKAGE_SIZE_MISMATCH",
    }

    # Tamper Generated Package lineage (TestRun sealed evidence still consistent).
    svc3, _store3, user_id3, test_run_id3 = await _pass_test_run(db_session)
    tr3 = await db_session.get(ToolFactoryTestRun, test_run_id3)
    assert tr3 is not None
    package3 = await db_session.get(
        ToolFactoryGeneratedPackage, tr3.generated_package_id
    )
    assert package3 is not None
    package3.package_content_sha256 = "c" * 64
    await db_session.commit()
    with pytest.raises(AppError) as lin:
        await svc3.create_review(
            actor_user_id=user_id3,
            test_run_id=test_run_id3,
            decision="REJECT",
            comment="no",
            idempotency_key=f"rv-lin-{uuid.uuid4().hex}",
        )
    assert lin.value.code == FACTORY_PACKAGE_LINEAGE_MISMATCH


@pytest.mark.asyncio
async def test_review_rejects_sealed_evidence_projection_drift(
    db_session: AsyncSession,
) -> None:
    """Projection columns must match hash-sealed evidence content (fail closed)."""

    # security_result projection only.
    svc, _store, user_id, test_run_id = await _pass_test_run(db_session)
    row = await db_session.get(ToolFactoryTestRun, test_run_id)
    assert row is not None and isinstance(row.security_result, dict)
    row.security_result = {**row.security_result, "passed": False, "extra": "tamper"}
    await db_session.commit()
    with pytest.raises(AppError) as sec:
        await svc.create_review(
            actor_user_id=user_id,
            test_run_id=test_run_id,
            decision="APPROVE",
            comment=None,
            idempotency_key=f"rv-sec-{uuid.uuid4().hex}",
        )
    assert sec.value.code == FACTORY_REVIEW_EVIDENCE_MISMATCH

    # contract_result projection only.
    svc2, _s2, user_id2, test_run_id2 = await _pass_test_run(db_session)
    row2 = await db_session.get(ToolFactoryTestRun, test_run_id2)
    assert row2 is not None and isinstance(row2.contract_result, dict)
    row2.contract_result = {**row2.contract_result, "passed": False}
    await db_session.commit()
    with pytest.raises(AppError) as con:
        await svc2.create_review(
            actor_user_id=user_id2,
            test_run_id=test_run_id2,
            decision="APPROVE",
            comment=None,
            idempotency_key=f"rv-con-{uuid.uuid4().hex}",
        )
    assert con.value.code == FACTORY_REVIEW_EVIDENCE_MISMATCH

    # status/result projection forced PASS while sealed evidence remains FAIL.
    svc3, _s3, user_id3, test_run_id3 = await _pass_test_run(db_session)
    row3 = await db_session.get(ToolFactoryTestRun, test_run_id3)
    assert row3 is not None and isinstance(row3.evidence, dict)
    fail_evidence = {
        **row3.evidence,
        "result": "FAIL",
        "error_code": "FORCED_FAIL",
        "error_message": "projection lie",
    }
    fail_evidence.pop("security", None)
    fail_evidence.pop("contract", None)
    row3.evidence = fail_evidence
    row3.evidence_hash = compute_canonical_json_hash(fail_evidence)
    row3.status = JobStatus.SUCCEEDED.value
    row3.result = "PASS"
    await db_session.commit()
    with pytest.raises(AppError) as fail_ev:
        await svc3.create_review(
            actor_user_id=user_id3,
            test_run_id=test_run_id3,
            decision="APPROVE",
            comment=None,
            idempotency_key=f"rv-fail-{uuid.uuid4().hex}",
        )
    assert fail_ev.value.code == FACTORY_REVIEW_EVIDENCE_MISMATCH

    # sealed evidence lineage/policy drifts from projection (hash still seals).
    svc4, _s4, user_id4, test_run_id4 = await _pass_test_run(db_session)
    row4 = await db_session.get(ToolFactoryTestRun, test_run_id4)
    assert row4 is not None and isinstance(row4.evidence, dict)
    drifted = {
        **row4.evidence,
        "generated_package_id": str(uuid.uuid4()),
        "security_policy_version": "tampered-policy",
    }
    row4.evidence = drifted
    row4.evidence_hash = compute_canonical_json_hash(drifted)
    await db_session.commit()
    with pytest.raises(AppError) as lineage_ev:
        await svc4.create_review(
            actor_user_id=user_id4,
            test_run_id=test_run_id4,
            decision="REJECT",
            comment="no",
            idempotency_key=f"rv-drift-{uuid.uuid4().hex}",
        )
    assert lineage_ev.value.code == FACTORY_REVIEW_EVIDENCE_MISMATCH

    # Happy path still works after the above rejections.
    svc5, _s5, user_id5, test_run_id5 = await _pass_test_run(db_session)
    ok = await svc5.create_review(
        actor_user_id=user_id5,
        test_run_id=test_run_id5,
        decision="APPROVE",
        comment="ok",
        idempotency_key=f"rv-ok-{uuid.uuid4().hex}",
    )
    assert ok.result.decision == "APPROVE"
    assert len(ok.result.review_evidence_hash) == 64


@pytest.mark.asyncio
async def test_review_idempotency_conflict_and_already_exists(
    db_session: AsyncSession,
) -> None:
    svc, _store, user_id, test_run_id = await _pass_test_run(db_session)
    key = f"rv-idem-{uuid.uuid4().hex}"
    first = await svc.create_review(
        actor_user_id=user_id,
        test_run_id=test_run_id,
        decision="APPROVE",
        comment=None,
        idempotency_key=key,
    )
    with pytest.raises(AppError) as conflict:
        await svc.create_review(
            actor_user_id=user_id,
            test_run_id=test_run_id,
            decision="REJECT",
            comment="other",
            idempotency_key=key,
        )
    assert conflict.value.code == "IDEMPOTENCY_KEY_REUSED"

    with pytest.raises(AppError) as exists:
        await svc.create_review(
            actor_user_id=user_id,
            test_run_id=test_run_id,
            decision="REJECT",
            comment="later",
            idempotency_key=f"rv-other-{uuid.uuid4().hex}",
        )
    assert exists.value.code == FACTORY_REVIEW_ALREADY_EXISTS
    assert first.result.decision == "APPROVE"


@pytest.mark.asyncio
async def test_review_one_terminal_row_no_mcp_side_effects(
    db_session: AsyncSession,
) -> None:
    """SQLite StaticPool cannot host true write races; PG covers concurrent APPROVE/REJECT."""

    svc, _store, user_id, test_run_id = await _pass_test_run(db_session)
    first = await svc.create_review(
        actor_user_id=user_id,
        test_run_id=test_run_id,
        decision="APPROVE",
        comment="ok",
        idempotency_key=f"rv-term-{uuid.uuid4().hex}",
    )
    with pytest.raises(AppError) as second:
        await svc.create_review(
            actor_user_id=user_id,
            test_run_id=test_run_id,
            decision="REJECT",
            comment="no",
            idempotency_key=f"rv-term2-{uuid.uuid4().hex}",
        )
    assert second.value.code == FACTORY_REVIEW_ALREADY_EXISTS
    count = (
        await db_session.execute(
            select(func.count())
            .select_from(ToolFactoryReview)
            .where(ToolFactoryReview.test_run_id == test_run_id)
        )
    ).scalar_one()
    assert int(count) == 1
    assert first.result.decision == "APPROVE"
    tools = (
        await db_session.execute(select(func.count()).select_from(MCPTool))
    ).scalar_one()
    servers = (
        await db_session.execute(select(func.count()).select_from(MCPServer))
    ).scalar_one()
    assert int(tools) == 0
    assert int(servers) == 0
