"""AgentVersion content hashing helpers."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any


def agent_version_content_hash(
    *,
    system_instruction: str,
    llm_profile_id: uuid.UUID,
    request_schema_version: str,
    plan_schema_version: str,
    selection_settings: dict[str, Any],
    planning_settings: dict[str, Any],
    response_settings: dict[str, Any],
) -> str:
    """Deterministic SHA-256 over authoring content fields only."""

    payload = {
        "system_instruction": system_instruction,
        "llm_profile_id": str(llm_profile_id),
        "request_schema_version": request_schema_version,
        "plan_schema_version": plan_schema_version,
        "selection_settings": selection_settings,
        "planning_settings": planning_settings,
        "response_settings": response_settings,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
