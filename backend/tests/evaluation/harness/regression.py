"""Final integration regression runner (Issue #119).

Composes existing unit/api/integration pytest modules per scenario.
Does not create a second orchestration runtime.

Missing non-optional matrix paths fail the scenario (no silent PASS).
Unit-only runs are labeled clearly and are not "full integration PASS".
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from tests.evaluation.harness.common import (
    artifact_dir,
    environment_label,
    existing_paths,
    git_sha,
    load_yaml,
    measured_at,
    redact_secrets,
    repo_root,
    run_pytest,
    write_json,
)


def _matrix_path():
    from tests.evaluation.harness.common import eval_root

    return eval_root() / "regression_matrix.yaml"


def _split_paths(paths: list[str]) -> tuple[list[str], list[str]]:
    """Return (required, optional) path lists. Paths tagged with optional: prefix."""
    required: list[str] = []
    optional: list[str] = []
    for path in paths:
        if isinstance(path, str) and path.startswith("optional:"):
            optional.append(path.removeprefix("optional:"))
        else:
            required.append(path)
    return required, optional


def run_scenario(
    scenario: dict[str, Any],
    *,
    include_integration: bool,
) -> dict[str, Any]:
    scenario_id = scenario["id"]
    title = scenario.get("title", scenario_id)
    layers = list(scenario.get("layers") or [])
    extra = list(scenario.get("pytest_args") or [])
    results: list[dict[str, Any]] = []
    overall = "PASS"
    integration_omitted = "integration" in layers and not include_integration

    layer_specs: list[tuple[str, list[str], bool]] = []
    if "unit" in layers:
        layer_specs.append(("unit", list(scenario.get("unit") or []), False))
    if "api" in layers:
        layer_specs.append(("api", list(scenario.get("api") or []), False))
    if "integration" in layers and include_integration:
        layer_specs.append(
            ("integration", list(scenario.get("integration") or []), True)
        )

    if not layer_specs:
        return {
            "id": scenario_id,
            "title": title,
            "status": "SKIP",
            "reason": "NO_LAYERS_SELECTED",
            "integration_omitted": integration_omitted,
            "layers": [],
        }

    for layer_name, paths, is_integration in layer_specs:
        required_paths, optional_paths = _split_paths(paths)
        present_required = existing_paths(required_paths)
        missing_required = [p for p in required_paths if p not in present_required]
        present_optional = existing_paths(optional_paths)
        missing_optional = [p for p in optional_paths if p not in present_optional]

        if missing_required:
            overall = "FAIL"
            results.append(
                {
                    "layer": layer_name,
                    "status": "FAIL",
                    "reason": "MISSING_REQUIRED_PATH",
                    "paths": present_required,
                    "missing_required": missing_required,
                    "missing_optional": missing_optional,
                    "optional_paths_present": present_optional,
                }
            )
            continue

        if not present_required and not present_optional:
            overall = "FAIL"
            results.append(
                {
                    "layer": layer_name,
                    "status": "FAIL",
                    "reason": "NO_PATHS_FOUND",
                    "paths": [],
                    "missing_required": required_paths,
                    "missing_optional": optional_paths,
                }
            )
            continue

        run_paths = present_required + present_optional
        proc = run_pytest(run_paths, extra_args=extra, integration=is_integration)
        status = "PASS" if proc.returncode == 0 else "FAIL"
        if status == "FAIL":
            overall = "FAIL"
        tail = (proc.stdout or "")[-2000:] + (proc.stderr or "")[-1000:]
        results.append(
            {
                "layer": layer_name,
                "status": status,
                "returncode": proc.returncode,
                "paths": run_paths,
                "missing_required": missing_required,
                "missing_optional": missing_optional,
                "output_tail": tail,
            }
        )

    if overall == "PASS" and all(r["status"] == "SKIP" for r in results):
        overall = "SKIP"

    return {
        "id": scenario_id,
        "title": title,
        "status": overall,
        "integration_omitted": integration_omitted,
        "layers": results,
    }


def run_matrix(*, include_integration: bool | None = None) -> dict[str, Any]:
    if include_integration is None:
        include_integration = bool(
            __import__("os").environ.get("TEST_DATABASE_URL", "").strip()
        )
    matrix = load_yaml(_matrix_path())
    scenarios = list(matrix.get("scenarios") or [])
    run_results = [
        run_scenario(s, include_integration=include_integration) for s in scenarios
    ]
    passed = sum(1 for r in run_results if r["status"] == "PASS")
    failed = sum(1 for r in run_results if r["status"] == "FAIL")
    skipped = sum(1 for r in run_results if r["status"] == "SKIP")
    scope = (
        "unit_api_and_integration"
        if include_integration
        else "unit_api_only_integration_omitted"
    )
    report = {
        "schema_version": "mcpflow.final_regression.v2",
        "matrix_version": matrix.get("matrix_version"),
        "measured_at": measured_at(),
        "git_sha": git_sha(),
        "environment": environment_label(),
        "include_integration": include_integration,
        "scope": scope,
        "summary": {
            "total": len(run_results),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "ok": failed == 0 and passed > 0,
            # Explicit: unit-only success is not full integration regression PASS.
            "full_integration_regression": bool(
                include_integration and failed == 0 and passed > 0
            ),
        },
        "scenarios": run_results,
    }
    return redact_secrets(report)


def print_human_summary(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print("=== MCPFlow Final Regression ===")
    print(f"matrix_version : {report.get('matrix_version')}")
    print(f"git_sha        : {report.get('git_sha')}")
    print(f"environment    : {report.get('environment')}")
    print(f"measured_at    : {report.get('measured_at')}")
    print(f"scope          : {report.get('scope')}")
    print(
        f"integration    : {'yes' if report.get('include_integration') else 'OMITTED'}"
    )
    print("")
    for item in report["scenarios"]:
        omitted = " (integration omitted)" if item.get("integration_omitted") else ""
        print(f"[{item['status']:<4}] {item['id']} — {item['title']}{omitted}")
    print("")
    print(
        f"SUMMARY: {summary['passed']} passed, "
        f"{summary['failed']} failed, {summary['skipped']} skipped "
        f"/ {summary['total']} total"
    )
    print(f"matrix_ok (selected scope): {summary['ok']}")
    print(
        f"full_integration_regression: {summary['full_integration_regression']}"
    )
    if not report.get("include_integration"):
        print(
            "NOTE: Integration layers were not executed. "
            "Do not interpret this as final integration regression PASS."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MCPFlow final regression harness")
    parser.add_argument(
        "--integration",
        action="store_true",
        help="Force-include integration layers (requires TEST_DATABASE_URL)",
    )
    parser.add_argument(
        "--unit-only",
        action="store_true",
        help="Skip integration layers even if TEST_DATABASE_URL is set",
    )
    parser.add_argument(
        "--out",
        default="",
        help="JSON output path (default: artifacts/regression/latest.json)",
    )
    args = parser.parse_args(argv)

    if args.unit_only:
        include_integration = False
    elif args.integration:
        include_integration = True
    else:
        include_integration = None

    report = run_matrix(include_integration=include_integration)
    out_path = (
        __import__("pathlib").Path(args.out)
        if args.out
        else artifact_dir("regression") / "latest.json"
    )
    write_json(out_path, report)
    print_human_summary(report)
    print(f"\nJSON: {out_path}")
    print(f"repo: {repo_root()}")
    return 0 if report["summary"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
