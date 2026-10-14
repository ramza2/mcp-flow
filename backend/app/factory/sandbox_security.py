"""Versioned Factory sandbox security policy for Generated Packages (Issue #91).

Validates Object Storage integrity + declarative package structure only.
Never executes package content.
"""

from __future__ import annotations

from typing import Any

from app.core.canonical_hash import compute_canonical_json_hash
from app.factory.package_generator import (
    FACTORY_PACKAGE_GENERATOR_VERSION,
    PACKAGE_KIND,
    PACKAGE_SCHEMA_VERSION,
)

FACTORY_SECURITY_POLICY_VERSION = "factory-security-policy-v1"

_FORBIDDEN_TOOL_KEYS = frozenset(
    {
        "command",
        "script",
        "executable",
        "install",
        "subprocess",
        "shell",
        "docker",
        "dockerfile",
        "entrypoint",
        "cmd",
        "process",
        "spawn",
        "env",
        "environment",
        "secrets",
        "secret",
        "password",
        "api_key",
        "apikey",
        "authorization",
        "auth_header",
        "bearer",
        "private_key",
        "credential",
        "credentials",
        "token",
        "access_token",
        "refresh_token",
        "client_secret",
        "raw_source",
        "openapi",
    }
)

_SECRETISH_SUBSTRINGS = (
    "authorization:",
    "bearer ",
    "api_key=",
    "apikey=",
    "client_secret=",
    "password=",
    "-----begin ",
)


class SandboxSecurityError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _walk_forbidden(node: Any, *, path: str = "$") -> str | None:
    if isinstance(node, dict):
        for key, value in node.items():
            key_l = str(key).lower()
            child = f"{path}.{key}"
            if key_l in _FORBIDDEN_TOOL_KEYS:
                return child
            found = _walk_forbidden(value, path=child)
            if found is not None:
                return found
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            found = _walk_forbidden(item, path=f"{path}[{idx}]")
            if found is not None:
                return found
    elif isinstance(node, str):
        lowered = node.lower()
        for needle in _SECRETISH_SUBSTRINGS:
            if needle in lowered:
                return path
    return None


