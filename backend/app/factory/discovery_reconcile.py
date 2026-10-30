"""Factory-aware Discovery reconciliation (Issue #99).

Compares reviewed/imported MCP Tools against remote tools/list using
deterministic ``remote_name`` identity and callable-contract hashing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.core.canonical_hash import compute_canonical_json_hash
from app.factory.mcp_output_schema import factory_mcp_output_schema
from app.mcp.normalize import RemoteToolDescriptor, _json_safe, schema_for_storage

FactoryClassification = Literal[
    "MATCHED",
    "MISSING_EXPECTED",
    "UNEXPECTED_REMOTE",
    "CONTRACT_CHANGED",
]
FactoryReconcileStatus = Literal["CLEAN", "MISMATCHED", "AMBIGUOUS"]

_FACTORY_ANNOTATION_KEYS = (
    "factory_import",
    "method",
    "path",
    "candidate_id",
    "operation_key",
    "deprecated",
)
_FACTORY_SCHEMA_DIALECT = "factory-declarative-v1"
_PACKAGE_LINEAGE_IDENTITY_KEYS = (
    "operation_key",
    "candidate_id",
    "publish_id",
    "review_id",
    "generated_package_id",
)


def has_immutable_factory_lineage(
    *,
    annotations: Any,
    raw_descriptor: Any = None,
    schema_dialect: str | None = None,
) -> bool:
    """True for Factory-imported ToolVersion provenance markers.

    Necessary but not sufficient for expected-set membership: Discovery also
    requires the Tool id to appear in the Server's durable FactoryPublish
    ``mcp_tool_ids``. Requires ``factory_import=true`` plus declarative dialect
    or package identity fields from annotations/raw_descriptor.
    """

    if not isinstance(annotations, dict):
        return False
    if annotations.get("factory_import") is not True:
        return False
    if schema_dialect == _FACTORY_SCHEMA_DIALECT:
        return True
    for key in _PACKAGE_LINEAGE_IDENTITY_KEYS:
        value = annotations.get(key)
        if value is not None and value != "":
            return True
    method = annotations.get("method")
    path = annotations.get("path")
    if method not in (None, "") and path not in (None, ""):
        return True
    if isinstance(raw_descriptor, dict):
        for key in _PACKAGE_LINEAGE_IDENTITY_KEYS:
            value = raw_descriptor.get(key)
            if value is not None and value != "":
                return True
        raw_method = raw_descriptor.get("method")
        raw_path = raw_descriptor.get("path")
        if raw_method not in (None, "") and raw_path not in (None, ""):
            return True
    return False


def factory_contract_fingerprint(
    *,
    remote_name: str,
    input_schema: Any,
    output_schema: Any,
) -> str:
    """Hash invocation-relevant contract fields only (not annotations/description)."""

    return compute_canonical_json_hash(
        {
            "remote_name": remote_name,
            "input_schema": _json_safe(schema_for_storage(input_schema)),
            "output_schema": _json_safe(schema_for_storage(output_schema)),
        }
    )


def extract_factory_lineage_annotations(
    annotations: dict[str, Any] | None,
) -> dict[str, Any]:
    """Copy Factory provenance keys that must survive remote reconcile."""

    if not isinstance(annotations, dict):
        return {}
    out: dict[str, Any] = {}
    for key in _FACTORY_ANNOTATION_KEYS:
        if key in annotations:
            out[key] = annotations[key]
    if out.get("factory_import") is not True:
        # Still mark reconcile evidence as Factory-imported lineage.
        out["factory_import"] = True
    return out


@dataclass(slots=True)
class FactoryReconcileEntry:
    remote_name: str
    classification: FactoryClassification
    expected_contract_hash: str | None = None
    remote_contract_hash: str | None = None


@dataclass(slots=True)
class FactoryReconcilePlan:
    status: FactoryReconcileStatus
    entries: list[FactoryReconcileEntry] = field(default_factory=list)
    error_message: str | None = None

    @property
    def is_clean(self) -> bool:
        return self.status == "CLEAN"

    def counts(self) -> dict[str, int]:
        tallies = {
            "matched": 0,
            "missing_expected": 0,
            "unexpected_remote": 0,
            "contract_changed": 0,
        }
        for entry in self.entries:
            if entry.classification == "MATCHED":
                tallies["matched"] += 1
            elif entry.classification == "MISSING_EXPECTED":
                tallies["missing_expected"] += 1
            elif entry.classification == "UNEXPECTED_REMOTE":
                tallies["unexpected_remote"] += 1
            elif entry.classification == "CONTRACT_CHANGED":
                tallies["contract_changed"] += 1
        return tallies

    def matched_names(self) -> list[str]:
        return sorted(
            e.remote_name for e in self.entries if e.classification == "MATCHED"
        )

    def to_raw(self) -> dict[str, Any]:
        counts = self.counts()
        return {
            "status": self.status,
            "error_message": self.error_message,
            "matched": counts["matched"],
            "missing_expected": counts["missing_expected"],
            "unexpected_remote": counts["unexpected_remote"],
            "contract_changed": counts["contract_changed"],
            "classifications": [
                {
                    "remote_name": e.remote_name,
                    "classification": e.classification,
                    "expected_contract_hash": e.expected_contract_hash,
                    "remote_contract_hash": e.remote_contract_hash,
                }
                for e in sorted(self.entries, key=lambda x: x.remote_name)
            ],
        }


def index_remote_tools_strict(
    remote_tools: list[RemoteToolDescriptor],
) -> tuple[dict[str, RemoteToolDescriptor], str | None]:
    """One-to-one remote_name index; duplicate names → error message."""

    by_name: dict[str, RemoteToolDescriptor] = {}
    for desc in remote_tools:
        name = desc.name
        if not isinstance(name, str) or not name.strip():
            return {}, "Remote tool name must be a non-empty string."
        if name in by_name:
            return {}, f"Duplicate remote tool identity: {name!r}."
        by_name[name] = desc
    return by_name, None


def build_factory_reconcile_plan(
    *,
    expected_by_name: dict[str, dict[str, Any]],
    remote_by_name: dict[str, RemoteToolDescriptor],
) -> FactoryReconcilePlan:
    """Build fail-closed Factory reconciliation plan.

    ``expected_by_name`` maps remote_name →
    ``{input_schema, output_schema}`` from imported ToolVersion (HTTP body).
    Compare against the MCP envelope advertised by Factory Runtime, without
    changing the persisted reviewed HTTP-body contract.
    """

    entries: list[FactoryReconcileEntry] = []
    expected_names = set(expected_by_name)
    remote_names = set(remote_by_name)

    for name in sorted(expected_names & remote_names):
        expected = expected_by_name[name]
        remote = remote_by_name[name]
        expected_fp = factory_contract_fingerprint(
            remote_name=name,
            input_schema=expected.get("input_schema"),
            output_schema=factory_mcp_output_schema(expected.get("output_schema")),
        )
        remote_fp = factory_contract_fingerprint(
            remote_name=remote.name,
            input_schema=remote.input_schema,
            output_schema=remote.output_schema,
        )
        if expected_fp == remote_fp:
            entries.append(
                FactoryReconcileEntry(
                    remote_name=name,
                    classification="MATCHED",
                    expected_contract_hash=expected_fp,
                    remote_contract_hash=remote_fp,
                )
            )
        else:
            entries.append(
                FactoryReconcileEntry(
                    remote_name=name,
                    classification="CONTRACT_CHANGED",
                    expected_contract_hash=expected_fp,
                    remote_contract_hash=remote_fp,
                )
            )

    for name in sorted(expected_names - remote_names):
        expected = expected_by_name[name]
        entries.append(
            FactoryReconcileEntry(
                remote_name=name,
                classification="MISSING_EXPECTED",
                expected_contract_hash=factory_contract_fingerprint(
                    remote_name=name,
                    input_schema=expected.get("input_schema"),
                    output_schema=factory_mcp_output_schema(expected.get("output_schema")),
                ),
            )
        )

    for name in sorted(remote_names - expected_names):
        remote = remote_by_name[name]
        entries.append(
            FactoryReconcileEntry(
                remote_name=name,
                classification="UNEXPECTED_REMOTE",
                remote_contract_hash=factory_contract_fingerprint(
                    remote_name=remote.name,
                    input_schema=remote.input_schema,
                    output_schema=remote.output_schema,
                ),
            )
        )

    has_problem = any(e.classification != "MATCHED" for e in entries)
    return FactoryReconcilePlan(
        status="MISMATCHED" if has_problem else "CLEAN",
        entries=entries,
    )


def factory_diff_as_generic_counts(plan: FactoryReconcilePlan) -> dict[str, int]:
    """Map Factory classifications onto legacy DiscoveryDiffSummary buckets."""

    counts = plan.counts()
    return {
        "added": counts["unexpected_remote"],
        "changed": counts["contract_changed"],
        "missing": counts["missing_expected"],
        "unchanged": counts["matched"],
    }
