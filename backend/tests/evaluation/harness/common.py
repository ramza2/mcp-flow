"""Shared helpers for regression/KPI harnesses (no secrets, no new runtime)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

EVAL_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = EVAL_ROOT.parents[1]
REPO_ROOT = BACKEND_ROOT.parent


def repo_root() -> Path:
    return REPO_ROOT


def backend_root() -> Path:
    return BACKEND_ROOT


def eval_root() -> Path:
    return EVAL_ROOT


def load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def git_sha() -> str:
    env_sha = os.environ.get("GITHUB_SHA") or os.environ.get("MCPFLOW_GIT_SHA")
    if env_sha:
        return env_sha.strip()
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return out.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def environment_label() -> str:
    return os.environ.get("MCPFLOW_ENV_LABEL", "local-dev").strip() or "local-dev"


def measured_at() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def artifact_dir(kind: str) -> Path:
    """Runtime evidence directory (gitignored). kind: kpi|regression."""
    override = os.environ.get("MCPFLOW_ARTIFACT_DIR")
    if override:
        base = Path(override)
    else:
        base = REPO_ROOT / "artifacts"
    path = base / kind
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=False)
        fh.write("\n")


def existing_paths(paths: list[str], *, cwd: Path | None = None) -> list[str]:
    root = cwd or BACKEND_ROOT
    found: list[str] = []
    for rel in paths:
        if (root / rel).exists():
            found.append(rel)
    return found


def run_pytest(
    paths: list[str],
    *,
    extra_args: list[str] | None = None,
    integration: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run pytest against existing paths. Returns CompletedProcess (never raises)."""
    args = [
        sys.executable,
        "-m",
        "pytest",
        *paths,
        "-q",
        "--tb=line",
        # Clear pyproject addopts so our -m selection is authoritative.
        "-o",
        "addopts=",
    ]
    if integration:
        args.extend(["-m", "integration"])
    else:
        args.extend(["-m", "not integration"])
    if extra_args:
        args.extend(extra_args)
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(BACKEND_ROOT))
    return subprocess.run(
        args,
        cwd=BACKEND_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def percent(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round((numerator / denominator) * 100.0, 4)


def redact_secrets(obj: Any) -> Any:
    """Best-effort strip of credential-like keys from nested structures."""
    blocked = {
        "password",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "private_key",
        "access_key",
        "raw_secret",
        "request_state",
    }
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            key_l = str(key).lower()
            if any(b in key_l for b in blocked):
                out[key] = "[REDACTED]"
            else:
                out[key] = redact_secrets(value)
        return out
    if isinstance(obj, list):
        return [redact_secrets(item) for item in obj]
    return obj
