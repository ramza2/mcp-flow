"""Read-only live Analyzer-core evaluation for approved Level 3 L3-02.

Uses the production RequestAnalyzerService prompt and sanitization methods,
production ModelProviderClient.generate_json, and StructuredRequestV1 validation.
Unlike the durable AgentRequest lifecycle, this harness never writes DB rows.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.agent.request_analyzer import RequestAnalyzerService
from app.model_provider.client import LLMConnectionTarget, ModelProviderClient
from app.model_provider.errors import ModelProviderError
from app.repositories.agent import AgentRepository
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.llm_profile import LLMProfileRepository
from app.schemas.agent import CANONICAL_REQUEST_SCHEMA_VERSION
from app.schemas.structured_request import (
    STRUCTURED_REQUEST_SCHEMA_VERSION,
    StructuredRequestV1,
)
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.evaluation.harness.common import (
    artifact_dir,
    environment_label,
    git_sha,
    load_yaml,
    measured_at,
)
from tests.evaluation.level3.scoring_l302 import (
    judge_l3_02,
    known_families,
    score_case,
)

ROOT = Path(__file__).resolve().parent
DATASET_FILE = ROOT / "dataset_l302.v1.yaml"
TARGETS_FILE = ROOT / "targets.v1.yaml"
DATASET_VERSION = "level3-natural-language-analysis.v1"
SCHEMA_VERSION = "mcpflow.level3_l302_natural_language_analysis.v1"


def db_url() -> str:
    return (
        os.getenv("TEST_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or os.getenv("MCPFLOW_DATABASE_URL")
        or ""
    ).strip()


def load_dataset() -> dict[str, Any]:
    data = load_yaml(DATASET_FILE)
    if not isinstance(data, dict):
        raise ValueError("Dataset must be a mapping")
    return data


def validate_dataset(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("dataset_version") != DATASET_VERSION:
        return {"ok": False, "reason": "DATASET_VERSION_MISMATCH"}
    cases = data.get("cases")
    if not isinstance(cases, list) or len(cases) != 20:
        return {"ok": False, "reason": "EXPECTED_20_FIXED_CASES"}
    ids: set[str] = set()
    families: set[str] = set()
    for row in cases:
        if not isinstance(row, dict):
            return {"ok": False, "reason": "INVALID_CASE"}
        case_id = row.get("id")
        family = row.get("family")
        raw = row.get("request_text")
        entities = row.get("expected_entities")
        if not isinstance(case_id, str) or not case_id.strip() or case_id in ids:
            return {"ok": False, "reason": "DUPLICATE_OR_BLANK_CASE_ID"}
        if not isinstance(raw, str) or not raw.strip():
            return {"ok": False, "reason": "BLANK_REQUEST", "case_id": case_id}
        if not isinstance(family, str) or family not in known_families():
            return {"ok": False, "reason": "UNKNOWN_FAMILY", "case_id": case_id}
        if not isinstance(entities, dict) or any(
            not isinstance(k, str) or not k.strip()
            or not isinstance(v, str) or not v.strip()
            for k, v in entities.items()
        ):
            return {"ok": False, "reason": "INVALID_ENTITY_GOLD", "case_id": case_id}
        ids.add(case_id)
        families.add(family)
    if families != known_families():
        return {"ok": False, "reason": "MISSING_FAMILY"}
    for row in cases:
        family = row["family"]
        temporal = row.get("expected_temporal")
        if family in {"calendar_create", "calendar_list"}:
            if not isinstance(temporal, dict) or not temporal:
                return {"ok": False, "reason": "TEMPORAL_GOLD_MISSING", "case_id": row["id"]}
            if set(temporal) - {"dates", "months", "time_24h"}:
                return {"ok": False, "reason": "TEMPORAL_GOLD_UNKNOWN_KEY", "case_id": row["id"]}
            dates = temporal.get("dates", [])
            months = temporal.get("months", [])
            clock = temporal.get("time_24h")
            if not isinstance(dates, list) or not isinstance(months, list):
                return {"ok": False, "reason": "TEMPORAL_GOLD_INVALID", "case_id": row["id"]}
            import re

            if any(not isinstance(x, str) or not re.fullmatch(r"20\\d{2}-(0[1-9]|1[0-2])-(0[1-9]|[12]\\d|3[01])", x) for x in dates):
                return {"ok": False, "reason": "TEMPORAL_DATE_INVALID", "case_id": row["id"]}
            if any(not isinstance(x, str) or not re.fullmatch(r"20\\d{2}-(0[1-9]|1[0-2])", x) for x in months):
                return {"ok": False, "reason": "TEMPORAL_MONTH_INVALID", "case_id": row["id"]}
            if family == "calendar_create":
                if len(dates) != 1 or months or not isinstance(clock, str) or not re.fullmatch(r"([01]\\d|2[0-3]):[0-5]\\d", clock):
                    return {"ok": False, "reason": "CREATE_DATETIME_GOLD_INVALID", "case_id": row["id"]}
            elif not (dates or months) or clock is not None:
                return {"ok": False, "reason": "LIST_PERIOD_GOLD_INVALID", "case_id": row["id"]}
        elif temporal is not None:
            return {"ok": False, "reason": "UNEXPECTED_TEMPORAL_GOLD", "case_id": row["id"]}
    review = data.get("review_status")
    if review not in {"PENDING_REVIEW", "APPROVED"}:
        return {"ok": False, "reason": "INVALID_GOLD_REVIEW_STATUS"}
    if review == "APPROVED":
        if not isinstance(data.get("reviewed_by"), str) or not data["reviewed_by"].strip():
            return {"ok": False, "reason": "GOLD_REVIEWER_MISSING"}
        if not isinstance(data.get("reviewed_at"), str) or not data["reviewed_at"].strip():
            return {"ok": False, "reason": "GOLD_REVIEW_DATE_MISSING"}
    return {
        "ok": True,
        "dataset_version": DATASET_VERSION,
        "review_status": data.get("review_status"),
        "case_count": len(cases),
        "family_counts": {
            family: sum(x["family"] == family for x in cases)
            for family in sorted(families)
        },
        "sha256": hashlib.sha256(DATASET_FILE.read_bytes()).hexdigest(),
    }


def target_min_percent() -> float:
    targets = load_yaml(TARGETS_FILE)
    return float(targets["targets"]["L3-02"]["target_min_percent"])


async def preflight_environment(
    session: AsyncSession, *, agent_version_id: uuid.UUID
) -> tuple[dict[str, Any], Any | None, Any | None]:
    version = await AgentVersionRepository(session).get(agent_version_id)
    if version is None:
        return {"ok": False, "reason": "AGENT_VERSION_NOT_FOUND"}, None, None
    if version.status != "PUBLISHED":
        return {"ok": False, "reason": "AGENT_VERSION_NOT_PUBLISHED"}, None, None
    if version.request_schema_version != CANONICAL_REQUEST_SCHEMA_VERSION:
        return {"ok": False, "reason": "REQUEST_SCHEMA_INCOMPATIBLE"}, None, None
    agent = await AgentRepository(session).get(version.agent_id)
    if agent is None or agent.status != "ACTIVE":
        return {"ok": False, "reason": "AGENT_NOT_ACTIVE"}, None, None
    profile = await LLMProfileRepository(session).get(version.llm_profile_id)
    if profile is None:
        return {"ok": False, "reason": "LLM_PROFILE_NOT_FOUND"}, None, None
    if profile.provider != "OPENAI_COMPATIBLE":
        return {"ok": False, "reason": "LLM_PROVIDER_UNSUPPORTED"}, None, None

    return {
        "ok": True,
        "agent_version_id": str(agent_version_id),
        "agent_name": agent.name,
        "agent_version_status": version.status,
        "llm_profile": {
            "id": str(profile.id),
            "model": profile.model,
            "provider": profile.provider,
            "lock_version": int(profile.lock_version),
            "has_credential_secret_ref": profile.credential_secret_id is not None,
            # No URL, keys, secret IDs, request headers or provider parameters.
        },
    }, version, profile


async def run_one(
    *,
    provider: ModelProviderClient,
    analyzer: RequestAnalyzerService,
    target: LLMConnectionTarget,
    parameters: dict[str, Any] | None,
    system_instruction: str,
    case: dict[str, Any],
) -> dict[str, Any]:
    started = time.perf_counter_ns()
    row: dict[str, Any] = {
        "case_id": case["id"],
        "family": case["family"],
        "passed": False,
    }
    try:
        text = str(case["request_text"])
        messages = analyzer._build_messages(
            system_instruction=system_instruction,
            raw_request_text=text,
        )
        payload = await provider.generate_json(
            target, messages=messages, parameters=parameters
        )
        if not isinstance(payload, dict):
            raise ValueError("Model response must be a JSON object")
        payload = dict(payload)
        payload["schema_version"] = STRUCTURED_REQUEST_SCHEMA_VERSION
        payload["request_text"] = text
        structured = StructuredRequestV1.model_validate(payload)
        structured = RequestAnalyzerService._sanitize_request_level_clarification(
            structured
        )
        row.update(score_case(case, structured))
        row["schema_valid"] = True
    except (ModelProviderError, ValidationError, ValueError) as exc:
        row["schema_valid"] = False
        row["error_type"] = type(exc).__name__
        row["error_code"] = (
            exc.error_code if isinstance(exc, ModelProviderError) else "VALIDATION_ERROR"
        )
        # Do not capture exception message: remote errors can contain secrets.
    except Exception as exc:
        row["schema_valid"] = False
        row["error_type"] = type(exc).__name__
        row["error_code"] = "UNEXPECTED_ERROR"
    finally:
        row["elapsed_ms"] = round((time.perf_counter_ns() - started) / 1_000_000, 3)
    return row


async def evaluate_cases(
    *,
    session: AsyncSession,
    version: Any,
    profile: Any,
    cases: list[dict[str, Any]],
    provider: ModelProviderClient | None = None,
) -> list[dict[str, Any]]:
    owns_provider = provider is None
    client = provider or ModelProviderClient()
    analyzer = RequestAnalyzerService(session, model_provider=client)
    target = LLMConnectionTarget(
        provider=profile.provider,
        model=profile.model,
        base_url=profile.base_url,
        credential_secret_id=profile.credential_secret_id,
        request_timeout_ms=profile.request_timeout_ms,
    )
    parameters = dict(profile.parameters) if profile.parameters else None
    results = []
    try:
        for case in cases:
            results.append(
                await run_one(
                    provider=client,
                    analyzer=analyzer,
                    target=target,
                    parameters=parameters,
                    system_instruction=version.system_instruction,
                    case=case,
                )
            )
    finally:
        if owns_provider:
            await client.aclose()
    return results


def artifact_path(*, sha: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return artifact_dir("level3") / (
        f"l3_02_natural_language_analysis_{sha[:12]}_{stamp}.json"
    )


async def execute(args: argparse.Namespace) -> dict[str, Any]:
    sha = git_sha()
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "metric_id": "L3-02",
        "metric_name": "자연어 업무지시 분석 성공률",
        "measured_at": measured_at(),
        "git_sha": sha,
        "environment": environment_label(),
        "mode": "run" if args.run else "preflight",
        "dataset_version": DATASET_VERSION,
        "target_min_percent": target_min_percent(),
        "measurement_definition": (
            "Live ModelProvider JSON generation with RequestAnalyzerService "
            "production prompt, StructuredRequestV1 validation, and Analyzer "
            "clarification sanitization. No AgentRequest lifecycle writes, "
            "Tool selection/execution, or database modifications."
        ),
    }
    data = load_dataset()
    check = validate_dataset(data)
    result["dataset_check"] = check
    if not check["ok"]:
        result.update(ok=False, reason=check["reason"])
        return result
    if not args.preflight and not args.run:
        result.update(ok=False, reason="MODE_REQUIRED")
        return result
    if args.run and check["review_status"] != "APPROVED":
        result.update(ok=False, reason="GOLD_SET_NOT_APPROVED")
        return result
    if args.run and not args.ack_gold_reviewed:
        result.update(ok=False, reason="GOLD_REVIEW_ACK_REQUIRED")
        return result
    if args.run and sha in {"", "unknown"}:
        result.update(ok=False, reason="GIT_SHA_UNKNOWN")
        return result
    try:
        agent_version_id = uuid.UUID(args.agent_version_id)
    except (ValueError, TypeError):
        result.update(ok=False, reason="AGENT_VERSION_ID_INVALID")
        return result
    result["agent_version_id"] = str(agent_version_id)
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
            if args.preflight and not args.run:
                result.update(ok=True, note="Read-only preflight; no LLM requests.")
                return result
            result["gold_review_attested"] = bool(args.ack_gold_reviewed)
            result["gold_reviewer"] = data["reviewed_by"]
            result["gold_reviewed_at"] = data["reviewed_at"]
            # Cases are frozen before the run; no dynamic data generation.
            rows = await evaluate_cases(
                session=session, version=version, profile=profile,
                cases=list(data["cases"]),
            )
            result["case_results"] = rows
            judgment = judge_l3_02(
                rows, expected_count=check["case_count"],
                target_min_percent=result["target_min_percent"],
            )
            result.update(judgment)
            result["ok"] = judgment["judgment"] in {"MET", "NOT_MET"}
    finally:
        await engine.dispose()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="L3-02 live read-only NL Analyzer evaluation")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--run", action="store_true")
    parser.add_argument("--agent-version-id", required=True)
    parser.add_argument(
        "--ack-gold-reviewed", action="store_true",
        help="Attest that operator reviewed frozen gold set before official run.",
    )
    parser.add_argument("--write-artifact", action="store_true")
    args = parser.parse_args(argv)
    report = asyncio.run(execute(args))
    if args.run and args.write_artifact and "case_results" in report:
        path = artifact_path(sha=report["git_sha"])
        # Exclusive create; never modify historical assessment evidence.
        with path.open("x", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
            f.write("\n")
        report["artifact_path"] = str(path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
