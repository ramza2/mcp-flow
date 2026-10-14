"""Deterministic declarative Generated Package builder (Issue #89).

Reads only an immutable Factory Build candidates_snapshot. Never re-parses
OpenAPI, never executes code, never starts MCP servers.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.core.canonical_hash import compute_canonical_json_hash

PACKAGE_KIND = "FACTORY_GENERATED_PACKAGE"
PACKAGE_SCHEMA_VERSION = "factory-generated-package-v1"
FACTORY_PACKAGE_GENERATOR_VERSION = "factory-package-generator-v1"

_REQUIRED_TOOL_FIELDS = (
    "candidate_id",
    "job_id",
    "analysis_artifact_id",
    "source_sha256",
    "analyzer_version",
    "operation_key",
    "method",
    "path",
)


class PackageGenerationError(Exception):
    """Fail-closed package construction / integrity error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def encode_canonical_json_bytes(payload: Any) -> bytes:
    """UTF-8 bytes of canonical JSON (sorted keys, compact separators)."""

    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _tool_contract_from_snapshot(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise PackageGenerationError(
            "FACTORY_PACKAGE_SNAPSHOT_INVALID",
            "Build candidates_snapshot entry must be an object.",
        )
    for field in _REQUIRED_TOOL_FIELDS:
        if field not in entry:
            raise PackageGenerationError(
                "FACTORY_PACKAGE_SNAPSHOT_INVALID",
                f"Build candidates_snapshot missing required field '{field}'.",
            )
    tags = entry.get("tags") if isinstance(entry.get("tags"), list) else []
    parameters = (
        entry.get("parameters") if isinstance(entry.get("parameters"), list) else []
    )
    return {
        "candidate_id": str(entry["candidate_id"]),
        "job_id": str(entry["job_id"]),
        "analysis_artifact_id": str(entry["analysis_artifact_id"]),
        "source_sha256": str(entry["source_sha256"]),
        "analyzer_version": str(entry["analyzer_version"]),
        "operation_key": str(entry["operation_key"]),
        "method": str(entry["method"]),
        "path": str(entry["path"]),
        "operation_id": entry.get("operation_id"),
        "summary": entry.get("summary"),
        "description": entry.get("description"),
        "tags": [str(t) for t in tags],
        "deprecated": bool(entry.get("deprecated", False)),
        "parameters": parameters,
        "request_schema": entry.get("request_schema"),
        "response_schema": entry.get("response_schema"),
    }


def build_generated_package(
    *,
    build_id: str,
    build_input_hash: str,
    build_generator_version: str,
    job_id: str,
    analysis_artifact_id: str,
    source_sha256: str,
    analyzer_version: str,
    candidates_snapshot: list[Any],
    generator_version: str = FACTORY_PACKAGE_GENERATOR_VERSION,
    package_schema_version: str = PACKAGE_SCHEMA_VERSION,
) -> tuple[dict[str, Any], bytes, str]:
    """Build declarative package payload + canonical bytes + content hash.

    Tools are ordered by (operation_key, candidate_id) for stable output even if
    the persisted snapshot order were disturbed.
    """
    if not candidates_snapshot:
        raise PackageGenerationError(
            "FACTORY_PACKAGE_SNAPSHOT_INVALID",
            "Build candidates_snapshot must be a non-empty list.",
        )

    tools = [_tool_contract_from_snapshot(entry) for entry in candidates_snapshot]
    for tool in tools:
        if tool["job_id"] != job_id:
            raise PackageGenerationError(
                "FACTORY_PACKAGE_LINEAGE_MISMATCH",
                "Snapshot tool job_id does not match Build lineage.",
            )
        if tool["analysis_artifact_id"] != analysis_artifact_id:
            raise PackageGenerationError(
                "FACTORY_PACKAGE_LINEAGE_MISMATCH",
                "Snapshot tool analysis_artifact_id does not match Build lineage.",
            )
        if tool["source_sha256"] != source_sha256:
            raise PackageGenerationError(
                "FACTORY_PACKAGE_LINEAGE_MISMATCH",
                "Snapshot tool source_sha256 does not match Build lineage.",
            )
        if tool["analyzer_version"] != analyzer_version:
            raise PackageGenerationError(
                "FACTORY_PACKAGE_LINEAGE_MISMATCH",
                "Snapshot tool analyzer_version does not match Build lineage.",
            )

    tools.sort(key=lambda t: (t["operation_key"], t["candidate_id"]))

    package: dict[str, Any] = {
        "schema_version": 1,
        "kind": PACKAGE_KIND,
        "package_schema_version": package_schema_version,
        "generator_version": generator_version,
        "build": {
            "build_id": build_id,
            "build_input_hash": build_input_hash,
            "build_generator_version": build_generator_version,
            "job_id": job_id,
            "analysis_artifact_id": analysis_artifact_id,
            "source_sha256": source_sha256,
            "analyzer_version": analyzer_version,
        },
        "tool_count": len(tools),
        "tools": tools,
    }
    raw = encode_canonical_json_bytes(package)
    content_sha256 = compute_canonical_json_hash(package)
    if content_sha256 != hashlib.sha256(raw).hexdigest():
        raise PackageGenerationError(
            "FACTORY_PACKAGE_HASH_MISMATCH",
            "Canonical package hash diverged from encoded bytes.",
        )
    return package, raw, content_sha256
