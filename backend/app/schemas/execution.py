"""Execution creation response schemas — docs/06 AgentRequest executions."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AgentRequestExecutionCreateResult(BaseModel):
    """POST /agent-requests/{id}/executions success body."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    status: str
    source_type: str
    trigger_type: str
    agent_request_id: uuid.UUID
    agent_version_id: uuid.UUID
    plan_hash: str = Field(min_length=64, max_length=64)
    requested_at: datetime
    step_count: int = Field(ge=1)
