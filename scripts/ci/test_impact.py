"""Offline, zero-network CI impact routing regression tests."""
import unittest

from impact import all_jobs, route_paths


class RouteImpactTests(unittest.TestCase):
    def test_frontend_only(self):
        self.assertEqual(route_paths(["frontend/src/App.tsx"]), {
            "backend": "false", "frontend": "true",
            "postgres": "false", "reason": "SELECTIVE",
        })

    def test_backend_app_requires_integration(self):
        d = route_paths(["backend/app/services/execution.py"])
        self.assertEqual((d["backend"], d["frontend"], d["postgres"]),
                         ("true", "false", "true"))

    def test_evaluation_only_skips_postgres(self):
        d = route_paths(["backend/tests/evaluation/test_level3_l304.py"])
        self.assertEqual((d["backend"], d["postgres"]), ("true", "false"))

    def test_unit_only_skips_postgres(self):
        d = route_paths(["backend/tests/unit/test_policy.py"])
        self.assertEqual((d["backend"], d["postgres"]), ("true", "false"))

    def test_integration_test_requires_postgres(self):
        d = route_paths(["backend/tests/integration/test_runtime.py"])
        self.assertEqual(d["postgres"], "true")

    def test_frontend_and_backend(self):
        d = route_paths(["frontend/src/main.tsx", "backend/app/main.py"])
        self.assertEqual((d["backend"], d["frontend"], d["postgres"]),
                         ("true", "true", "true"))

    def test_docs_only(self):
        self.assertEqual(route_paths(["docs/09-test-strategy.md", "AGENTS.md"]), {
            "backend": "false", "frontend": "false",
            "postgres": "false", "reason": "DOCS_ONLY",
        })

    def test_workflow_change_runs_all(self):
        self.assertEqual(route_paths([".github/workflows/ci.yml"]),
                         all_jobs("SHARED_OR_UNCLASSIFIED_FAIL_CLOSED"))

    def test_scripts_change_runs_all(self):
        self.assertEqual(route_paths(["scripts/deploy.sh"]),
                         all_jobs("SHARED_OR_UNCLASSIFIED_FAIL_CLOSED"))

    def test_unknown_path_runs_all(self):
        self.assertEqual(route_paths(["some-new-directory/file.txt"]),
                         all_jobs("SHARED_OR_UNCLASSIFIED_FAIL_CLOSED"))

    def test_empty_and_bad_path_fail_closed(self):
        self.assertEqual(route_paths([]), all_jobs("EMPTY_DIFF_FAIL_CLOSED"))
        self.assertEqual(route_paths(["../unsafe"]),
                         all_jobs("INVALID_PATH_FAIL_CLOSED"))


if __name__ == "__main__":
    unittest.main()
