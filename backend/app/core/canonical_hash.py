"""Shared canonical JSON hashing (docs/05 §21).

Deterministic SHA-256 over sorted UTF-8 JSON. Do not hash DB JSON formatting.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def compute_canonical_json_hash(payload: Any) -> str:
    """SHA-256 lowercase hex of canonical JSON (sorted keys, compact separators).

    Serializer bytes must stay compatible with already-persisted Plan hashes
    (``compute_plan_hash``). Do not change separators / ensure_ascii / key
    ordering / numeric coercion here without an explicit hash-version migration.
    """
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
