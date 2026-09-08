"""Runtime authenticated principal (immutable; no Role/Permission cache)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CurrentPrincipal:
    user_id: uuid.UUID
    session_id: uuid.UUID
    username: str
    display_name: str | None = None
