#!/usr/bin/env python3
"""Fail-closed file impact classification for MCPFlow CI.

PR: compare base SHA against GitHub's synthetic merge checkout.
Push: compare event before SHA against pushed HEAD.
Manual and uncertain diffs: run all jobs. No service calls or mutations.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

_SHA = re.compile(r"[0-9a-f]{40}\\Z")


def all_jobs(reason: str) -> dict[str, str]:
    return {"backend": "true", "frontend": "true",
            "postgres": "true", "reason": reason}


def route_paths(paths: list[str]) -> dict[str, str]:
    if not paths:
        return all_jobs("EMPTY_DIFF_FAIL_CLOSED")
    backend = frontend = postgres = False
    for path in paths:
        if not path or path.startswith("/") or ".." in path.split("/"):
            return all_jobs("INVALID_PATH_FAIL_CLOSED")
        # Documentation and top-level guidance do not change runtime code.
        if path.startswith("docs/") or path == "AGENTS.md" or path == "LICENSE" or (
            "/" not in path and path.lower().endswith(".md")
        ):
            continue
        if path.startswith("frontend/"):
            frontend = True
        elif path.startswith("backend/"):
            backend = True
            # Test-only changes cannot alter runtime PostgreSQL behavior.
            if not path.startswith(("backend/tests/unit/", "backend/tests/evaluation/")):
                postgres = True
        else:
            # CI, scripts, infrastructure, root configs, unfamiliar paths.
            return all_jobs("SHARED_OR_UNCLASSIFIED_FAIL_CLOSED")
    return {
        "backend": str(backend).lower(),
        "frontend": str(frontend).lower(),
        "postgres": str(postgres).lower(),
        "reason": "SELECTIVE" if backend or frontend else "DOCS_ONLY",
    }


def determine() -> dict[str, str]:
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    if event_name == "workflow_dispatch":
        return all_jobs("MANUAL_FULL")
    if event_name not in {"pull_request", "push"}:
        return all_jobs("UNKNOWN_EVENT_FAIL_CLOSED")
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        head = os.environ["GITHUB_SHA"].lower()
        if event_name == "pull_request":
            base = event["pull_request"]["base"]["sha"].lower()
        else:
            base = event["before"].lower()
        if not _SHA.fullmatch(base) or not _SHA.fullmatch(head):
            return all_jobs("INVALID_SHA_FAIL_CLOSED")
        if base == head:
            return all_jobs("SAME_SHA_FAIL_CLOSED")
        result = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--no-renames",
             "--name-only", "-z", "--diff-filter=ACDMRTUXB", base, head],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        paths = [
            chunk.decode("utf-8", "surrogateescape")
            for chunk in result.stdout.split(b"\\0") if chunk
        ]
        return route_paths(paths)
    except (OSError, KeyError, TypeError, ValueError, subprocess.CalledProcessError):
        return all_jobs("DIFF_ERROR_FAIL_CLOSED")


def main() -> None:
    result = determine()
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as target:
            for key, value in result.items():
                target.write(f"{key}={value}\\n")
    print("CI IMPACT: " + json.dumps(result, sort_keys=True))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as target:
            target.write(
                "### CI impact routing\\n\\n"
                + " | ".join(f"{key}: {value}" for key, value in result.items())
                + "\\n"
            )


if __name__ == "__main__":
    main()
