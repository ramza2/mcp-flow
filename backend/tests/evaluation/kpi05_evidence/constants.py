"""Pinned ops ToolVersion + read-only reused KPI-05 Execution IDs."""

from __future__ import annotations

import uuid

# ACTIVE / VALID / READ_ONLY suggest_questions (ops evidence).
DEFAULT_SUGGEST_QUESTIONS_TOOL_VERSION_ID = uuid.UUID(
    "c3b1cfb7-9b16-4997-830b-cd0b4f33f8e2"
)

# Existing durable KPI-05 evidence — verify read-only; never mutate.
REUSED_EXECUTION_IDS: dict[str, uuid.UUID] = {
    "sequential_tool_chain": uuid.UUID("d272f597-a775-454e-86d7-7a499caaa47d"),
    "foreach_loop": uuid.UUID("c5dac6da-db84-477a-b1c4-07caf965f7ed"),
    "while_loop": uuid.UUID("f1ad1ab3-497e-4bd0-a9e6-488e08fbdaaf"),
    "approval_wait_resume": uuid.UUID("0bd5f5d0-8839-4d2f-aa50-87aada3d5d8b"),
}

SCENARIO_PARALLEL = "parallel_join_wave"
SCENARIO_CONDITION = "condition_branch"

EVIDENCE_SCHEMA_VERSION = "mcpflow.kpi05_parallel_condition_evidence.v1"
