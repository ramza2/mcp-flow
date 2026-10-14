"""Unit tests for Factory sandbox security + contract validators (Issue #91)."""

from __future__ import annotations

import copy
import json
import uuid

import pytest
from app.core.canonical_hash import compute_canonical_json_hash
from app.factory.package_generator import (
    FACTORY_PACKAGE_GENERATOR_VERSION,
    PACKAGE_KIND,
    PACKAGE_SCHEMA_VERSION,
    encode_canonical_json_bytes,
)
from app.factory.sandbox_contract import (
    FACTORY_CONTRACT_POLICY_VERSION,
    SandboxContractError,
    run_contract_checks,
)
from app.factory.sandbox_security import (
    FACTORY_SECURITY_POLICY_VERSION,
    SandboxSecurityError,
    run_security_checks,
)


def _tool(
    *,
    candidate_id: str | None = None,
    operation_key: str = "get:/pets",
    method: str = "GET",
    path: str = "/pets",
    job_id: str,
    analysis_artifact_id: str,
    source_sha256: str,
    analyzer_version: str,
) -> dict:
    return {
        "candidate_id": candidate_id or str(uuid.uuid4()),
        "job_id": job_id,
        "analysis_artifact_id": analysis_artifact_id,
        "source_sha256": source_sha256,
        "analyzer_version": analyzer_version,
        "operation_key": operation_key,
        "method": method,
        "path": path,
        "summary": "list pets",
        "parameters": [{"name": "limit", "in": "query"}],
        "request_schema": {"type": "object"},
        "response_schema": {"type": "object"},
        "tags": ["pets"],
    }


def _package(**overrides: object) -> dict:
    job_id = str(uuid.uuid4())
    analysis_id = str(uuid.uuid4())
    build_id = str(uuid.uuid4())
    source_sha = "a" * 64
    analyzer = "openapi-analyzer-v1"
    build_input = "b" * 64
    tools = [
        _tool(
            job_id=job_id,
            analysis_artifact_id=analysis_id,
            source_sha256=source_sha,
            analyzer_version=analyzer,
        )
    ]
    package: dict = {
        "schema_version": 1,
        "kind": PACKAGE_KIND,
        "package_schema_version": PACKAGE_SCHEMA_VERSION,
        "generator_version": FACTORY_PACKAGE_GENERATOR_VERSION,
        "build": {
            "build_id": build_id,
            "build_input_hash": build_input,
            "build_generator_version": "factory-build-foundation-v1",
            "job_id": job_id,
            "analysis_artifact_id": analysis_id,
            "source_sha256": source_sha,
            "analyzer_version": analyzer,
        },
        "tool_count": 1,
        "tools": tools,
    }
    package.update(overrides)  # type: ignore[arg-type]
    return package


def _raw_and_meta(package: dict) -> tuple[bytes, dict]:
    raw = encode_canonical_json_bytes(package)
    meta = {
        "size": len(raw),
        "sha": compute_canonical_json_hash(package),
        "generator": package["generator_version"],
        "schema": package["package_schema_version"],
        "build_id": package["build"]["build_id"],
        "build_input_hash": package["build"]["build_input_hash"],
        "job_id": package["build"]["job_id"],
        "analysis_artifact_id": package["build"]["analysis_artifact_id"],
        "source_sha256": package["build"]["source_sha256"],
        "analyzer_version": package["build"]["analyzer_version"],
        "tool_count": package["tool_count"],
    }
    return raw, meta


def test_security_and_contract_pass_on_valid_package() -> None:
    package = _package()
    raw, meta = _raw_and_meta(package)
    security = run_security_checks(
        raw=raw,
        package=package,
        expected_size_bytes=meta["size"],
        expected_content_sha256=meta["sha"],
        expected_generator_version=meta["generator"],
        expected_package_schema_version=meta["schema"],
        expected_build_id=meta["build_id"],
        expected_build_input_hash=meta["build_input_hash"],
        expected_job_id=meta["job_id"],
        expected_analysis_artifact_id=meta["analysis_artifact_id"],
        expected_source_sha256=meta["source_sha256"],
        expected_analyzer_version=meta["analyzer_version"],
        expected_tool_count=meta["tool_count"],
    )
    assert security["passed"] is True
    assert security["policy_version"] == FACTORY_SECURITY_POLICY_VERSION

    contract = run_contract_checks(
        package=package,
        expected_job_id=meta["job_id"],
        expected_analysis_artifact_id=meta["analysis_artifact_id"],
        expected_source_sha256=meta["source_sha256"],
        expected_analyzer_version=meta["analyzer_version"],
        expected_build_id=meta["build_id"],
        expected_build_input_hash=meta["build_input_hash"],
    )
    assert contract["passed"] is True
    assert contract["policy_version"] == FACTORY_CONTRACT_POLICY_VERSION
    assert contract["tool_count"] == 1


