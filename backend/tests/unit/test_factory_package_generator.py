"""Unit tests for deterministic Factory Generated Package builder."""

from __future__ import annotations

from app.core.canonical_hash import compute_canonical_json_hash
from app.factory.package_generator import (
    FACTORY_PACKAGE_GENERATOR_VERSION,
    PACKAGE_SCHEMA_VERSION,
    build_generated_package,
    encode_canonical_json_bytes,
)


def _snap(*, op: str, cid: str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa") -> dict:
    return {
        "candidate_id": cid,
        "job_id": "11111111-1111-1111-1111-111111111111",
        "analysis_artifact_id": "22222222-2222-2222-2222-222222222222",
        "source_sha256": "a" * 64,
        "analyzer_version": "openapi-analyzer-v1",
        "operation_key": op,
        "method": op.split(" ", 1)[0],
        "path": op.split(" ", 1)[1],
        "operation_id": None,
        "summary": None,
        "description": None,
        "tags": [],
        "deprecated": False,
        "parameters": [],
        "request_schema": None,
        "request_body_required": False,
        "response_schema": {"type": "object"},
    }


def test_package_deterministic_for_same_input() -> None:
    snapshot = [
        _snap(op="POST /pets", cid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        _snap(op="GET /pets", cid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
    ]
    kwargs = dict(
        build_id="33333333-3333-3333-3333-333333333333",
        build_input_hash="b" * 64,
        build_generator_version="factory-build-foundation-v1",
        job_id="11111111-1111-1111-1111-111111111111",
        analysis_artifact_id="22222222-2222-2222-2222-222222222222",
        source_sha256="a" * 64,
        analyzer_version="openapi-analyzer-v1",
        candidates_snapshot=snapshot,
    )
    p1, raw1, h1 = build_generated_package(**kwargs)
    p2, raw2, h2 = build_generated_package(**kwargs)
    assert h1 == h2
    assert raw1 == raw2
    assert h1 == compute_canonical_json_hash(p1)
    assert raw1 == encode_canonical_json_bytes(p1)
    assert p1["generator_version"] == FACTORY_PACKAGE_GENERATOR_VERSION
    assert p1["package_schema_version"] == PACKAGE_SCHEMA_VERSION
    assert [t["operation_key"] for t in p1["tools"]] == ["GET /pets", "POST /pets"]


def test_package_stable_order_ignores_input_order() -> None:
    a = _snap(op="GET /a", cid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    b = _snap(op="GET /b", cid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    p1, _, h1 = build_generated_package(
        build_id="33333333-3333-3333-3333-333333333333",
        build_input_hash="b" * 64,
        build_generator_version="factory-build-foundation-v1",
        job_id="11111111-1111-1111-1111-111111111111",
        analysis_artifact_id="22222222-2222-2222-2222-222222222222",
        source_sha256="a" * 64,
        analyzer_version="openapi-analyzer-v1",
        candidates_snapshot=[b, a],
    )
    p2, _, h2 = build_generated_package(
        build_id="33333333-3333-3333-3333-333333333333",
        build_input_hash="b" * 64,
        build_generator_version="factory-build-foundation-v1",
        job_id="11111111-1111-1111-1111-111111111111",
        analysis_artifact_id="22222222-2222-2222-2222-222222222222",
        source_sha256="a" * 64,
        analyzer_version="openapi-analyzer-v1",
        candidates_snapshot=[a, b],
    )
    assert h1 == h2
    assert p1["tools"] == p2["tools"]
