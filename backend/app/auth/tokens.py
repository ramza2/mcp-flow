"""Opaque token helpers for Session / CSRF credentials."""

from __future__ import annotations

import hashlib
import secrets


def generate_opaque_token(*, nbytes: int = 32) -> str:
    """CSPRNG opaque token (default 256-bit entropy)."""
    return secrets.token_urlsafe(nbytes)


def sha256_hex(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