def test_security_fail_closed_on_size_and_hash_mismatch() -> None:
    package = _package()
    raw, meta = _raw_and_meta(package)
    with pytest.raises(SandboxSecurityError) as size_exc:
        run_security_checks(
            raw=raw + b"x",
            package=package,
            expected_size_bytes=meta["size"],
            expected_content_sha256=meta["sha"],
            expected_generator_version=meta["generator"],
            expected_package_schema_version=meta["schema"],
            expected_build_id=meta["build_id"],
            expected_build_input_hash=meta["build_input_hash"],
            expected_job_id=meta["job_id"],
            expected_analysis_artifact_id=meta["analysis_artifact_id"],
            expected_source_sha256=meta["source_sha256"],
            expected_analyzer_version=meta["analyzer_version"],
            expected_tool_count=meta["tool_count"],
        )
    assert size_exc.value.code == "FACTORY_PACKAGE_SIZE_MISMATCH"

    with pytest.raises(SandboxSecurityError) as hash_exc:
        run_security_checks(
            raw=raw,
            package=package,
            expected_size_bytes=meta["size"],
            expected_content_sha256="0" * 64,
            expected_generator_version=meta["generator"],
            expected_package_schema_version=meta["schema"],
            expected_build_id=meta["build_id"],
            expected_build_input_hash=meta["build_input_hash"],
            expected_job_id=meta["job_id"],
            expected_analysis_artifact_id=meta["analysis_artifact_id"],
            expected_source_sha256=meta["source_sha256"],
            expected_analyzer_version=meta["analyzer_version"],
            expected_tool_count=meta["tool_count"],
        )
    assert hash_exc.value.code == "FACTORY_PACKAGE_HASH_MISMATCH"


def test_security_rejects_forbidden_executable_and_secret_fields() -> None:
    package = _package()
    package["tools"][0]["command"] = "rm -rf /"
    raw, meta = _raw_and_meta(package)
    with pytest.raises(SandboxSecurityError) as exc:
        run_security_checks(
            raw=raw,
            package=package,
            expected_size_bytes=meta["size"],
            expected_content_sha256=meta["sha"],
            expected_generator_version=meta["generator"],
            expected_package_schema_version=meta["schema"],
            expected_build_id=meta["build_id"],
            expected_build_input_hash=meta["build_input_hash"],
            expected_job_id=meta["job_id"],
            expected_analysis_artifact_id=meta["analysis_artifact_id"],
            expected_source_sha256=meta["source_sha256"],
            expected_analyzer_version=meta["analyzer_version"],
            expected_tool_count=meta["tool_count"],
        )
    assert exc.value.code == "FACTORY_PACKAGE_FORBIDDEN_CONTENT"

    package2 = _package()
    package2["tools"][0]["summary"] = "Authorization: Bearer secret-token"
    raw2, meta2 = _raw_and_meta(package2)
    with pytest.raises(SandboxSecurityError) as secret_exc:
        run_security_checks(
            raw=raw2,
            package=package2,
            expected_size_bytes=meta2["size"],
            expected_content_sha256=meta2["sha"],
            expected_generator_version=meta2["generator"],
            expected_package_schema_version=meta2["schema"],
            expected_build_id=meta2["build_id"],
            expected_build_input_hash=meta2["build_input_hash"],
            expected_job_id=meta2["job_id"],
            expected_analysis_artifact_id=meta2["analysis_artifact_id"],
            expected_source_sha256=meta2["source_sha256"],
            expected_analyzer_version=meta2["analyzer_version"],
            expected_tool_count=meta2["tool_count"],
        )
    assert secret_exc.value.code == "FACTORY_PACKAGE_FORBIDDEN_CONTENT"


def test_contract_fail_closed_on_duplicate_operation_and_bad_method() -> None:
    package = _package()
    dup = copy.deepcopy(package["tools"][0])
    dup["candidate_id"] = str(uuid.uuid4())
    package["tools"].append(dup)
    package["tool_count"] = 2
    with pytest.raises(SandboxContractError) as dup_exc:
        run_contract_checks(
            package=package,
            expected_job_id=package["build"]["job_id"],
            expected_analysis_artifact_id=package["build"]["analysis_artifact_id"],
            expected_source_sha256=package["build"]["source_sha256"],
            expected_analyzer_version=package["build"]["analyzer_version"],
            expected_build_id=package["build"]["build_id"],
            expected_build_input_hash=package["build"]["build_input_hash"],
        )
    assert dup_exc.value.code == "FACTORY_CONTRACT_DUPLICATE"

    package2 = _package()
    package2["tools"][0]["method"] = "TRACE"
    with pytest.raises(SandboxContractError) as method_exc:
        run_contract_checks(
            package=package2,
            expected_job_id=package2["build"]["job_id"],
            expected_analysis_artifact_id=package2["build"]["analysis_artifact_id"],
            expected_source_sha256=package2["build"]["source_sha256"],
            expected_analyzer_version=package2["build"]["analyzer_version"],
            expected_build_id=package2["build"]["build_id"],
            expected_build_input_hash=package2["build"]["build_input_hash"],
        )
    assert method_exc.value.code == "FACTORY_CONTRACT_INVALID"


def test_contract_rejects_non_json_compatible_schema() -> None:
    package = _package()
    package["tools"][0]["request_schema"] = {"bad": {1, 2, 3}}  # type: ignore[dict-item]
    # Force through JSON round-trip failure path by injecting a non-serializable
    # structure that survives as a set only in-memory (validators see the set).
    with pytest.raises(SandboxContractError) as exc:
        run_contract_checks(
            package=package,
            expected_job_id=package["build"]["job_id"],
            expected_analysis_artifact_id=package["build"]["analysis_artifact_id"],
            expected_source_sha256=package["build"]["source_sha256"],
            expected_analyzer_version=package["build"]["analyzer_version"],
            expected_build_id=package["build"]["build_id"],
            expected_build_input_hash=package["build"]["build_input_hash"],
        )
    assert exc.value.code == "FACTORY_CONTRACT_INVALID"


def test_policy_versions_are_stable() -> None:
    assert FACTORY_SECURITY_POLICY_VERSION == "factory-security-policy-v1"
    assert FACTORY_CONTRACT_POLICY_VERSION == "factory-contract-policy-v1"
    # Ensure validators do not accidentally require package JSON dumps of secrets.
    package = _package()
    dumped = json.dumps(package)
    assert "Authorization" not in dumped
    assert "Bearer" not in dumped
