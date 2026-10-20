"""KPI-05 parallel_join_wave / condition_branch evidence tooling.

Dry-run by default. Explicit ``--run`` registers new evaluation-only Workflows
and creates Executions. Never mutates existing KPI evidence Executions.
"""

from __future__ import annotations

from tests.evaluation.kpi05_evidence.constants import (
    DEFAULT_SUGGEST_QUESTIONS_TOOL_VERSION_ID,
    REUSED_EXECUTION_IDS,
)
from tests.evaluation.kpi05_evidence.plans import (
    build_condition_branch_plan,
    build_parallel_join_wave_plan,
)

__all__ = [
    "DEFAULT_SUGGEST_QUESTIONS_TOOL_VERSION_ID",
    "REUSED_EXECUTION_IDS",
    "build_condition_branch_plan",
    "build_parallel_join_wave_plan",
]
