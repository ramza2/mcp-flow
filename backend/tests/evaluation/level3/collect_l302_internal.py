"""L3-02 INTERNAL v1 observation collector; never impersonates independent review.

Uses the same real Analyzer core as v1/v2. Separate human-owner gold gate
must pass BEFORE database or model calls. This collects observations only:
scoring requires a subsequent real single-human eight-criteria review.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import stat
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.agent.request_analyzer import RequestAnalyzerService
from app.model_provider.client import LLMConnectionTarget, ModelProviderClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.evaluation.harness.common import (
    artifact_dir,
    environment_label,
    git_sha,
    load_yaml,
    measured_at,
)
from tests.evaluation.level3.collect_l302_v2 import (
    _canonical,
    _write_exclusive,
    observe_one,
)
from tests.evaluation.level3.measure_l3_02 import (
    db_url,
    preflight_environment,
    target_min_percent,
)
from tests.evaluation.level3.scoring_l302_internal import (
    DATASET_VERSION,
    gold_gate,
)

GOLD_PATH = Path(__file__).resolve().parent / "dataset_l302.internal.v1.yaml"
SCHEMA_VERSION = "mcpflow.level3_l302_observations.internal.v1"


def load_gold() -> tuple[dict[str, Any], str]:
    gold = load_yaml(GOLD_PATH)
    if not isinstance(gold, dict):
        raise ValueError("INTERNAL_GOLD_FILE_INVALID")
    return gold, hashlib.sha256(GOLD_PATH.read_bytes()).hexdigest()


def private_dir() -> Path:
    parent = artifact_dir("level3")
    path = parent / "l302-internal-private"
    if path.is_symlink():
        raise ValueError("PRIVATE_EVIDENCE_SYMLINK")
    path.mkdir(mode=0o700, exist_ok=True)
    if not path.is_dir() or stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise ValueError("PRIVATE_EVIDENCE_PERMISSIONS")
    return path


async def execute(args: argparse.Namespace) -> dict[str, Any]:
    sha = git_sha()
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "metric_id": "L3-02",
        "dataset_version": DATASET_VERSION,
        "mode": "preflight" if args.preflight else "run",
        "measured_at": measured_at(),
        "git_sha": sha,
        "environment": environment_label(),
        "target_min_percent": target_min_percent(),
        "assessment_kind": "INTERNAL_SELF_ASSESSMENT_ONLY",
        "judgment": "INCOMPLETE",
        "measurement_scope": (
            "Production Analyzer JSON generation and schema validation only; "
            "no Tool execution; separate actual human semantic review required."
        ),
    }
    try:
        gold, gold_digest = load_gold()
        gate = gold_gate(gold)
    except (OSError, ValueError):
        result.update(
            ok=False, run_permitted=False, reason="INTERNAL_GOLD_FILE_INVALID"
        )
        return result
    result.update(
        gold_sha256=gold_digest,
        gold_review_status=gold.get("review_status"),
        method_review_status=gold.get("method_review_status"),
        run_permitted=gate["ok"],
        gate_reason=gate["reason"],
    )
    if args.preflight:
        result.update(ok=True, note="Gold approval gate only. No DB or LLM calls.")
        return result
    if not gate["ok"]:
        result.update(ok=False, reason=gate["reason"])
        return result
    if not args.ack_gold_reviewed:
        result.update(ok=False, reason="INTERNAL_GOLD_REVIEW_ACK_REQUIRED")
        return result
    if sha in {"", "unknown"}:
        result.update(ok=False, reason="GIT_SHA_UNKNOWN")
        return result
    try:
        agent_version_id = uuid.UUID(args.agent_version_id)
    except (ValueError, TypeError):
        result.update(ok=False, reason="AGENT_VERSION_ID_INVALID")
        return result
    url = db_url()
    if not url:
        result.update(ok=False, reason="DATABASE_URL_MISSING")
        return result

    engine = create_async_engine(url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            env, version, profile = await preflight_environment(
                session, agent_version_id=agent_version_id
            )
            result["preflight"] = env
            if not env["ok"]:
                result.update(ok=False, reason=env["reason"])
                return result
            target = LLMConnectionTarget(
                provider=profile.provider,
                model=profile.model,
                base_url=profile.base_url,
                credential_secret_id=profile.credential_secret_id,
                request_timeout_ms=profile.request_timeout_ms,
            )
            params = dict(profile.parameters) if profile.parameters else None
            run_ref = str(uuid.uuid4())
            result["agent_version_id"] = str(agent_version_id)
            result["run_ref"] = run_ref
            result["gold_reviewed_by"] = gold["reviewed_by"]
            result["gold_reviewed_at"] = gold["reviewed_at"]

            public_cases: list[dict[str, Any]] = []
            private_cases: list[dict[str, Any]] = []
            provider = ModelProviderClient()
            try:
                analyzer = RequestAnalyzerService(
                    session, model_provider=provider
                )
                for case in gold["cases"]:
                    observation = await observe_one(
                        provider=provider,
                        analyzer=analyzer,
                        target=target,
                        parameters=params,
                        system_instruction=version.system_instruction,
                        case=case,
                    )
                    public_cases.append(observation["public_case"])
                    private_cases.append(observation["private_observation"])
            finally:
                await provider.aclose()

            result["case_results"] = public_cases
            result["observed_cases"] = len(public_cases)
            result["reason"] = "PENDING_ACTUAL_HUMAN_SEMANTIC_REVIEW"
            result["ok"] = len(public_cases) == 20

            if args.write_artifact:
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
                base = f"l3_02_internal_{sha[:12]}_{stamp}_{run_ref}"
                secret_path = private_dir() / (base + ".json")
                private = {
                    "schema_version": SCHEMA_VERSION,
                    "run_ref": run_ref,
                    "gold_sha256": gold_digest,
                    "git_sha": sha,
                    "profile_id": env["llm_profile"]["id"],
                    "profile_lock_version": env["llm_profile"]["lock_version"],
                    "observations": private_cases,
                }
                _write_exclusive(secret_path, private)
                result["private_evidence_sha256"] = hashlib.sha256(
                    _canonical(private) + b"\n"
                ).hexdigest()
                public_path = artifact_dir("level3") / (base + ".json")
                _write_exclusive(public_path, result)
                result["artifact_path"] = str(public_path)
                result["private_evidence_path"] = str(secret_path)
    finally:
        await engine.dispose()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="L3-02 internal self-assessment live observation collector"
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--preflight", action="store_true")
    modes.add_argument("--run", action="store_true")
    parser.add_argument("--agent-version-id")
    parser.add_argument("--ack-gold-reviewed", action="store_true")
    parser.add_argument("--write-artifact", action="store_true")
    args = parser.parse_args(argv)
    result = asyncio.run(execute(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
