"""Versioned Factory sandbox contract policy for Generated Packages (Issue #91).

Validates declarative tool contracts only. Never executes package content.
"""

from __future__ import annotations

from typing import Any

FACTORY_CONTRACT_POLICY_VERSION = "factory-contract-policy-v1"

_ALLOWED_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
)

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


class SandboxContractError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _is_json_compatible(node: Any, *, depth: int = 0) -> bool:
    if depth > 32:
        return False
    if node is None or isinstance(node, (bool, int, float, str)):
        return True
    if isinstance(node, list):
        return all(_is_json_compatible(item, depth=depth + 1) for item in node)
    if isinstance(node, dict):
        return all(
            isinstance(key, str) and _is_json_compatible(value, depth=depth + 1)
            for key, value in node.items()
        )
    return False


def _validate_parameters(parameters: Any) -> str | None:
    if not isinstance(parameters, list):
        return "parameters must be a list"
    for idx, item in enumerate(parameters):
        if not isinstance(item, dict):
            return f"parameters[{idx}] must be an object"
        if not _is_json_compatible(item):
            return f"parameters[{idx}] is not JSON-compatible"
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            return f"parameters[{idx}].name must be a non-empty string"
        location = item.get("in")
        if location is not None and (
            not isinstance(location, str) or not location.strip()
        ):
            return f"parameters[{idx}].in must be a non-empty string when set"
    return None


def run_contract_checks(
    *,
    package: dict[str, Any],
    expected_job_id: str,
    expected_analysis_artifact_id: str,
    expected_source_sha256: str,
    expected_analyzer_version: str,
    expected_build_id: str,
    expected_build_input_hash: str,
    policy_version: str = FACTORY_CONTRACT_POLICY_VERSION,
) -> dict[str, Any]:
    """Return structured contract evidence or raise SandboxContractError."""

    tools = package.get("tools")
    if not isinstance(tools, list) or not tools:
        raise SandboxContractError(
            "FACTORY_CONTRACT_INVALID",
            "Generated Package tools must be a non-empty list.",
        )

    build = package.get("build")
    if not isinstance(build, dict):
        raise SandboxContractError(
            "FACTORY_CONTRACT_LINEAGE_MISMATCH",
            "Generated Package build lineage is missing.",
        )

    checks: list[dict[str, Any]] = []
    seen_ops: set[str] = set()
    seen_candidate_ids: set[str] = set()

    for idx, tool in enumerate(tools):
        if not isinstance(tool, dict):
            raise SandboxContractError(
                "FACTORY_CONTRACT_INVALID",
                f"Tool entry at index {idx} is not an object.",
            )
        for field in _REQUIRED_TOOL_FIELDS:
            value = tool.get(field)
            if not isinstance(value, str) or not value.strip():
                raise SandboxContractError(
                    "FACTORY_CONTRACT_INVALID",
                    f"tools[{idx}].{field} must be a non-empty string.",
                )

        candidate_id = str(tool["candidate_id"])
        if candidate_id in seen_candidate_ids:
            raise SandboxContractError(
                "FACTORY_CONTRACT_DUPLICATE",
                f"Duplicate candidate_id {candidate_id}.",
            )
        seen_candidate_ids.add(candidate_id)

        operation_key = str(tool["operation_key"])
        if operation_key in seen_ops:
            raise SandboxContractError(
                "FACTORY_CONTRACT_DUPLICATE",
                f"Duplicate operation_key {operation_key}.",
            )
        seen_ops.add(operation_key)

        method = str(tool["method"]).upper()
        if method not in _ALLOWED_METHODS:
            raise SandboxContractError(
                "FACTORY_CONTRACT_INVALID",
                f"tools[{idx}].method {method!r} is not an allowed HTTP method.",
            )
        path = str(tool["path"])
        if not path.startswith("/"):
            raise SandboxContractError(
                "FACTORY_CONTRACT_INVALID",
                f"tools[{idx}].path must start with '/'.",
            )

        if str(tool["job_id"]) != expected_job_id:
            raise SandboxContractError(
                "FACTORY_CONTRACT_LINEAGE_MISMATCH",
                f"tools[{idx}].job_id does not match evidence.",
            )
        if str(tool["analysis_artifact_id"]) != expected_analysis_artifact_id:
            raise SandboxContractError(
                "FACTORY_CONTRACT_LINEAGE_MISMATCH",
                f"tools[{idx}].analysis_artifact_id does not match evidence.",
            )
        if str(tool["source_sha256"]) != expected_source_sha256:
            raise SandboxContractError(
                "FACTORY_CONTRACT_LINEAGE_MISMATCH",
                f"tools[{idx}].source_sha256 does not match evidence.",
            )
        if str(tool["analyzer_version"]) != expected_analyzer_version:
            raise SandboxContractError(
                "FACTORY_CONTRACT_LINEAGE_MISMATCH",
                f"tools[{idx}].analyzer_version does not match evidence.",
            )

        param_err = _validate_parameters(tool.get("parameters", []))
        if param_err is not None:
            raise SandboxContractError(
                "FACTORY_CONTRACT_INVALID",
                f"tools[{idx}]: {param_err}",
            )
        for schema_field in ("request_schema", "response_schema"):
            schema = tool.get(schema_field)
            if schema is not None and not _is_json_compatible(schema):
                raise SandboxContractError(
                    "FACTORY_CONTRACT_INVALID",
                    f"tools[{idx}].{schema_field} is not JSON-compatible.",
                )

        checks.append(
            {
                "operation_key": operation_key,
                "candidate_id": candidate_id,
                "method": method,
                "path": path,
                "passed": True,
            }
        )

    # Package-level lineage agreement with Build evidence.
    if str(build.get("build_id")) != expected_build_id:
        raise SandboxContractError(
            "FACTORY_CONTRACT_LINEAGE_MISMATCH",
            "Package build_id does not match evidence.",
        )
    if str(build.get("build_input_hash")) != expected_build_input_hash:
        raise SandboxContractError(
            "FACTORY_CONTRACT_LINEAGE_MISMATCH",
            "Package build_input_hash does not match evidence.",
        )

    return {
        "policy_version": policy_version,
        "passed": True,
        "tool_count": len(tools),
        "checks": checks,
        "failures": [],
    }
