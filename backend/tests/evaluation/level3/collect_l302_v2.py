"""Level 3 L3-02 v2: gated live observation collection, NOT automatic scoring.

Before genuine two-person gold approval the run path fails closed without DB or
LLM access. Captured structured outputs go to a private 0700 directory; the
ordinary artifact contains only per-case hashes and non-sensitive diagnostics.
Human semantic adjudication is handled separately by scoring_l302_v2.py.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import stat
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.agent.request_analyzer import RequestAnalyzerService
from app.model_provider.client import LLMConnectionTarget, ModelProviderClient
from app.model_provider.errors import ModelProviderError
from app.schemas.structured_request import (
    STRUCTURED_REQUEST_SCHEMA_VERSION,
    StructuredRequestV1,
)
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.evaluation.harness.common import (
    artifact_dir,
    environment_label,
    git_sha,
    measured_at,
)
from tests.evaluation.level3.measure_l3_02 import (
    _safe_validation_issue_summary,
    db_url,
    preflight_environment,
    target_min_percent,
)
from tests.evaluation.level3.scoring_l302_v2 import (
    DATASET_VERSION,
    gold_gate,
)

GOLD_PATH = Path(__file__).resolve().parent / "dataset_l302.v2.yaml"
SCHEMA_VERSION = "mcpflow.level3_l302_observations.v2"


def _gold_bytes() -> tuple[dict[str, Any], str]:
    import yaml

    raw = GOLD_PATH.read_bytes()
    parsed = yaml.safe_load(raw)
    if not isinstance(parsed, dict):
        raise ValueError("INVALID_GOLD_FILE")
    return parsed, hashlib.sha256(raw).hexdigest()


def _canonical(raw: dict[str, Any]) -> bytes:
    return json.dumps(
        raw, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def _private_dir() -> Path:
    """Never silently lower privacy on preexisting observation directories."""
    path = artifact_dir("level3") / "l302-v2-private"
    if path.is_symlink():
        raise ValueError("PRIVATE_EVIDENCE_SYMLINK")
    path.mkdir(mode=0o700, exist_ok=True)
    if not path.is_dir() or stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise ValueError("PRIVATE_EVIDENCE_PERMISSIONS")
    return path


def _write_exclusive(path: Path, data: dict[str, Any]) -> None:
    # Never overwrite or follow symlinks; restrict file to current UID.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(_canonical(data) + b"\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


async def observe_one(
    *,
    provider: ModelProviderClient,
    analyzer: RequestAnalyzerService,
    target: LLMConnectionTarget,
    parameters: dict[str, Any] | None,
    system_instruction: str,
    case: dict[str, Any],
) -> dict[str, Any]:
    """Capture a minimal audit record; never include raw provider exception text."""
    started = time.perf_counter_ns()
    observation: dict[str, Any] = {"case_id": case["id"], "schema_valid": False}
    try:
        messages = analyzer._build_messages(
            system_instruction=system_instruction,
            raw_request_text=case["request_text"],
        )
        response = await provider.generate_json(
            target, messages=messages, parameters=parameters
        )
        if not isinstance(response, dict):
            raise ValueError("NON_OBJECT_RESPONSE")
        payload = dict(response)
        # Same trusted system-owned fields as v1 evaluation.
        payload["schema_version"] = STRUCTURED_REQUEST_SCHEMA_VERSION
        payload["request_text"] = case["request_text"]
        structured = StructuredRequestV1.model_validate(payload)
        structured = RequestAnalyzerService._sanitize_request_level_clarification(
            structured
        )
        observation["schema_valid"] = True
        observation["structured"] = structured.model_dump(mode="json")
    except ValidationError as exc:
        observation["error_code"] = "VALIDATION_ERROR"
        observation["validation_issues"] = _safe_validation_issue_summary(exc)
    except ModelProviderError as exc:
        observation["error_code"] = exc.error_code
    except ValueError:
        observation["error_code"] = "VALIDATION_ERROR"
    except Exception:
        observation["error_code"] = "UNEXPECTED_ERROR"

    # Elapsed duration is outside the hashed observation. Avoid nondeterminism
    # between private/published hashes of semantic content.
    elapsed_ms = round((time.perf_counter_ns() - started) / 1_000_000, 3)
    return {
        "private_observation": observation,
        "public_case": {
            "case_id": case["id"],
            "family": case["family"],
            "schema_valid": observation["schema_valid"],
            "observed_sha256": hashlib.sha256(_canonical(observation)).hexdigest(),
            "error_code": observation.get("error_code"),
            "validation_issues": observation.get("validation_issues", []),
            "elapsed_ms": elapsed_ms,
            "review_state": "PENDING_INDEPENDENT_SEMANTIC_REVIEWS",
        },
    }


def _review_gate() -> tuple[dict[str, Any], str, dict[str, Any]]:
    gold, digest = _gold_bytes()
    return gold, digest, gold_gate(gold)


async def execute(args: argparse.Namespace) -> dict[str, Any]:
    sha = git_sha()
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "metric_id": "L3-02",
        "dataset_version": DATASET_VERSION,
        "mode": "preflight" if args.preflight else "run",
        "measured_at": measured_at(),
        "git_sha": sha,
        "environment": environment_label(),
        "target_min_percent": target_min_percent(),
        "judgment": "INCOMPLETE",
        "measurement_scope": (
            "Live Analyzer JSON generation and schema validation only; "
            "no Tool selection, Tool execution or AgentRequest DB writes. "
            "Semantic scores require independent reviewer records."
        ),
    }
    try:
        gold, digest, gate = _review_gate()
    except (ValueError, OSError) as exc:
        report.update(
            ok=False, run_permitted=False,
            reason="GOLD_FILE_UNAVAILABLE" if isinstance(exc, OSError) else "INVALID_GOLD_FILE",
        )
        return report
    report["gold_sha256"] = digest
    report["gold_review_status"] = gold.get("review_status")
    report["method_review_status"] = gold.get("method_review_status")
    report["run_permitted"] = gate["ok"]
    report["gate_reason"] = gate["reason"]
    if args.preflight:
        report.update(ok=True, note="Gold gate only; no DB or model calls.")
        return report
    if not gate["ok"]:
        report.update(ok=False, reason=gate["reason"])
        return report
    if not args.ack_gold_reviewed:
        report.update(ok=False, reason="GOLD_REVIEW_ACK_REQUIRED")
        return report
    if sha in {"", "unknown"}:
        report.update(ok=False, reason="GIT_SHA_UNKNOWN")
        return report
    try:
        agent_version_id = uuid.UUID(args.agent_version_id)
    except (TypeError, ValueError):
        report.update(ok=False, reason="AGENT_VERSION_ID_INVALID")
        return report
    url = db_url()
    if not url:
        report.update(ok=False, reason="DATABASE_URL_MISSING")
        return report

    engine = create_async_engine(url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            env, version, profile = await preflight_environment(
                session, agent_version_id=agent_version_id
            )
            report["preflight"] = env
            if not env["ok"]:
                report.update(ok=False, reason=env["reason"])
                return report
            target = LLMConnectionTarget(
                provider=profile.provider,
                model=profile.model,
                base_url=profile.base_url,
                credential_secret_id=profile.credential_secret_id,
                request_timeout_ms=profile.request_timeout_ms,
            )
            params = dict(profile.parameters) if profile.parameters else None
            report["agent_version_id"] = str(agent_version_id)
            report["run_ref"] = str(uuid.uuid4())
            report["gold_reviewed_by"] = gold["reviewed_by"]
            report["gold_reviewed_at"] = gold["reviewed_at"]

            private_rows: list[dict[str, Any]] = []
            public_rows: list[dict[str, Any]] = []
            provider = ModelProviderClient()
            try:
                analyzer = RequestAnalyzerService(session, model_provider=provider)
                for case in gold["cases"]:
                    item = await observe_one(
                        provider=provider,
                        analyzer=analyzer,
                        target=target,
                        parameters=params,
                        system_instruction=version.system_instruction,
                        case=case,
                    )
                    private_rows.append(item["private_observation"])
                    public_rows.append(item["public_case"])
            finally:
                await provider.aclose()
            report["case_results"] = public_rows
            report["observed_cases"] = len(public_rows)
            report["reason"] = "PENDING_INDEPENDENT_SEMANTIC_REVIEWS"
            report["ok"] = len(public_rows) == 20

            if args.write_artifact:
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
                base = f"l3_02_v2_{sha[:12]}_{stamp}_{report['run_ref']}"
                private_path = _private_dir() / (base + ".json")
                private_payload = {
                    "schema_version": SCHEMA_VERSION,
                    "run_ref": report["run_ref"],
                    "gold_sha256": digest,
                    "git_sha": sha,
                    "profile_id": env["llm_profile"]["id"],
                    "profile_lock_version": env["llm_profile"]["lock_version"],
                    "observations": private_rows,
                }
                _write_exclusive(private_path, private_payload)
                report["private_evidence_sha256"] = hashlib.sha256(
                    _canonical(private_payload) + b"\n"
                ).hexdigest()
                public_path = artifact_dir("level3") / (base + ".json")
                # No raw model output is ever included in the public artifact.
                _write_exclusive(public_path, report)
                report["artifact_path"] = str(public_path)
                report["private_evidence_path"] = str(private_path)
    finally:
        await engine.dispose()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="L3-02 v2 approved-gold live observation collector"
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--preflight", action="store_true")
    modes.add_argument("--run", action="store_true")
    parser.add_argument("--agent-version-id")
    parser.add_argument("--ack-gold-reviewed", action="store_true")
    parser.add_argument("--write-artifact", action="store_true")
    args = parser.parse_args(argv)
    report = asyncio.run(execute(args))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