def validate_package_integrity(
    *,
    raw: bytes,
    package: dict[str, Any],
    expected_size_bytes: int,
    expected_content_sha256: str,
    expected_generator_version: str,
    expected_package_schema_version: str,
    expected_build_id: str,
    expected_build_input_hash: str,
    expected_job_id: str,
    expected_analysis_artifact_id: str,
    expected_source_sha256: str,
    expected_analyzer_version: str,
    expected_tool_count: int,
) -> list[dict[str, Any]]:
    """Side-effect-free Object Storage + package lineage integrity checks.

    Shared by enqueue-time gating and versioned security policy. Never executes
    package content.
    """

    checks: list[dict[str, Any]] = []

    def _ok(name: str, detail: str | None = None) -> None:
        row: dict[str, Any] = {"name": name, "passed": True}
        if detail is not None:
            row["detail"] = detail
        checks.append(row)

    if len(raw) != int(expected_size_bytes):
        raise SandboxSecurityError(
            "FACTORY_PACKAGE_SIZE_MISMATCH",
            "Generated Package object size does not match persisted evidence.",
        )
    _ok("object_size_match", str(len(raw)))

    actual_hash = compute_canonical_json_hash(package)
    if actual_hash != expected_content_sha256:
        raise SandboxSecurityError(
            "FACTORY_PACKAGE_HASH_MISMATCH",
            "Generated Package object hash does not match persisted evidence.",
        )
    _ok("object_sha256_match", actual_hash)

    if package.get("kind") != PACKAGE_KIND:
        raise SandboxSecurityError(
            "FACTORY_PACKAGE_SCHEMA_INVALID",
            "Generated Package kind is invalid.",
        )
    _ok("package_kind", PACKAGE_KIND)

    if package.get("package_schema_version") != expected_package_schema_version:
        raise SandboxSecurityError(
            "FACTORY_PACKAGE_SCHEMA_INVALID",
            "Generated Package schema version does not match evidence.",
        )
    if expected_package_schema_version != PACKAGE_SCHEMA_VERSION:
        raise SandboxSecurityError(
            "FACTORY_PACKAGE_SCHEMA_INVALID",
            "Unsupported Generated Package schema version.",
        )
    _ok("package_schema_version", expected_package_schema_version)

    if package.get("generator_version") != expected_generator_version:
        raise SandboxSecurityError(
            "FACTORY_PACKAGE_LINEAGE_MISMATCH",
            "Generated Package generator_version does not match evidence.",
        )
    if expected_generator_version != FACTORY_PACKAGE_GENERATOR_VERSION:
        raise SandboxSecurityError(
            "FACTORY_PACKAGE_LINEAGE_MISMATCH",
            "Unsupported Generated Package generator_version.",
        )
    _ok("generator_version", expected_generator_version)

    build = package.get("build")
    if not isinstance(build, dict):
        raise SandboxSecurityError(
            "FACTORY_PACKAGE_LINEAGE_MISMATCH",
            "Generated Package build lineage is missing.",
        )
    lineage_pairs = (
        ("build_id", expected_build_id),
        ("build_input_hash", expected_build_input_hash),
        ("job_id", expected_job_id),
        ("analysis_artifact_id", expected_analysis_artifact_id),
        ("source_sha256", expected_source_sha256),
        ("analyzer_version", expected_analyzer_version),
    )
    for field, expected in lineage_pairs:
        if str(build.get(field)) != expected:
            raise SandboxSecurityError(
                "FACTORY_PACKAGE_LINEAGE_MISMATCH",
                f"Generated Package build.{field} does not match evidence.",
            )
    _ok("embedded_build_lineage")

    tools = package.get("tools")
    if not isinstance(tools, list):
        raise SandboxSecurityError(
            "FACTORY_PACKAGE_SCHEMA_INVALID",
            "Generated Package tools must be a list.",
        )
    if int(package.get("tool_count", -1)) != expected_tool_count:
        raise SandboxSecurityError(
            "FACTORY_PACKAGE_TOOL_COUNT_MISMATCH",
            "Generated Package tool_count does not match evidence.",
        )
    if len(tools) != expected_tool_count:
        raise SandboxSecurityError(
            "FACTORY_PACKAGE_TOOL_COUNT_MISMATCH",
            "Generated Package tools length does not match tool_count.",
        )
    _ok("tool_count", str(expected_tool_count))
    return checks


def run_security_checks(
    *,
    raw: bytes,
    package: dict[str, Any],
    expected_size_bytes: int,
    expected_content_sha256: str,
    expected_generator_version: str,
    expected_package_schema_version: str,
    expected_build_id: str,
    expected_build_input_hash: str,
    expected_job_id: str,
    expected_analysis_artifact_id: str,
    expected_source_sha256: str,
    expected_analyzer_version: str,
    expected_tool_count: int,
    policy_version: str = FACTORY_SECURITY_POLICY_VERSION,
) -> dict[str, Any]:
    """Return structured security evidence or raise SandboxSecurityError."""

    checks = validate_package_integrity(
        raw=raw,
        package=package,
        expected_size_bytes=expected_size_bytes,
        expected_content_sha256=expected_content_sha256,
        expected_generator_version=expected_generator_version,
        expected_package_schema_version=expected_package_schema_version,
        expected_build_id=expected_build_id,
        expected_build_input_hash=expected_build_input_hash,
        expected_job_id=expected_job_id,
        expected_analysis_artifact_id=expected_analysis_artifact_id,
        expected_source_sha256=expected_source_sha256,
        expected_analyzer_version=expected_analyzer_version,
        expected_tool_count=expected_tool_count,
    )

    tools = package.get("tools")
    assert isinstance(tools, list)  # validated by integrity
    for idx, tool in enumerate(tools):
        if not isinstance(tool, dict):
            raise SandboxSecurityError(
                "FACTORY_PACKAGE_SCHEMA_INVALID",
                f"Tool entry at index {idx} is not an object.",
            )
        forbidden = _walk_forbidden(tool, path=f"$.tools[{idx}]")
        if forbidden is not None:
            raise SandboxSecurityError(
                "FACTORY_PACKAGE_FORBIDDEN_CONTENT",
                f"Forbidden executable/secret-bearing content at {forbidden}.",
            )
    checks.append({"name": "declarative_tools_only", "passed": True})

    return {
        "policy_version": policy_version,
        "passed": True,
        "checks": checks,
    }
